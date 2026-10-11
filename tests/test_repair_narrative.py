"""Repair-report narrative consistency (v1.13.0).

A repair report's unchained narrative — why the loop stopped, the
round budget, which patch produced each attempt, the preserved
rejection, the credited adapter — was taken at face value by
``verifygate verify`` and printed by the web view: every probe in
this file's ``TAMPERS`` list verified INTACT against v1.12.0. The
narrative is deterministic given the sealed attempts, so verification
now re-derives it (``narrative_problems``) and the web view shows the
derived stopped reason next to a SUMMARY MISMATCH banner.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from verifygate.cli import main
from verifygate.verify import (derived_stopped_reason, document_ok,
                               narrative_problems, verify_document)
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


# --- the committed reports satisfy the narrative rules -------------

@pytest.mark.parametrize("path", REPORTS, ids=lambda p: p.parent.name)
def test_committed_reports_have_consistent_narratives(path):
    report = _load(path)
    assert narrative_problems(report) == []
    assert _ok(report)


def test_legacy_workspace_style_report_is_consistent():
    # The v0.2.0-era Judge Mode sample shape (no llm_usage_total /
    # unsealed fields) conforms: omission is legacy, not tamper.
    report = _load(REPORTS[0])
    report.pop("llm_usage_total")
    report.pop("unsealed_llm_usage")
    assert narrative_problems(report) == []
    assert _ok(report)


def test_derived_stopped_reason_values():
    assert derived_stopped_reason(_load(REPORTS[0])) == "passed"
    assert derived_stopped_reason(_load(REPORTS[1])) == "passed"
    assert derived_stopped_reason({"attempts": []}) is None
    assert derived_stopped_reason({}) is None


# --- each pre-fix probe now fails verification ----------------------

def _tamper_stopped_reason(report):
    report["stopped_reason"] = "max_rounds"  # it passed in 1 round


def _tamper_max_rounds_below_rounds_used(report):
    report["max_rounds"] = 0  # but 1 round was used


def _tamper_changed_files_type(report):
    report["attempts"][1]["patch_from_previous"]["changed_files"] = "evil.py"


def _tamper_report_adapter(report):
    report["llm_adapter"] = "some-other-llm"


def _tamper_patch_round(report):
    report["attempts"][1]["patch_from_previous"]["round"] = 7


def _tamper_patch_applied(report):
    report["attempts"][1]["patch_from_previous"]["applied"] = False


def _tamper_attempt_zero_patch(report):
    report["attempts"][0]["patch_from_previous"] = {
        "round": 0, "llm_adapter": report["llm_adapter"], "applied": True,
        "changed_files": ["x.py"], "error": None, "llm_usage": [],
        "patch_text": "diff --git a/x b/x",
    }


def _tamper_invented_rejection(report):
    report["failed_patch"] = {
        "round": 1, "llm_adapter": report["llm_adapter"], "applied": False,
        "changed_files": [], "error": "fake rejection", "llm_usage": [],
        "patch_text": "fake",
    }
    report["last_patch_error"] = "fake rejection"


def _tamper_last_patch_error(report):
    report["last_patch_error"] = "an error that never happened"


TAMPERS = [
    _tamper_stopped_reason,
    _tamper_max_rounds_below_rounds_used,
    _tamper_changed_files_type,
    _tamper_report_adapter,
    _tamper_patch_round,
    _tamper_patch_applied,
    _tamper_attempt_zero_patch,
    _tamper_invented_rejection,
    _tamper_last_patch_error,
]


@pytest.mark.parametrize("tamper", TAMPERS, ids=lambda f: f.__name__)
def test_narrative_tampers_fail_verification(tamper):
    report = _load(REPORTS[0])
    tamper(report)
    assert narrative_problems(report) != []
    assert not _ok(report)


def test_joint_renumbering_past_max_rounds_fails():
    # Renumbering the attempt, its patch linkage, and rounds_used
    # consistently still collides with the sealed budget (max 3 < 5).
    report = _load(REPORTS[0])
    report["attempts"][1]["round"] = 5
    report["attempts"][1]["patch_from_previous"]["round"] = 5
    report["rounds_used"] = 5
    assert narrative_problems(report) != []
    assert not _ok(report)


def test_non_increasing_attempt_rounds_fail():
    report = _load(REPORTS[1])  # attempts at rounds 0 and 2
    report["attempts"][1]["round"] = 0
    report["attempts"][1]["patch_from_previous"]["round"] = 0
    assert narrative_problems(report) != []
    assert not _ok(report)


# --- the v2 report's real rejection stays legitimate ---------------

def test_preserved_rejection_before_success_is_consistent():
    report = _load(REPORTS[1])  # round 1 rejected, round 2 applied
    assert report["failed_patch"]["round"] == 1
    assert report["last_patch_error"] == report["failed_patch"]["error"]
    assert narrative_problems(report) == []
    # Claiming the same rejection happened on the applied round is not.
    report["failed_patch"]["round"] = 2
    assert narrative_problems(report) != []
    assert not _ok(report)


# --- derived stopped reasons for the loop's other exits ------------

def _fail_final(report):
    """Turn a PASS-final report into its FAIL-only first attempt."""
    report["attempts"] = report["attempts"][:1]
    report["final_verdict"] = "FAIL"
    report["rounds_used"] = 0
    report["llm_usage_total"] = {"llm_calls": 1, "total_tokens": 1533}
    return report


def test_derived_stop_max_rounds_when_budget_spent_on_applied_patch():
    report = _load(REPORTS[0])
    # One attempt only, no rounds used, final FAIL: the loop's
    # initial stopped_reason for a FAIL is the budget outcome.
    report = _fail_final(report)
    report["stopped_reason"] = "max_rounds"
    report["max_rounds"] = 0
    assert derived_stopped_reason(report) == "max_rounds"
    assert narrative_problems(report) == []
    report["stopped_reason"] = "passed"
    assert narrative_problems(report) != []


def test_derived_stop_patch_not_applicable_on_final_rejection():
    report = _fail_final(_load(REPORTS[0]))
    report["max_rounds"] = 3
    report["rounds_used"] = 2
    report["failed_patch"] = {
        "round": 2, "llm_adapter": report["llm_adapter"], "applied": False,
        "changed_files": [], "error": "does not apply", "llm_usage": [],
        "patch_text": "junk",
    }
    report["last_patch_error"] = "does not apply"
    report["stopped_reason"] = "patch_not_applicable"
    assert derived_stopped_reason(report) == "patch_not_applicable"
    assert narrative_problems(report) == []
    report["stopped_reason"] = "max_rounds"
    assert narrative_problems(report) != []
    assert not _ok(report)


# --- CLI + web view surface the same verdict ------------------------

def test_cli_verify_rejects_narrative_tamper(tmp_path, capsys):
    report = _load(REPORTS[0])
    report["stopped_reason"] = "max_rounds"
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    assert main(["verify", "--record", str(path)]) == 1
    out = capsys.readouterr().out
    assert "report summary: MISMATCH" in out


def test_webview_shows_derived_stop_and_flags_tamper():
    report = _load(REPORTS[0])
    report["stopped_reason"] = "max_rounds"
    page = render_document(report)
    assert "REPORT SUMMARY MISMATCH" in page
    assert "stopped: passed" in page  # derived, not the rewritten claim
    clean = render_document(_load(REPORTS[0]))
    assert "REPORT SUMMARY MISMATCH" not in clean
    assert "stopped: passed" in clean
