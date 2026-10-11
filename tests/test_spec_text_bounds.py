"""Spec text bounds: a spec's strings are a bounded dimension too.

Before v1.28.0 every other dimension of a spec was capped — its
check count (``MAX_CHECKS``), its per-check timeouts
(``MAX_TIMEOUT_S``), its candidate's files and bytes — but the
length of the text it carries was not:

* a probe spec with a 100,000-character ``name``, a
  500,000-character ``brief``, and 100,000-character check
  names/commands was accepted by ``parse_spec`` verbatim;
* a record honestly sealed by ``build_record`` over those fields —
  plus a 500,000-character check output tail — verified INTACT.

The name and the check names/commands are sealed into every
evidence record the spec produces, the brief is embedded verbatim
in every real-mode patch prompt (a paid Token Factory call), and
the check tails are sealed per check — so one oversized spec (or
one forged record) bloated every record and prompt it touched.

Contract pinned here:
* ``parse_spec`` rejects text above the ceilings
  (``MAX_SPEC_NAME_CHARS`` / ``MAX_BRIEF_CHARS`` /
  ``MAX_CHECK_NAME_CHARS`` / ``MAX_CHECK_COMMAND_CHARS``) with a
  ``SpecError`` — a one-line usage error, exit 2, at every CLI
  entry point, before any check runs or any prompt is built;
  exactly-at-ceiling text is accepted;
* ``evidence``'s structure check mirrors the sealed fields' caps,
  plus the sandbox's tail cap (``MAX_TAIL_CHARS``) for
  ``stdout_tail`` / ``stderr_tail`` — the sandbox already truncates
  a sealed tail to ``MAX_OUTPUT_CHARS`` plus its marker, so a
  longer tail is not a shape a real run emits;
* every committed fixture sits far below every cap and still
  verifies.
"""

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from verifygate import cli
from verifygate.evidence import (
    GENESIS,
    MAX_TAIL_CHARS,
    _canonical,
    _sha,
    build_record,
    signing_key,
    verify_chain,
)
from verifygate.sandbox import MAX_OUTPUT_CHARS
from verifygate.spec import (
    MAX_BRIEF_CHARS,
    MAX_CHECK_COMMAND_CHARS,
    MAX_CHECK_NAME_CHARS,
    MAX_SPEC_NAME_CHARS,
    SpecError,
    parse_spec,
)
from verifygate.verify import document_ok, verify_document
from verifygate.webview import render_document

APP = Path(__file__).resolve().parent.parent
BIG = "x" * 100_000


def _raw(**overrides) -> dict:
    raw = {
        "name": "text-bounds",
        "brief": "A spec about bounded text.",
        "checks": [{"name": "gate", "command": "true", "timeout_s": 60}],
    }
    raw.update(overrides)
    return raw


def _check(**overrides) -> dict:
    check = {
        "name": "gate", "command": "true", "exit_code": 0,
        "duration_ms": 1, "stdout_tail": "", "stderr_tail": "",
        "timed_out": False, "verdict": "PASS",
    }
    check.update(overrides)
    return check


def _record(check=None, spec_name="text-bounds") -> dict:
    return build_record(
        spec_name=spec_name, spec_hash="a" * 64,
        candidate_tree_hash="b" * 64, mode="judge",
        check_results=[check or _check()], verdict="PASS",
        diagnosis=None,
        replay_command="python3 -m verifygate run --spec s.json "
                       "--candidate cand --mode judge")


def _rechain(record):
    """Recompute a (chain v3) record's chain + signature in place.

    The dev signing key is public by design, so this proves the
    *structure* rules — not the chain — catch an over-limit field.
    """
    prev = _sha(GENESIS)
    for entry in record["entries"]:
        payload = {k: v for k, v in entry.items() if k != "hash"}
        prev = _sha(prev + _canonical(payload))
        entry["hash"] = prev
    record["record_hash"] = _sha(prev + _canonical({
        "verdict": record["verdict"],
        "version": record["version"],
        "created_at": record["created_at"],
        "replay": record["replay"],
        "signing_key_is_default_dev_key":
            record["signing_key_is_default_dev_key"],
    }))
    record["signature"] = hmac.new(
        signing_key().encode("utf-8"),
        record["record_hash"].encode("utf-8"),
        hashlib.sha256).hexdigest()
    return record


# -- the ceilings themselves ------------------------------------------

def test_ceilings_are_the_documented_generous_values():
    assert MAX_SPEC_NAME_CHARS == 200
    assert MAX_BRIEF_CHARS == 10_000
    assert MAX_CHECK_NAME_CHARS == 200
    assert MAX_CHECK_COMMAND_CHARS == 4_000
    # The tail ceiling is the sandbox's output cap plus its marker.
    assert MAX_TAIL_CHARS == MAX_OUTPUT_CHARS + 14
    # The bundled specs sit far below every text ceiling.
    for spec_path in (APP / "samples" / "spec.json",
                      APP / "samples-js" / "spec.json",
                      APP / "dogfood_spec.json"):
        raw = json.loads(spec_path.read_text(encoding="utf-8"))
        assert len(raw["name"]) * 4 < MAX_SPEC_NAME_CHARS
        assert len(raw["brief"]) * 4 < MAX_BRIEF_CHARS
        for c in raw["checks"]:
            assert len(c["name"]) * 4 < MAX_CHECK_NAME_CHARS
            assert len(c["command"]) * 4 < MAX_CHECK_COMMAND_CHARS


# -- the parser --------------------------------------------------------

def test_parse_accepts_exactly_the_ceilings():
    spec = parse_spec(_raw(
        name="n" * MAX_SPEC_NAME_CHARS,
        brief="b" * MAX_BRIEF_CHARS,
        checks=[{"name": "c" * MAX_CHECK_NAME_CHARS,
                 "command": "t" * MAX_CHECK_COMMAND_CHARS,
                 "timeout_s": 60}]))
    assert len(spec.name) == MAX_SPEC_NAME_CHARS
    assert len(spec.brief) == MAX_BRIEF_CHARS
    assert len(spec.checks[0].name) == MAX_CHECK_NAME_CHARS
    assert len(spec.checks[0].command) == MAX_CHECK_COMMAND_CHARS


@pytest.mark.parametrize("field,raw", [
    ("name", _raw(name="n" * (MAX_SPEC_NAME_CHARS + 1))),
    ("brief", _raw(brief="b" * (MAX_BRIEF_CHARS + 1))),
    ("check name", _raw(checks=[
        {"name": "c" * (MAX_CHECK_NAME_CHARS + 1),
         "command": "true", "timeout_s": 60}])),
    ("command", _raw(checks=[
        {"name": "gate",
         "command": "t" * (MAX_CHECK_COMMAND_CHARS + 1),
         "timeout_s": 60}])),
])
def test_parse_rejects_above_each_ceiling(field, raw):
    with pytest.raises(SpecError, match="at most"):
        parse_spec(raw)


def test_parse_rejects_the_original_probe_spec():
    with pytest.raises(SpecError, match="at most"):
        parse_spec({
            "name": BIG, "brief": "y" * 500_000,
            "checks": [{"name": BIG, "command": BIG, "timeout_s": 60}]})


def test_cli_run_rejects_over_ceiling_spec(tmp_path):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_raw(
        brief="b" * (MAX_BRIEF_CHARS + 1))), encoding="utf-8")
    out = tmp_path / "e.json"
    rc = cli.main([
        "run", "--spec", str(spec_path),
        "--candidate", str(APP / "samples" / "passing"),
        "--mode", "judge", "--out", str(out),
    ])
    assert rc == 2
    assert not out.exists()  # a usage error writes no record


def test_cli_repair_rejects_over_ceiling_spec(tmp_path):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_raw(
        name="n" * (MAX_SPEC_NAME_CHARS + 1))), encoding="utf-8")
    out = tmp_path / "r.json"
    rc = cli.main([
        "repair", "--spec", str(spec_path),
        "--candidate", str(APP / "samples" / "failing"),
        "--mode", "judge", "--out", str(out),
    ])
    assert rc == 2
    assert not out.exists()


# -- the evidence mirror ------------------------------------------------

def test_record_at_the_ceilings_verifies():
    record = _record(
        check=_check(name="c" * MAX_CHECK_NAME_CHARS,
                     command="t" * MAX_CHECK_COMMAND_CHARS,
                     stdout_tail="o" * MAX_TAIL_CHARS,
                     stderr_tail="e" * MAX_TAIL_CHARS),
        spec_name="n" * MAX_SPEC_NAME_CHARS)
    assert verify_chain(record)
    assert document_ok(verify_document(record))


@pytest.mark.parametrize("record", [
    _record(spec_name="n" * (MAX_SPEC_NAME_CHARS + 1)),
    _record(check=_check(name="c" * (MAX_CHECK_NAME_CHARS + 1))),
    _record(check=_check(
        command="t" * (MAX_CHECK_COMMAND_CHARS + 1))),
    _record(check=_check(stdout_tail="o" * (MAX_TAIL_CHARS + 1))),
    _record(check=_check(stderr_tail="e" * (MAX_TAIL_CHARS + 1))),
    _record(check=_check(name=BIG, command=BIG,
                         stdout_tail="z" * 500_000)),
], ids=["spec_name", "check_name", "command", "stdout_tail",
        "stderr_tail", "original_probe"])
def test_record_above_a_ceiling_fails_verification(record):
    # build_record honestly sealed these — the structure mirror is
    # what rejects a shape no valid spec / real run can emit.
    assert not verify_chain(record)
    assert not document_ok(verify_document(record))


def test_rechained_over_limit_record_still_fails():
    record = _rechain(_record(
        check=_check(command="t" * (MAX_CHECK_COMMAND_CHARS + 1))))
    assert not verify_chain(record)


def test_verify_cli_and_webview_on_over_limit_record(tmp_path):
    record = _record(spec_name="n" * (MAX_SPEC_NAME_CHARS + 1))
    path = tmp_path / "record.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    assert cli.main(["verify", "--record", str(path)]) == 1
    assert "CHAIN INVALID" in render_document(record)
    ok_path = tmp_path / "ok.json"
    ok_path.write_text(json.dumps(_record()), encoding="utf-8")
    assert cli.main(["verify", "--record", str(ok_path)]) == 0


# -- end to end ----------------------------------------------------------

def test_real_run_seals_tails_within_the_ceiling(tmp_path):
    # A check printing far past the output cap seals a capped tail
    # (the sandbox truncates), and the record verifies.
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_raw(checks=[{
        "name": "noisy", "timeout_s": 60,
        "command": "python3 -c \"print('x' * 100000)\"",
    }])), encoding="utf-8")
    out = tmp_path / "e.json"
    rc = cli.main([
        "run", "--spec", str(spec_path),
        "--candidate", str(APP / "samples" / "passing"),
        "--mode", "judge", "--out", str(out),
    ])
    assert rc == 0
    record = json.loads(out.read_text(encoding="utf-8"))
    check = next(e for e in record["entries"] if e["kind"] == "check")
    assert len(check["stdout_tail"]) <= MAX_TAIL_CHARS
    assert verify_chain(record)


def _all_checks(obj):
    if isinstance(obj, dict):
        if obj.get("kind") == "check":
            yield obj
        if obj.get("kind") == "header":
            yield obj
        for value in obj.values():
            yield from _all_checks(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _all_checks(item)


def test_all_committed_fixtures_still_verify():
    fixtures = [
        APP / "evidence_self_check.json",
        APP / "samples" / "evidence_passing.json",
        APP / "samples" / "evidence_failing.json",
        APP / "samples" / "evidence_real_fail.json",
        APP / "samples" / "evidence_real_fail_v2.json",
        APP / "samples" / "evidence_real_repair.json",
        APP / "samples" / "evidence_real_repair_v2.json",
        APP / "samples" / "evidence_real_repair_v3.json",
        APP / "samples-js" / "evidence_passing.json",
        APP / "samples-js" / "evidence_failing.json",
        APP / "samples-js" / "evidence_real_repair.json",
        APP / "samples-js" / "evidence_real_repair_v3.json",
    ]
    for path in fixtures:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert document_ok(verify_document(data)), path
        # Every cap carries real headroom over the committed
        # fixtures (longest spec name 23, command 20, tail 1,863).
        for entry in _all_checks(data):
            if entry["kind"] == "header":
                assert len(entry["spec_name"]) * 4 < MAX_SPEC_NAME_CHARS, path
            else:
                assert len(entry["name"]) * 4 < MAX_CHECK_NAME_CHARS, path
                assert len(entry["command"]) * 4 < MAX_CHECK_COMMAND_CHARS, path
                assert len(entry["stdout_tail"]) * 2 < MAX_TAIL_CHARS, path
                assert len(entry["stderr_tail"]) * 2 < MAX_TAIL_CHARS, path
