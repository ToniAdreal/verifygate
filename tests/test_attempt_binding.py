"""Cross-attempt spec binding (v1.14.0).

A repair report's attempts are all verified against the one spec
the loop was given — the loop never changes the task. Verification
compared each attempt's header ``spec_name`` / ``mode`` against the
report's summary, but never compared the attempts' sealed
``spec_hash`` values with each other, so an attempt sealed under a
*different* spec (same name and mode, different brief or checks)
could be spliced into a report — re-sealed with the labelled public
dev key, the forgery route earlier rounds already assume — and
``verify`` reported the report INTACT once the wrapper verdict /
record-hash, figures, and narrative were patched up to match.
``attempt_binding_problems`` now requires one shared spec hash.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from verifygate.cli import main
from verifygate.evidence import build_record
from verifygate.spec import parse_spec
from verifygate.verify import (attempt_binding_problems, document_ok,
                               header_of, verify_document)
from verifygate.webview import render_document

ROOT = Path(__file__).resolve().parents[1]

REPORTS = [
    ROOT / "samples" / "evidence_real_repair.json",
    ROOT / "samples" / "evidence_real_repair_v2.json",
    ROOT / "samples-js" / "evidence_real_repair.json",
]


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _ok(data) -> bool:
    return document_ok(verify_document(data))


def _splice_divergent_spec(report: dict) -> dict:
    """Replace attempt 1's record with one sealed under a different
    spec: same name, same checks, different brief — so the spec
    *hash* differs while name/mode/checks all look familiar. The
    wrapper verdict and record_hash are updated to the forged
    record, exactly as a splicer with the public dev key would."""
    spec_json = _load(ROOT / "samples" / "spec.json")
    variant = dict(spec_json, brief="A different task, same name.")
    variant_hash = parse_spec(variant).hash
    original = report["attempts"][1]["evidence"]
    assert variant_hash != header_of(original)["spec_hash"]
    checks = [
        {k: v for k, v in e.items() if k not in ("kind", "hash")}
        for e in original["entries"] if e["kind"] == "check"
    ]
    usage = None
    for entry in original["entries"]:
        if entry["kind"] == "llm_usage":
            usage = entry["calls"]
    forged = build_record(
        spec_name=header_of(original)["spec_name"],
        spec_hash=variant_hash,
        candidate_tree_hash=header_of(original)["candidate_tree_hash"],
        mode=header_of(original)["mode"],
        check_results=checks,
        verdict=original["verdict"],
        diagnosis=None,
        replay_command=original["replay"],
        llm_usage=usage,
    )
    spliced = copy.deepcopy(report)
    spliced["attempts"][1]["evidence"] = forged
    spliced["attempts"][1]["record_hash"] = forged["record_hash"]
    spliced["attempts"][1]["verdict"] = forged["verdict"]
    return spliced


# --- the committed reports are bound to one spec -------------------

@pytest.mark.parametrize("path", REPORTS, ids=lambda p: p.parent.name)
def test_committed_reports_share_one_spec_hash(path):
    report = _load(path)
    assert attempt_binding_problems(report) == []
    hashes = {
        header_of(a["evidence"])["spec_hash"] for a in report["attempts"]
    }
    assert len(hashes) == 1
    assert _ok(report)


def test_binding_problems_on_malformed_input():
    assert attempt_binding_problems({}) != []
    assert attempt_binding_problems({"attempts": []}) != []
    assert attempt_binding_problems({"attempts": [{"evidence": {}}]}) != []


# --- the pre-fix probe now fails verification ----------------------

def test_divergent_spec_splice_fails_verification():
    spliced = _splice_divergent_spec(_load(REPORTS[0]))
    assert attempt_binding_problems(spliced) != []
    results = verify_document(spliced)
    # The forged record's own chain is valid — the failure is the
    # cross-attempt binding, surfaced as a report-summary mismatch.
    assert all(r["chain_ok"] for _, r in results)
    assert all(r["report_match"] is False for _, r in results)
    assert not document_ok(results)


def test_divergent_spec_splice_fails_cli(tmp_path, capsys):
    path = tmp_path / "spliced.json"
    path.write_text(json.dumps(
        _splice_divergent_spec(_load(REPORTS[0]))), encoding="utf-8")
    assert main(["verify", "--record", str(path)]) == 1
    out = capsys.readouterr().out
    assert "report summary: MISMATCH" in out
    assert "RECORD INTACT" not in out


def test_divergent_spec_splice_renders_mismatch_banner():
    html = render_document(_splice_divergent_spec(_load(REPORTS[0])))
    assert "REPORT SUMMARY MISMATCH" in html
    assert "not all sealed under the same spec" in html


def test_intact_report_renders_no_binding_banner():
    html = render_document(_load(REPORTS[0]))
    assert "REPORT SUMMARY MISMATCH" not in html


def test_js_report_splice_also_fails():
    # The splice is not Python-sample-specific: attempt records of
    # the JS report are bound to the JS spec hash, and a record
    # sealed under the Python spec (different task entirely) fails.
    report = _load(REPORTS[2])
    spliced = copy.deepcopy(report)
    foreign = _load(REPORTS[0])["attempts"][1]["evidence"]
    spliced["attempts"][1]["evidence"] = foreign
    spliced["attempts"][1]["record_hash"] = foreign["record_hash"]
    spliced["attempts"][1]["verdict"] = foreign["verdict"]
    assert attempt_binding_problems(spliced) != []
    assert not _ok(spliced)
