"""The spec brief must be encodable: lone surrogates (v1.100.0).

v1.97.0 judged the spec name, the check names, and the check
commands for lone surrogates — the text fields that are sealed
into an evidence record or spawned as a process. The brief was
left: it is neither sealed nor spawned, so it passed
``parse_spec`` / ``validate_spec`` (type and length only) and
``llm.validate_brief`` the same way.

But the brief is embedded verbatim in every patch-generation
prompt, and the mock adapter echoes it into its patch proposal —
and a proposal carrying lone surrogates is a ``PatchError`` at
parse time (v1.97.0: a diff is applied as UTF-8 text). So a
surrogate brief for a non-sample task poisoned every repair
round: the loop fed the rejection back, retried, and finally
stopped ``patch_not_applicable`` with the blame on the patch
text the brief had poisoned, while in real mode the same text
entered a paid Token Factory prompt as characters no UTF-8
encoding can carry.

All three judgement sites now reject a surrogate brief up
front, with the one error each layer already produces
(``SpecError`` at the spec layer, ``ValueError`` at the adapter
layer), before any check runs or any model call is made. A NUL
byte in a brief stays legal, exactly as in a spec name: it is
valid UTF-8, and a proposal echoing it stops as an ordinary
no-diff proposal, identically to a clean brief (pinned below).
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from verifygate.llm import validate_brief
from verifygate.mock_llm import MockLLMAdapter
from verifygate.repair import run_repair
from verifygate.runner import run_verification
from verifygate.spec import SpecError, load_spec, parse_spec, validate_spec
from verifygate.tavily import MockReferenceLookup
from verifygate.tokenfactory import TokenFactoryAdapter

APP = Path(__file__).resolve().parents[1]
FAILING = APP / "samples" / "failing"
SURROGATE_BRIEF = "rework the widget \ud800 loader"
CLEAN_SPEC = {
    "name": "widget-fix",
    "brief": "rework the widget loader",
    "checks": [{"name": "c", "command": "false", "timeout_s": 5}],
}


def _spec(brief):
    return parse_spec({**CLEAN_SPEC, "brief": brief})


def _raw_spec(brief):
    return {**CLEAN_SPEC, "brief": brief}


class _CountingLLM:
    """Duck adapter that counts its advisory calls (v1.42.0 shape)."""

    name = "counting-stub"

    def __init__(self):
        self.patch_calls = 0
        self.explain_calls = 0

    def generate_patch(self, brief, failing_evidence=None):
        self.patch_calls += 1
        return "no diff here"

    def explain_failure(self, check_results):
        self.explain_calls += 1
        return "diagnosis"


class _FakeTransport:
    def __init__(self):
        self.bodies = []

    def __call__(self, body):
        self.bodies.append(body)
        return {
            "choices": [{"message": {"content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                      "total_tokens": 2},
            "model": "fake-model",
        }


# -- the spec layer ----------------------------------------------------------


def test_parse_spec_rejects_a_surrogate_brief():
    with pytest.raises(SpecError, match="brief contains lone surrogates"):
        _spec(SURROGATE_BRIEF)


def test_load_spec_rejects_a_surrogate_brief_from_json(tmp_path):
    # The shape such a brief actually arrives in: a \uXXXX escape
    # in the spec file decodes to the lone surrogate.
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(_raw_spec("rework \ud800 loader")),
                    encoding="utf-8")
    with pytest.raises(SpecError, match="brief contains lone surrogates"):
        load_spec(path)


@pytest.mark.parametrize("brief", [
    "rework the widget loader", "", "候选 brief — ünicode ✓",
    "a\x00b",  # a NUL is valid UTF-8; see the module docstring
])
def test_parse_spec_accepts_encodable_briefs(brief):
    assert _spec(brief).brief == brief


def test_validate_spec_rejects_a_direct_surrogate_brief():
    spec = _spec(CLEAN_SPEC["brief"])
    forged = dataclasses.replace(spec, brief=SURROGATE_BRIEF)
    with pytest.raises(SpecError, match="brief contains lone surrogates"):
        validate_spec(forged)


def test_runner_rejects_a_surrogate_brief_before_any_work(tmp_path):
    spec = _spec(CLEAN_SPEC["brief"])
    forged = dataclasses.replace(spec, brief=SURROGATE_BRIEF)
    llm = _CountingLLM()
    with pytest.raises(SpecError, match="brief contains lone surrogates"):
        run_verification(
            spec=forged, candidate_dir=tmp_path / "missing-candidate",
            llm=llm, references=MockReferenceLookup(), mode="judge",
            spec_label="spec.json", candidate_label="cand")
    # The brief error wins over the missing candidate: validation
    # happens before the workspace exists, and no adapter is called.
    assert llm.patch_calls == 0 and llm.explain_calls == 0


def test_repair_rejects_a_surrogate_brief_with_zero_generation_calls():
    spec = _spec(CLEAN_SPEC["brief"])
    forged = dataclasses.replace(spec, brief=SURROGATE_BRIEF)
    llm = _CountingLLM()
    with pytest.raises(SpecError, match="brief contains lone surrogates"):
        run_repair(
            spec=forged, candidate_dir=FAILING, llm=llm,
            references=MockReferenceLookup(), mode="judge",
            spec_label="spec.json", candidate_label="samples/failing",
            max_rounds=3)
    assert llm.patch_calls == 0


# -- the adapter layer -------------------------------------------------------


def test_validate_brief_rejects_surrogates_and_accepts_encodable_text():
    with pytest.raises(ValueError, match="lone surrogates"):
        validate_brief(SURROGATE_BRIEF)
    assert validate_brief("fix it") == "fix it"
    assert validate_brief("") == ""
    assert validate_brief("a\x00b") == "a\x00b"


def test_mock_generate_patch_rejects_a_surrogate_brief():
    with pytest.raises(ValueError, match="lone surrogates"):
        MockLLMAdapter().generate_patch(SURROGATE_BRIEF, None)


def test_real_generate_patch_rejects_without_any_transport_call():
    transport = _FakeTransport()
    adapter = TokenFactoryAdapter(transport=transport)
    with pytest.raises(ValueError, match="lone surrogates"):
        adapter.generate_patch(SURROGATE_BRIEF, None)
    assert transport.bodies == []


# -- the CLI surface and the unchanged controls ------------------------------


def _cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "verifygate", *args],
        cwd=APP, capture_output=True, text=True, timeout=120)


def test_cli_run_and_repair_reject_a_surrogate_brief_spec(tmp_path):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_raw_spec(SURROGATE_BRIEF)),
                         encoding="utf-8")
    out = tmp_path / "record.json"
    run = _cli("run", "--spec", str(spec_path), "--candidate",
               str(FAILING), "--mode", "judge", "--out", str(out))
    assert run.returncode == 2
    assert "lone surrogates" in run.stderr
    assert not out.exists()
    report = tmp_path / "report.json"
    repair = _cli("repair", "--spec", str(spec_path), "--candidate",
                  str(FAILING), "--mode", "judge", "--out", str(report))
    assert repair.returncode == 2
    assert "lone surrogates" in repair.stderr
    assert not report.exists()


def test_clean_brief_repair_still_repairs_the_sample():
    # Control: the bundled sample's clean brief still drives the
    # mock's known-good diff to a FAIL -> PASS repair (the runner /
    # loop path the surrogate brief can no longer reach).
    spec = load_spec(APP / "samples" / "spec.json")
    assert "\ud800" not in spec.brief
    report = run_repair(
        spec=spec, candidate_dir=FAILING, llm=MockLLMAdapter(),
        references=MockReferenceLookup(), mode="judge",
        spec_label="samples/spec.json",
        candidate_label="samples/failing", max_rounds=3)
    assert report["final_verdict"] == "PASS"
    assert report["stopped_reason"] == "passed"


def test_nul_brief_still_stops_as_an_ordinary_no_diff_proposal():
    # Control for the scope line in the module docstring: a NUL
    # brief is legal and behaves exactly like a clean non-sample
    # brief — the mock's plan carries no diff and the loop stops
    # patch_not_applicable, it is not rejected up front.
    spec = _spec("rework the widget a\x00b loader")
    report = run_repair(
        spec=spec, candidate_dir=FAILING, llm=MockLLMAdapter(),
        references=MockReferenceLookup(), mode="judge",
        spec_label="spec.json", candidate_label="samples/failing",
        max_rounds=1)
    assert report["final_verdict"] == "FAIL"
    assert report["stopped_reason"] == "patch_not_applicable"
