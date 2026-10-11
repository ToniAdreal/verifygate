"""Check-count bound: a spec's checks are a bounded dimension too.

Before v1.24.0 every other dimension of a run was capped — a single
check's timeout (``MAX_TIMEOUT_S``), the repair loop's rounds
(``MAX_ROUNDS``), a candidate's files and bytes
(``MAX_CANDIDATE_FILES`` / ``MAX_CANDIDATE_BYTES``) — but the number
of checks in a spec was not. Checks run sequentially, each entitled
to its full timeout, and every check's result is sealed into the
evidence record, so a 5,000-check probe spec was accepted with an
aggregate timeout budget of 18,000,000 seconds (~208 days), and a
record sealed over 5,000 checks verified INTACT.

Contract pinned here:
* ``parse_spec`` rejects a spec carrying more than ``MAX_CHECKS``
  checks (a ``SpecError``, so every CLI entry point reports it as a
  one-line usage error, exit 2, before any check runs);
* exactly ``MAX_CHECKS`` checks is accepted — the ceiling is
  generous (the bundled samples carry 2, the dogfood spec 1);
* ``evidence``'s structure check mirrors the cap, so a record
  carrying more check entries than any valid spec can produce
  fails verification however its chain was sealed, while every
  committed fixture still verifies.
"""

import json
from pathlib import Path

import pytest

from verifygate import cli
from verifygate.evidence import build_record, verify_chain
from verifygate.spec import MAX_CHECKS, SpecError, parse_spec
from verifygate.verify import document_ok, verify_document
from verifygate.webview import render_document

APP = Path(__file__).resolve().parent.parent


def _raw_spec(n_checks: int) -> dict:
    return {
        "name": "many-checks",
        "brief": "A spec with a lot of gate commands.",
        "checks": [
            {"name": f"c{i}", "command": "true", "timeout_s": 60}
            for i in range(n_checks)
        ],
    }


def _record(n_checks: int) -> dict:
    checks = [{
        "name": f"c{i}", "command": "true", "exit_code": 0,
        "duration_ms": 1, "stdout_tail": "", "stderr_tail": "",
        "timed_out": False, "verdict": "PASS",
    } for i in range(n_checks)]
    return build_record(
        spec_name="many-checks", spec_hash="a" * 64,
        candidate_tree_hash="b" * 64, mode="judge",
        check_results=checks, verdict="PASS", diagnosis=None,
        replay_command="python3 -m verifygate run --spec s.json "
                       "--candidate cand --mode judge")


def test_ceiling_is_the_documented_generous_value():
    assert MAX_CHECKS == 100
    # The bundled specs sit far below the ceiling.
    for spec_path in (APP / "samples" / "spec.json",
                      APP / "samples-js" / "spec.json",
                      APP / "dogfood_spec.json"):
        raw = json.loads(spec_path.read_text(encoding="utf-8"))
        assert len(raw["checks"]) <= MAX_CHECKS // 50


def test_parse_accepts_exactly_the_ceiling():
    spec = parse_spec(_raw_spec(MAX_CHECKS))
    assert len(spec.checks) == MAX_CHECKS


@pytest.mark.parametrize("n", [MAX_CHECKS + 1, 5000])
def test_parse_rejects_above_the_ceiling(n):
    with pytest.raises(SpecError, match="at most"):
        parse_spec(_raw_spec(n))


def test_cli_run_rejects_over_ceiling_spec(tmp_path):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_raw_spec(MAX_CHECKS + 1)),
                         encoding="utf-8")
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
    spec_path.write_text(json.dumps(_raw_spec(MAX_CHECKS + 1)),
                         encoding="utf-8")
    out = tmp_path / "r.json"
    rc = cli.main([
        "repair", "--spec", str(spec_path),
        "--candidate", str(APP / "samples" / "failing"),
        "--mode", "judge", "--out", str(out),
    ])
    assert rc == 2
    assert not out.exists()


def test_record_at_the_ceiling_verifies():
    record = _record(MAX_CHECKS)
    assert verify_chain(record)
    assert document_ok(verify_document(record))


def test_record_above_the_ceiling_fails_verification():
    record = _record(MAX_CHECKS + 1)
    # The chain itself was honestly sealed by build_record — the
    # structure mirror is what rejects a shape no valid spec emits.
    assert not verify_chain(record)
    assert not document_ok(verify_document(record))


def test_verify_cli_exits_1_on_over_ceiling_record(tmp_path):
    path = tmp_path / "record.json"
    path.write_text(json.dumps(_record(MAX_CHECKS + 1)),
                    encoding="utf-8")
    assert cli.main(["verify", "--record", str(path)]) == 1
    ok_path = tmp_path / "ok.json"
    ok_path.write_text(json.dumps(_record(MAX_CHECKS)),
                       encoding="utf-8")
    assert cli.main(["verify", "--record", str(ok_path)]) == 0


def test_webview_flags_over_ceiling_record():
    html = render_document(_record(MAX_CHECKS + 1))
    assert "CHAIN INVALID" in html
    intact = render_document(_record(2))
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
