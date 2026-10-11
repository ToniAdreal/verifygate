"""Chain-integrity hardening (v1.5.0): kinds and report summaries.

Three tamper paths that chain v1 + summary-trusting consumers left
open, each reproduced before the fix:

  1. an entry's ``kind`` was not part of its hashed payload, so a
     FAIL check entry could be relabelled "diagnosis" — consumers
     filter by kind, so the check vanished from the check list while
     ``verify_chain`` still returned True;
  2. a repair report's ``final_verdict`` is not inside any chain, and
     ``verify`` never compared it with the sealed attempts, so it
     could be flipped while every chain still verified;
  3. the web view rendered that stated ``final_verdict`` as the
     report's verdict badge.

Chain v2 hashes the kind with the content; verification of *both*
versions also enforces the record structure (kinds, order, required
fields), which closes the relabel hole for legacy records without
invalidating any untampered one. ``verify`` cross-checks report
summaries against the sealed records, and the web view derives its
verdicts from the sealed records, flagging any stated mismatch.
"""

import copy
import json
from pathlib import Path

from verifygate.cli import main
from verifygate.evidence import CHAIN_VERSION, build_record, verify_chain
from verifygate.verify import document_ok, verify_document
from verifygate.webview import render_document

ROOT = Path(__file__).resolve().parent.parent
PASSING = ROOT / "samples" / "evidence_passing.json"
FAILING = ROOT / "samples" / "evidence_failing.json"
REAL_REPAIR = ROOT / "samples" / "evidence_real_repair.json"
JS_REAL_REPAIR = ROOT / "samples-js" / "evidence_real_repair.json"
REAL_FAIL = ROOT / "samples" / "evidence_real_fail.json"

CHECK = {
    "name": "unit_tests",
    "command": "python3 -m pytest -q",
    "exit_code": 1,
    "duration_ms": 42,
    "stdout_tail": "1 failed",
    "stderr_tail": "",
    "timed_out": False,
    "verdict": "FAIL",
}


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(tmp_path, data, name="record.json"):
    out = tmp_path / name
    out.write_text(json.dumps(data), encoding="utf-8")
    return str(out)


def _v2_record():
    return build_record(
        spec_name="demo",
        spec_hash="a" * 64,
        candidate_tree_hash="b" * 64,
        mode="judge",
        check_results=[CHECK],
        verdict="FAIL",
        diagnosis={"explanation": "it broke", "references": [],
                   "llm_adapter": "mock", "reference_source": "mock",
                   "reference_error": None},
        replay_command="python3 -m verifygate run --spec s --candidate c --mode judge",
        llm_usage=[{"adapter": "x", "model": "m", "purpose": "explain_failure",
                    "usage": {"total_tokens": 7}}],
    )


# -- chain v2/v3 ------------------------------------------------------

def test_new_records_are_chain_v3_and_verify():
    record = _v2_record()
    assert record["chain_version"] == CHAIN_VERSION == 3
    assert verify_chain(record) is True


def test_v2_kind_relabel_breaks_chain():
    for target in ("diagnosis", "llm_usage", "header", "bogus"):
        record = _v2_record()
        check = next(e for e in record["entries"] if e["kind"] == "check")
        check["kind"] = target
        assert verify_chain(record) is False, target


def test_v2_entry_kind_swap_breaks_chain():
    record = _v2_record()
    diag = next(e for e in record["entries"] if e["kind"] == "diagnosis")
    diag["kind"] = "check"
    assert verify_chain(record) is False


# -- legacy chain v1: untampered verifies, relabelled fails -----------

def test_legacy_records_still_verify():
    for path in (PASSING, FAILING, REAL_FAIL):
        record = _load(path)
        assert record.get("chain_version") is None  # legacy fixture
        assert verify_chain(record) is True, path
    for path in (REAL_REPAIR, JS_REAL_REPAIR):
        for attempt in _load(path)["attempts"]:
            assert verify_chain(attempt["evidence"]) is True, path


def test_legacy_kind_relabel_fails_verification():
    # The original hole: relabel a FAIL check as a diagnosis and the
    # v1 chain recomputed True. Structure validation must catch it.
    for target in ("diagnosis", "llm_usage", "header", "bogus"):
        record = _load(FAILING)
        check = next(e for e in record["entries"] if e["kind"] == "check")
        check["kind"] = target
        assert verify_chain(record) is False, target


def test_legacy_diagnosis_relabelled_as_check_fails():
    record = _load(FAILING)
    diag = next(e for e in record["entries"] if e["kind"] == "diagnosis")
    diag["kind"] = "check"  # lacks name/command/verdict/exit_code
    assert verify_chain(record) is False


def test_legacy_header_relabel_fails():
    record = _load(PASSING)
    record["entries"][0]["kind"] = "check"
    assert verify_chain(record) is False


def test_unknown_chain_version_fails():
    record = _v2_record()
    record["chain_version"] = 99
    assert verify_chain(record) is False


def test_duplicate_diagnosis_fails_structure():
    record = _load(FAILING)
    diag = next(e for e in record["entries"] if e["kind"] == "diagnosis")
    record["entries"].append(copy.deepcopy(diag))
    assert verify_chain(record) is False


# -- repair report summary cross-checks --------------------------------

def test_report_summary_tamper_fails_verify_document():
    report = _load(REAL_REPAIR)
    report["final_verdict"] = "FAIL"  # sealed attempts say PASS
    results = verify_document(report)
    assert all(r["report_match"] is False for _, r in results)
    assert document_ok(results) is False


def test_report_summary_intact_matches():
    results = verify_document(_load(REAL_REPAIR))
    assert all(r["report_match"] is True for _, r in results)
    assert document_ok(results) is True


def test_attempt_wrapper_tamper_fails():
    for field, value in (("verdict", "FAIL"), ("record_hash", "0" * 64)):
        report = _load(REAL_REPAIR)
        report["attempts"][1][field] = value
        assert document_ok(verify_document(report)) is False, field


def test_report_spec_name_tamper_fails():
    report = _load(REAL_REPAIR)
    report["spec_name"] = "some-other-spec"
    assert document_ok(verify_document(report)) is False


def test_single_record_has_no_report_match():
    results = verify_document(_load(PASSING))
    assert results[0][1]["report_match"] is None
    assert document_ok(results) is True


def test_cli_verify_shows_report_summary(tmp_path, capsys):
    assert main(["verify", "--record", str(REAL_REPAIR)]) == 0
    assert "report summary: match" in capsys.readouterr().out
    report = _load(REAL_REPAIR)
    report["final_verdict"] = "FAIL"
    assert main(["verify", "--record", _write(tmp_path, report)]) == 1
    assert "report summary: MISMATCH" in capsys.readouterr().out


# -- web view derives verdicts from the sealed records ------------------

def test_webview_flags_tampered_report_summary():
    report = _load(REAL_REPAIR)
    report["final_verdict"] = "FAIL"
    html = render_document(report)
    assert "REPORT SUMMARY MISMATCH" in html
    # The badge is the sealed final attempt's PASS, not the stated FAIL.
    assert "final verdict:" in html and ">PASS</span>" in html


def test_webview_intact_report_has_no_mismatch_banner():
    html = render_document(_load(REAL_REPAIR))
    assert "SUMMARY MISMATCH" not in html
    assert "Hash chain verified" in html


def test_webview_relabelled_legacy_record_is_chain_invalid():
    record = _load(FAILING)
    check = next(e for e in record["entries"] if e["kind"] == "check")
    check["kind"] = "diagnosis"
    html = render_document(record)
    assert "CHAIN INVALID" in html
