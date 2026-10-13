"""Unencodable text: NUL bytes and lone surrogates (v1.97.0).

Two families of characters can appear in JSON text (a ``\\u0000`` or
``\\ud800`` escape decodes happily) yet can never cross the boundary
they are aimed at:

* A check **command** containing a NUL byte can never be spawned —
  the operating system passes commands as NUL-terminated strings,
  so ``subprocess`` raises ``ValueError: embedded null byte``.
  Before v1.97.0 the spec accepted such a command, the run executed
  the checks before it, and then died mid-run with no evidence
  record for the checks already decided.
* A **lone surrogate** (an unpaired half of a surrogate pair, e.g.
  from a JSON ``\\ud800`` escape) is not valid Unicode and cannot be
  encoded as UTF-8. The evidence chain hashes UTF-8 text, so a spec
  name / check name / check command carrying one crashed
  ``build_record`` *after* the checks ran; a forged record carrying
  one crashed ``verify_chain`` itself with ``UnicodeEncodeError``
  instead of simply being invalid — a judge could not judge it.
* A patch **target path** containing a NUL byte raised the same raw
  ``ValueError`` out of ``apply_patch`` — not ``PatchError`` — so it
  escaped the repair loop's rejection handling and killed the
  repair; diff text containing lone surrogates crashed the applier
  the same way when the content was written as UTF-8.
* A loader **file path** containing a NUL byte raised a raw
  ``ValueError`` out of the path validation instead of the loaders'
  ``JsonFileError`` / ``SpecError`` contract.

The rules now, each mirrored at every layer that enforces it:

* Spec layer: ``parse_spec`` / ``validate_spec`` reject a spec name
  or check name carrying lone surrogates, and a check command
  carrying lone surrogates or a NUL byte, before any check runs.
  (A NUL in a *name* still seals and verifies fine — it is valid
  UTF-8 text; only the command crosses the spawn boundary.)
* Sandbox layer: ``validate_check_command`` mirrors the command
  rule as ``ValueError``.
* Evidence layer: ``verify_chain`` returns ``False`` (never raises)
  for a record carrying a lone surrogate anywhere in its sealed
  strings, and ``build_record`` refuses to seal one with a
  ``ValueError``. Adapter-supplied commentary (explanations, usage
  text, reference text) is *sanitised* on the sealing paths —
  surrogates become U+FFFD — because commentary must never cost a
  decided verdict its record.
* Patch layer: a NUL in a target path, or lone surrogates anywhere
  in the diff text, is a ``PatchError`` at parse time, so the
  repair loop feeds it back like any other inapplicable diff.
* Loader layer: a NUL in a file path is a ``JsonFileError``.
* Display layer: the verify result's text fields and the rendered
  HTML view are sanitised (U+FFFD) at their construction/write
  sites, so judging a forged record — printing its summary line,
  rendering its page — never crashes on the character that makes
  it invalid.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from verifygate import cli, patch
from verifygate.evidence import (
    build_record,
    normalise_adapter_name,
    normalise_usage_calls,
    verify_chain,
)
from verifygate.jsonio import (
    JsonFileError,
    has_lone_surrogates,
    load_json_file,
    replace_lone_surrogates,
    validate_file_path,
)
from verifygate.mock_llm import MockLLMAdapter
from verifygate.patch import PatchError, apply_patch
from verifygate.runner import _cap_error, normalise_explanation, run_verification
from verifygate.sandbox import validate_check_command
from verifygate.spec import SpecError, TaskSpec, load_spec, parse_spec, validate_spec
from verifygate.tavily import MockReferenceLookup, normalise_reference
from verifygate.verify import verify_document

SAMPLES = Path(__file__).resolve().parents[1] / "samples"

SURR = "\ud800"  # a lone high surrogate: not valid Unicode
REPL = "\ufffd"


def _spec(**overrides):
    raw = {
        "name": "demo",
        "brief": "demo brief",
        "checks": [{"name": "unit", "command": "true", "timeout_s": 5}],
    }
    raw.update(overrides)
    return raw


def _spec_with_command(command):
    return _spec(checks=[{"name": "unit", "command": command, "timeout_s": 5}])


def _passing_record():
    return json.loads((SAMPLES / "evidence_passing.json").read_text())


# ---------------------------------------------------------------- jsonio


def test_surrogate_helpers():
    assert not has_lone_surrogates("plain text")
    assert not has_lone_surrogates("café 😀")
    assert not has_lone_surrogates("nul\x00byte")  # NUL is valid UTF-8
    assert has_lone_surrogates(f"bad {SURR} text")
    assert replace_lone_surrogates(f"a{SURR}b\udfff") == f"a{REPL}b{REPL}"
    assert replace_lone_surrogates("clean") == "clean"


def test_validate_file_path_rejects_nul():
    with pytest.raises(JsonFileError):
        validate_file_path("abc\x00.json", "spec path")
    with pytest.raises(JsonFileError):
        load_json_file("abc\x00.json", max_bytes=1024, kind="spec")


def test_load_spec_nul_path_is_spec_error(tmp_path):
    with pytest.raises(SpecError):
        load_spec("abc\x00.json")


# ------------------------------------------------------------------- spec


@pytest.mark.parametrize("raw", [
    _spec(name=f"de{SURR}mo"),
    _spec(checks=[{"name": f"un{SURR}it", "command": "true", "timeout_s": 5}]),
    _spec_with_command(f"echo {SURR}"),
])
def test_parse_spec_rejects_lone_surrogates_in_sealed_fields(raw):
    with pytest.raises(SpecError):
        parse_spec(raw)


@pytest.mark.parametrize("command", ["echo a\x00b", "\x00", "true\x00"])
def test_parse_spec_rejects_nul_command(command):
    with pytest.raises(SpecError):
        parse_spec(_spec_with_command(command))


def test_parse_spec_still_accepts_nul_in_names():
    # A NUL byte is valid UTF-8 text: in a *name* it seals into the
    # record and the record verifies. Only the command (which must
    # be spawned) and surrogates (which cannot be encoded at all)
    # are rejected.
    spec = parse_spec(_spec(name="de\x00mo"))
    assert spec.name == "de\x00mo"


def test_parse_spec_accepts_ordinary_unicode():
    spec = parse_spec(_spec(name="café-😀", brief="naïve façade"))
    assert spec.name == "café-😀"


def test_validate_spec_rejects_directly_constructed_bad_specs():
    good = parse_spec(_spec_with_command("true"))
    bad_command = TaskSpec(
        name=good.name, brief=good.brief,
        checks=(type(good.checks[0])(
            name="unit", command="echo a\x00b", timeout_s=5),),
        raw=_spec_with_command("echo a\x00b"))
    with pytest.raises(SpecError):
        validate_spec(bad_command)
    bad_name = TaskSpec(
        name=f"de{SURR}mo", brief=good.brief, checks=good.checks,
        raw=_spec(name=f"de{SURR}mo"))
    with pytest.raises(SpecError):
        validate_spec(bad_name)


# ---------------------------------------------------------------- sandbox


@pytest.mark.parametrize("command", ["echo a\x00b", f"echo {SURR}"])
def test_validate_check_command_rejects_unspawnable(command):
    with pytest.raises(ValueError):
        validate_check_command(command)


def test_validate_check_command_accepts_normal():
    assert validate_check_command("python3 -m pytest -q") == \
        "python3 -m pytest -q"


# --------------------------------------------------------------- evidence


def _forged(mutate):
    record = _passing_record()
    mutate(record)
    return record


def test_verify_chain_surrogate_in_header_name_is_false_not_crash():
    record = _forged(lambda r: r["entries"][0].update(
        spec_name=f"x{SURR}y"))
    assert verify_chain(record) is False


def test_verify_chain_surrogate_in_check_fields_is_false_not_crash():
    for field in ("name", "command", "stdout_tail", "stderr_tail"):
        record = _forged(lambda r, f=field: r["entries"][1].update(
            **{f: f"x{SURR}"}))
        assert verify_chain(record) is False, field


def test_verify_chain_surrogate_in_metadata_is_false_not_crash():
    record = _forged(lambda r: r.update(replay_command=f"run {SURR}"))
    assert verify_chain(record) is False


def test_verify_document_reports_surrogate_record_invalid():
    record = _forged(lambda r: r["entries"][0].update(
        spec_name=f"x{SURR}y"))
    results = dict(verify_document(record))
    assert results["record"]["chain_ok"] is False


def test_cli_verify_surrogate_record_exits_1(capsys, tmp_path):
    record = _forged(lambda r: r["entries"][0].update(
        spec_name=f"x{SURR}y"))
    path = tmp_path / "forged.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    code = cli.main(["verify", "--record", str(path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "CHAIN INVALID" in out


def test_cli_view_surrogate_record_still_renders(capsys, tmp_path):
    # Judging a forged record is the view's whole job: the page
    # renders (with the invalid-chain banner), the unencodable
    # character degraded to U+FFFD, instead of the write crashing.
    record = _forged(lambda r: r["entries"][0].update(
        spec_name=f"x{SURR}y"))
    path = tmp_path / "forged.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    out = tmp_path / "view.html"
    code = cli.main(["view", "--record", str(path), "--out", str(out)])
    assert code == 0
    html = out.read_text(encoding="utf-8")
    assert f"x{REPL}y" in html


def _check_result(**overrides):
    result = {
        "name": "unit", "command": "true", "exit_code": 0,
        "duration_ms": 5, "stdout_tail": "", "stderr_tail": "",
        "timed_out": False, "verdict": "PASS",
    }
    result.update(overrides)
    return result


def _build(**overrides):
    kwargs = dict(
        spec_name="demo", spec_hash="a" * 64,
        candidate_tree_hash="b" * 64, mode="judge",
        check_results=[_check_result()], verdict="PASS",
        diagnosis=None,
        replay_command="python3 -m verifygate run --spec spec.json "
                       "--candidate candidate --mode judge",
    )
    kwargs.update(overrides)
    return build_record(**kwargs)


def test_build_record_refuses_to_seal_surrogates():
    with pytest.raises(ValueError):
        _build(spec_name=f"de{SURR}mo")
    with pytest.raises(ValueError):
        _build(check_results=[_check_result(command=f"echo {SURR}")])
    with pytest.raises(ValueError):
        _build(check_results=[_check_result(stdout_tail=f"out{SURR}")])
    with pytest.raises(ValueError):
        _build(diagnosis={"adapter": "mock", "explanation": f"x{SURR}",
                          "failed_checks": [], "references": []})
    with pytest.raises(ValueError):
        _build(llm_usage=[{"adapter": "a", "model": f"m{SURR}",
                           "purpose": "repair", "usage": None}])
    genuine = _build()
    assert verify_chain(genuine) is True


def test_build_record_refuses_nul_command_mirror():
    # A NUL command is not a shape the runner can emit (the sandbox
    # cannot spawn it), so the structure mirror rejects the record.
    record = _build(check_results=[_check_result(command="echo a\x00b")])
    assert verify_chain(record) is False


# ------------------------------------------------- sealing-path sanitising


def test_normalise_explanation_replaces_surrogates():
    assert normalise_explanation(f"the cause {SURR} is x") == \
        f"the cause {REPL} is x"


def test_cap_error_replaces_surrogates():
    assert _cap_error(f"boom {SURR}") == f"boom {REPL}"


def test_normalise_adapter_name_replaces_surrogates():
    assert normalise_adapter_name(f"adap{SURR}ter", "fallback") == \
        f"adap{REPL}ter"


def test_normalise_usage_calls_replaces_surrogates():
    calls = normalise_usage_calls([{
        "adapter": "token-factory", "model": f"m{SURR}",
        "purpose": "diagnosis", "finish_reason": "stop",
        "usage": {"prompt_tokens": 3, "completion_tokens": 4,
                  "total_tokens": 7},
    }])
    assert calls[0]["model"] == f"m{REPL}"


def test_normalise_reference_replaces_surrogates():
    ref = normalise_reference({
        "title": f"ti{SURR}tle", "url": "https://example.test",
        "snippet": "body", "source": "tavily"})
    assert ref["title"] == f"ti{REPL}tle"


class _SurrogateExplainer(MockLLMAdapter):
    """An adapter whose diagnosis text carries a lone surrogate."""

    def explain_failure(self, check_results):
        return f"the model says {SURR} this broke"


def test_run_with_surrogate_diagnosis_still_seals_verifying_record(tmp_path):
    candidate = tmp_path / "cand"
    candidate.mkdir()
    (candidate / "x.txt").write_text("hi\n")
    spec = parse_spec(_spec(
        checks=[{"name": "unit", "command": "false", "timeout_s": 5}]))
    record = run_verification(
        spec=spec, candidate_dir=candidate, mode="judge",
        llm=_SurrogateExplainer(), references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label="candidate")
    assert record["verdict"] == "FAIL"
    assert verify_chain(record) is True
    diagnosis = next(e for e in record["entries"]
                     if e["kind"] == "diagnosis")
    assert REPL in diagnosis["explanation"]
    assert not has_lone_surrogates(json.dumps(record))


# ------------------------------------------------------------------ patch


def _modify_diff(target):
    return (f"--- a/{target}\n+++ b/{target}\n"
            "@@ -1 +1 @@\n-one\n+ONE\n")


def test_patch_nul_target_path_is_patch_error(tmp_path):
    (tmp_path / "a.txt").write_text("one\n")
    with pytest.raises(PatchError):
        apply_patch(tmp_path, _modify_diff("a\x00.txt"))
    assert (tmp_path / "a.txt").read_text() == "one\n"


def test_patch_nul_creation_path_is_patch_error(tmp_path):
    diff = ("--- /dev/null\n+++ b/new\x00.txt\n"
            "@@ -0,0 +1 @@\n+hello\n")
    with pytest.raises(PatchError):
        apply_patch(tmp_path, diff)
    assert list(tmp_path.iterdir()) == []


def test_patch_nul_git_quoted_path_is_patch_error(tmp_path):
    # \000 inside a git C-style quoted path decodes to a NUL byte.
    diff = ('--- a/"a\\000.txt"\n+++ b/"a\\000.txt"\n'
            "@@ -1 +1 @@\n-one\n+ONE\n")
    with pytest.raises(PatchError):
        apply_patch(tmp_path, diff)


def test_patch_nul_rename_path_is_patch_error(tmp_path):
    (tmp_path / "a.txt").write_text("one\n")
    diff = ("diff --git a/a.txt b/b.txt\n"
            "similarity index 100%\n"
            "rename from a.txt\n"
            "rename to b\x00.txt\n")
    with pytest.raises(PatchError):
        apply_patch(tmp_path, diff)
    assert (tmp_path / "a.txt").read_text() == "one\n"


def test_patch_surrogate_content_is_patch_error(tmp_path):
    (tmp_path / "a.txt").write_text("one\n")
    diff = _modify_diff("a.txt").replace("+ONE", f"+ON{SURR}E")
    with pytest.raises(PatchError):
        apply_patch(tmp_path, diff)
    assert (tmp_path / "a.txt").read_text() == "one\n"


def test_patch_surrogate_path_is_patch_error(tmp_path):
    with pytest.raises(PatchError):
        apply_patch(tmp_path, _modify_diff(f"a{SURR}.txt"))


# ---------------------------------------------------------- CLI end-to-end


def test_cli_nul_command_spec_fails_before_any_check_runs(
        capsys, tmp_path):
    # The NUL check is second: if validation did not happen before
    # any work, the first check's side effect (the marker) would
    # exist and a record would be on its way.
    candidate = tmp_path / "cand"
    candidate.mkdir()
    marker = candidate / "marker.txt"
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({
        "name": "nul-command",
        "brief": "second check can never run",
        "checks": [
            {"name": "first", "command": "touch marker.txt",
             "timeout_s": 10},
            {"name": "bad", "command": "echo a\x00b", "timeout_s": 10},
        ],
    }), encoding="utf-8")
    record_path = tmp_path / "record.json"
    code = cli.main(["run", "--spec", str(spec_path),
                     "--candidate", str(candidate),
                     "--out", str(record_path)])
    err = capsys.readouterr().err
    assert code == 2
    assert "spec error" in err
    assert not marker.exists()
    assert not record_path.exists()
