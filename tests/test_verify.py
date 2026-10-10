"""Standalone record verification: `verifygate verify`.

The third-party check on an evidence record: recompute the hash chain
and signature without re-running anything, and optionally bind the
record to the spec file and candidate directory on disk. Tampering
with a verdict, a check result, or a sealed token-usage figure must
fail; so must presenting the wrong candidate or spec.
"""

import copy
import json
from pathlib import Path

import pytest

from verifygate.cli import main
from verifygate.verify import collect_records, verify_document

ROOT = Path(__file__).resolve().parent.parent
PASSING = ROOT / "samples" / "evidence_passing.json"
REAL_REPAIR = ROOT / "samples" / "evidence_real_repair.json"
REAL_FAIL = ROOT / "samples" / "evidence_real_fail.json"
SPEC = ROOT / "samples" / "spec.json"
CAND_PASSING = ROOT / "samples" / "passing"
CAND_FAILING = ROOT / "samples" / "failing"


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(tmp_path, data, name="record.json"):
    out = tmp_path / name
    out.write_text(json.dumps(data), encoding="utf-8")
    return str(out)


def _run_verify(record, *extra):
    return main(["verify", "--record", str(record), *extra])


def test_single_record_verifies(capsys):
    assert _run_verify(PASSING) == 0
    out = capsys.readouterr().out
    assert "chain OK" in out
    assert "RECORD INTACT: 1 record(s) verified" in out


def test_tampered_verdict_fails(capsys, tmp_path):
    data = _load(PASSING)
    data["verdict"] = "FAIL"  # the verdict a cheater would flip
    assert _run_verify(_write(tmp_path, data)) == 1
    assert "CHAIN INVALID" in capsys.readouterr().out


def test_tampered_sealed_usage_fails(tmp_path):
    data = _load(REAL_FAIL)
    for entry in data["entries"]:
        if entry["kind"] == "llm_usage":
            entry["total_tokens"] = 1  # forge a cheaper token bill
    assert _run_verify(_write(tmp_path, data)) == 1


def test_repair_report_verifies_every_attempt(capsys):
    assert _run_verify(REAL_REPAIR) == 0
    out = capsys.readouterr().out
    assert "attempt 0" in out and "attempt 1" in out
    assert out.count("chain OK") == 2
    assert "RECORD INTACT: 2 record(s) verified" in out


def test_repair_report_with_tampered_attempt_fails(tmp_path):
    data = _load(REAL_REPAIR)
    check = next(e for e in data["attempts"][0]["evidence"]["entries"]
                 if e["kind"] == "check")
    assert check["exit_code"] == 1  # attempt 0 really failed
    check["exit_code"] = 0  # pretend the failing check passed
    assert _run_verify(_write(tmp_path, data)) == 1


def test_candidate_binding_match_and_mismatch(capsys):
    assert _run_verify(PASSING, "--candidate", str(CAND_PASSING)) == 0
    assert "candidate: match" in capsys.readouterr().out
    assert _run_verify(PASSING, "--candidate", str(CAND_FAILING)) == 1
    assert "candidate: MISMATCH" in capsys.readouterr().out


def test_spec_binding_match_and_mismatch(tmp_path):
    assert _run_verify(PASSING, "--spec", str(SPEC)) == 0
    other_spec = _write(tmp_path, {
        "name": "other", "brief": "x",
        "checks": [{"name": "c", "command": "true", "timeout_s": 5}],
    }, name="other-spec.json")
    assert _run_verify(PASSING, "--spec", other_spec) == 1


def test_repair_candidate_binding_applies_to_attempt_zero(capsys):
    # Attempt 0 is the submitted (failing) candidate; attempt 1 is a
    # patched copy that no longer exists on disk -> reported as not
    # bound, and the report still verifies.
    rc = _run_verify(REAL_REPAIR, "--candidate", str(CAND_FAILING),
                     "--spec", str(SPEC))
    assert rc == 0
    out = capsys.readouterr().out
    assert "candidate: match" in out
    assert "candidate: not bound (patched copy)" in out
    assert out.count("spec: match") == 2


def test_repair_candidate_binding_wrong_candidate_fails():
    assert _run_verify(REAL_REPAIR, "--candidate", str(CAND_PASSING)) == 1


def test_usage_errors_exit_2(tmp_path, capsys):
    assert _run_verify(tmp_path / "missing.json") == 2
    bad = _write(tmp_path, {"tool": "something-else"}, name="bad.json")
    assert _run_verify(bad) == 2
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not json", encoding="utf-8")
    assert _run_verify(malformed) == 2
    assert "record error" in capsys.readouterr().err


def test_collect_records_rejects_non_verifygate():
    with pytest.raises(ValueError):
        collect_records({"tool": "nope", "entries": []})
    with pytest.raises(ValueError):
        collect_records({"tool": "verifygate"})


def test_verify_document_matches_cli_semantics():
    data = _load(REAL_REPAIR)
    results = verify_document(data, candidate_dir=CAND_FAILING)
    assert [label for label, _ in results] == ["attempt 0", "attempt 1"]
    assert all(r["chain_ok"] for _, r in results)
    assert results[0][1]["candidate_match"] is True
    assert results[1][1]["candidate_match"] is None  # patched copy: unbound


def test_deepcopy_tamper_of_diagnosis_fails(tmp_path):
    data = copy.deepcopy(_load(REAL_FAIL))
    diag = next(e for e in data["entries"] if e["kind"] == "diagnosis")
    diag["explanation"] = "nothing was wrong"
    assert _run_verify(_write(tmp_path, data)) == 1
