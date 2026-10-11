"""Spec-validation hardening (v1.7.0): malformed specs fail as
SpecError before any check runs — never as a vacuous PASS, a silently
reinterpreted timeout, or an unhandled traceback.

Each case was reproduced against v1.6.0 before the fix:

* a whitespace-only ``command`` was accepted and *passed* — the shell
  exits 0 on empty input, so the record sealed a PASS for a check
  that ran nothing (a verdict-integrity hole, not just a type nit);
* a non-string ``command`` (JSON ``123``) passed the old truthiness
  test and crashed ``verifygate run`` with a ``TypeError`` traceback
  and exit 1, violating the CLI contract (usage errors exit 2);
* ``timeout_s: true`` was accepted as a 1-second timeout, because
  ``bool`` is an ``int`` subclass in Python;
* a non-string check ``name`` (``42``) and a non-string ``brief``
  were accepted and sealed verbatim into the evidence record — which
  then verified INTACT, because structure validation only required
  the fields to be truthy.
"""

import json
from pathlib import Path

import pytest

from verifygate.cli import main
from verifygate.evidence import build_record, verify_chain
from verifygate.spec import MAX_TIMEOUT_S, SpecError, parse_spec

CHECK = {"name": "tests", "command": "python3 -m pytest -q", "timeout_s": 30}


def _spec(**overrides):
    raw = {"name": "demo", "brief": "do the thing", "checks": [dict(CHECK)]}
    raw.update(overrides)
    return raw


def _spec_with_check(**check_overrides):
    check = dict(CHECK)
    check.update(check_overrides)
    return _spec(checks=[check])


# -- names, brief, commands: types and emptiness ----------------------

@pytest.mark.parametrize("name", ["", "   ", 123, None, ["x"]])
def test_spec_name_must_be_non_empty_string(name):
    with pytest.raises(SpecError):
        parse_spec(_spec(name=name))


@pytest.mark.parametrize("brief", [123, None, ["x"], {"x": 1}])
def test_brief_must_be_a_string_when_present(brief):
    with pytest.raises(SpecError):
        parse_spec(_spec(brief=brief))


def test_brief_may_be_omitted():
    raw = _spec()
    del raw["brief"]
    assert parse_spec(raw).brief == ""


@pytest.mark.parametrize("check_name", ["", "  ", 42, None])
def test_check_name_must_be_non_empty_string(check_name):
    with pytest.raises(SpecError):
        parse_spec(_spec_with_check(name=check_name))


@pytest.mark.parametrize("command", ["", "   ", "\t\n", 123, None, ["true"]])
def test_check_command_must_be_non_empty_string(command):
    with pytest.raises(SpecError):
        parse_spec(_spec_with_check(command=command))


def test_check_entry_must_be_an_object():
    with pytest.raises(SpecError):
        parse_spec(_spec(checks=["not-an-object"]))


def test_command_with_leading_whitespace_but_real_content_is_valid():
    spec = parse_spec(_spec_with_check(command="  python3 -m pytest -q"))
    assert spec.checks[0].command == "  python3 -m pytest -q"


# -- timeout_s: a real int within bounds -------------------------------

@pytest.mark.parametrize("timeout", [True, False, 1.5, "60", None, 0, -5,
                                     MAX_TIMEOUT_S + 1])
def test_timeout_must_be_an_int_within_bounds(timeout):
    with pytest.raises(SpecError):
        parse_spec(_spec_with_check(timeout_s=timeout))


@pytest.mark.parametrize("timeout", [1, 60, MAX_TIMEOUT_S])
def test_timeout_bounds_are_accepted(timeout):
    spec = parse_spec(_spec_with_check(timeout_s=timeout))
    assert spec.checks[0].timeout_s == timeout


# -- the CLI contract: a malformed spec is exit 2, never a traceback ---

def test_cli_run_with_non_string_command_exits_2(tmp_path):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_spec_with_check(command=123)),
                         encoding="utf-8")
    candidate = tmp_path / "cand"
    candidate.mkdir()
    (candidate / "a.py").write_text("x = 1\n", encoding="utf-8")
    rc = main(["run", "--spec", str(spec_path), "--candidate", str(candidate),
               "--mode", "judge", "--out", str(tmp_path / "e.json")])
    assert rc == 2


def test_cli_run_with_whitespace_command_exits_2(tmp_path):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_spec_with_check(command="   ")),
                         encoding="utf-8")
    candidate = tmp_path / "cand"
    candidate.mkdir()
    (candidate / "a.py").write_text("x = 1\n", encoding="utf-8")
    rc = main(["run", "--spec", str(spec_path), "--candidate", str(candidate),
               "--mode", "judge", "--out", str(tmp_path / "e.json")])
    assert rc == 2


# -- record structure mirrors the parser -------------------------------

def _record_with_check(check):
    # The record's verdict must be the one the checks imply (structure
    # validation recomputes it since v1.12.0); deriving it here keeps
    # these tests about the check field under test, not the verdict.
    verdict = check.get("verdict") if check.get("verdict") in (
        "PASS", "FAIL") else "FAIL"
    return build_record(
        spec_name="demo", spec_hash="a" * 64, candidate_tree_hash="b" * 64,
        mode="judge", check_results=[check], verdict=verdict,
        diagnosis=None, replay_command="python3 -m verifygate run --spec s --candidate c --mode judge")


def test_record_with_non_string_check_name_no_longer_verifies():
    record = _record_with_check({
        "name": 42, "command": "true", "exit_code": 1, "duration_ms": 1,
        "stdout_tail": "", "stderr_tail": "", "timed_out": False,
        "verdict": "FAIL"})
    assert verify_chain(record) is False


def test_record_with_whitespace_check_command_no_longer_verifies():
    record = _record_with_check({
        "name": "c1", "command": "   ", "exit_code": 0, "duration_ms": 1,
        "stdout_tail": "", "stderr_tail": "", "timed_out": False,
        "verdict": "PASS"})
    assert verify_chain(record) is False


def test_well_formed_record_still_verifies():
    record = _record_with_check({
        "name": "c1", "command": "true", "exit_code": 0, "duration_ms": 1,
        "stdout_tail": "", "stderr_tail": "", "timed_out": False,
        "verdict": "PASS"})
    assert verify_chain(record) is True


def test_committed_fixtures_still_verify():
    # Every untampered committed record (all chain versions) must keep
    # verifying under the tightened structure rules.
    from verifygate.verify import collect_records, verify_document
    root = Path(__file__).resolve().parent.parent
    fixtures = [
        root / "samples" / "evidence_passing.json",
        root / "samples" / "evidence_failing.json",
        root / "samples" / "evidence_real_fail.json",
        root / "samples" / "evidence_real_repair.json",
        root / "samples" / "evidence_real_repair_v2.json",
        root / "samples-js" / "evidence_passing.json",
        root / "samples-js" / "evidence_failing.json",
        root / "samples-js" / "evidence_real_repair.json",
        root / "evidence_self_check.json",
    ]
    for path in fixtures:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert collect_records(data), path
        results = verify_document(data)
        assert all(r["chain_ok"] for _, r in results), path
