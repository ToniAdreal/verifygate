"""Check-result numeric bounds (v1.33.0).

Before v1.33.0 a check entry's numbers were the last unbounded
values in a record. ``duration_ms`` was checked only for being a
non-negative int, and ``exit_code`` only for being an int or None,
so — all reproduced against v1.32.0 — a record honestly sealed by
``build_record`` over:

* a duration of 10**12 ms (a check is killed at its timeout, whose
  ceiling is one hour),
* a timeout entry (``timed_out: true``) carrying exit code 0 (the
  sandbox kills a timed-out check before it can exit and seals
  ``exit_code: None``),
* a completed check (``timed_out: false``) carrying
  ``exit_code: None`` with a FAIL verdict, and
* an exit code of 999,999 (no POSIX exit status or signal number)

verified INTACT in every case.

Contract pinned here: ``evidence``'s structure check mirrors the
sandbox's emission contract — duration at most ``MAX_DURATION_MS``
(twice the longest legal timeout, in milliseconds), exit code
None exactly when the check timed out, otherwise a real int in
``MIN_EXIT_CODE..MAX_EXIT_CODE``. Shapes a real run emits — a
timeout FAIL, a signal-death FAIL, a duration or exit code exactly
at the ceiling — verify, as does every committed fixture.
"""

import json
from pathlib import Path

import pytest

from verifygate import cli
from verifygate.evidence import (
    MAX_DURATION_MS,
    MAX_EXIT_CODE,
    MIN_EXIT_CODE,
    build_record,
    verify_chain,
)
from verifygate.spec import MAX_TIMEOUT_S
from verifygate.verify import document_ok, verify_document
from verifygate.webview import render_document

APP = Path(__file__).resolve().parent.parent

PASS_CHECK = {
    "name": "c1", "command": "true", "exit_code": 0, "duration_ms": 1,
    "stdout_tail": "", "stderr_tail": "", "timed_out": False,
    "verdict": "PASS",
}
FAIL_CHECK = dict(PASS_CHECK, exit_code=1, verdict="FAIL")
DIAGNOSIS = {
    "explanation": "it broke", "references": [],
    "llm_adapter": "mock-deterministic", "reference_source": "mock",
    "reference_error": None,
}


def _record(check) -> dict:
    verdict = check["verdict"]
    return build_record(
        spec_name="demo", spec_hash="a" * 64,
        candidate_tree_hash="b" * 64, mode="judge",
        check_results=[check], verdict=verdict,
        diagnosis=DIAGNOSIS if verdict == "FAIL" else None,
        replay_command="python3 -m verifygate run --spec s "
                       "--candidate c --mode judge")


def test_ceilings_are_the_documented_generous_values():
    assert MAX_DURATION_MS == MAX_TIMEOUT_S * 2 * 1000
    assert (MIN_EXIT_CODE, MAX_EXIT_CODE) == (-255, 255)
    # Every committed fixture sits far below the duration ceiling
    # and uses exit codes 0/1 only.
    for path in (APP / "samples" / "evidence_passing.json",
                 APP / "evidence_self_check.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        for entry in data["entries"]:
            if entry.get("kind") == "check":
                assert entry["duration_ms"] <= MAX_DURATION_MS // 10
                assert entry["exit_code"] in (0, 1)


def test_duration_at_the_ceiling_verifies():
    check = dict(PASS_CHECK, duration_ms=MAX_DURATION_MS)
    assert verify_chain(_record(check))


@pytest.mark.parametrize("duration", [MAX_DURATION_MS + 1, 10**12])
def test_duration_above_the_ceiling_fails_verification(duration):
    check = dict(PASS_CHECK, duration_ms=duration)
    record = _record(check)
    # Honestly sealed by build_record: the structure mirror is what
    # rejects a shape no sandbox run can emit.
    assert not verify_chain(record)
    assert not document_ok(verify_document(record))


def test_timeout_with_an_exit_code_fails_verification():
    check = dict(FAIL_CHECK, timed_out=True, exit_code=0)
    assert not verify_chain(_record(check))
    check = dict(FAIL_CHECK, timed_out=True, exit_code=1)
    assert not verify_chain(_record(check))


def test_completed_check_without_an_exit_code_fails_verification():
    check = dict(FAIL_CHECK, exit_code=None)
    assert not verify_chain(_record(check))


def test_timeout_without_an_exit_code_verifies():
    check = dict(FAIL_CHECK, timed_out=True, exit_code=None)
    assert verify_chain(_record(check))


@pytest.mark.parametrize("code", [MAX_EXIT_CODE, MIN_EXIT_CODE, -9, 126])
def test_exit_codes_within_the_range_verify(code):
    check = dict(FAIL_CHECK, exit_code=code)
    assert verify_chain(_record(check))


@pytest.mark.parametrize("code", [MAX_EXIT_CODE + 1, MIN_EXIT_CODE - 1,
                                  999_999, -999_999])
def test_exit_codes_outside_the_range_fail_verification(code):
    check = dict(FAIL_CHECK, exit_code=code)
    record = _record(check)
    assert not verify_chain(record)
    assert not document_ok(verify_document(record))


def test_verify_cli_exits_1_on_out_of_bounds_record(tmp_path):
    path = tmp_path / "record.json"
    path.write_text(json.dumps(_record(
        dict(PASS_CHECK, duration_ms=MAX_DURATION_MS + 1))),
        encoding="utf-8")
    assert cli.main(["verify", "--record", str(path)]) == 1
    ok_path = tmp_path / "ok.json"
    ok_path.write_text(json.dumps(_record(PASS_CHECK)),
                       encoding="utf-8")
    assert cli.main(["verify", "--record", str(ok_path)]) == 0


def test_webview_flags_out_of_bounds_record():
    html = render_document(_record(dict(FAIL_CHECK, exit_code=999_999)))
    assert "CHAIN INVALID" in html
    intact = render_document(_record(FAIL_CHECK))
    assert "CHAIN INVALID" not in intact


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
