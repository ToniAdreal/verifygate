"""Report-level bounds: unsealed usage, candidate dirs, name fields.

Before v1.31.0 the narrative check totalled a repair report's
``unsealed_llm_usage`` list but never bounded it, and never judged
an attempt's ``candidate_dir`` at all: a coordinated edit
(inflating the list and the stated total together) carrying 5,000
calls with 500,000-character model strings still verified INTACT,
as did a 500,000-character candidate directory (all reproduced).

Contract pinned here:
* the unsealed list must pass the same sealed-call ceilings as a
  patch linkage's usage snapshot (at most
  ``MAX_CALLS_PER_USAGE_ENTRY`` calls, per-field string caps);
* every attempt's ``candidate_dir``, when present, must be a
  string of at most ``MAX_CANDIDATE_DIR_CHARS`` characters;
* the report's ``spec_name`` / ``llm_adapter`` are capped at the
  sealed ceilings as defence in depth;
* a report exactly at every new ceiling still verifies, and every
  committed report fixture verifies untampered.
"""

import json
from pathlib import Path

from verifygate import cli
from verifygate.evidence import (
    MAX_ADAPTER_NAME_CHARS,
    MAX_CALLS_PER_USAGE_ENTRY,
)
from verifygate.spec import MAX_SPEC_NAME_CHARS
from verifygate.verify import (
    MAX_CANDIDATE_DIR_CHARS,
    derived_usage_total,
    document_ok,
    narrative_problems,
    verify_document,
)
from verifygate.webview import render_document

APP = Path(__file__).resolve().parent.parent
REPORT = APP / "samples" / "evidence_real_repair.json"
REPORT_V2 = APP / "samples" / "evidence_real_repair_v2.json"
REPORT_V3 = APP / "samples" / "evidence_real_repair_v3.json"
REPORT_JS = APP / "samples-js" / "evidence_real_repair.json"
REPORT_JS_V3 = APP / "samples-js" / "evidence_real_repair_v3.json"
ALL_REPORTS = [REPORT, REPORT_V2, REPORT_V3, REPORT_JS, REPORT_JS_V3]


def _report(path=REPORT) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _call(tokens=0) -> dict:
    return {"adapter": "nebius-token-factory", "model": "m",
            "purpose": "generate_patch", "usage": {"total_tokens": tokens}}


# -- the reproduced inflations now fail -------------------------------------


def test_inflated_unsealed_usage_strings_fail():
    report = _report()
    report["unsealed_llm_usage"] = [
        {"adapter": "x" * 500_000, "model": "y" * 500_000,
         "purpose": "generate_patch", "usage": {"total_tokens": 0}}] * 5
    # Coordinated edit: the stated total is updated to match, so the
    # figures cross-check alone cannot catch this — the bound must.
    report["llm_usage_total"] = derived_usage_total(report)
    assert "unsealed_llm_usage exceeds the stored bounds" \
        in narrative_problems(report)
    assert not document_ok(verify_document(report))


def test_unsealed_usage_over_the_call_ceiling_fails():
    report = _report()
    report["unsealed_llm_usage"] = [
        _call() for _ in range(MAX_CALLS_PER_USAGE_ENTRY + 1)]
    report["llm_usage_total"] = derived_usage_total(report)
    assert "unsealed_llm_usage exceeds the stored bounds" \
        in narrative_problems(report)
    assert not document_ok(verify_document(report))


def test_unsealed_usage_not_a_list_fails():
    report = _report()
    report["unsealed_llm_usage"] = "not-a-list"
    assert "unsealed_llm_usage exceeds the stored bounds" \
        in narrative_problems(report)


def test_inflated_candidate_dir_fails():
    report = _report()
    report["attempts"][0]["candidate_dir"] = "z" * 500_000
    assert "attempt candidate_dir exceeds the stored bounds" \
        in narrative_problems(report)
    assert not document_ok(verify_document(report))


def test_non_string_candidate_dir_fails():
    report = _report()
    report["attempts"][0]["candidate_dir"] = 12345
    assert "attempt candidate_dir exceeds the stored bounds" \
        in narrative_problems(report)


def test_report_spec_name_over_the_sealed_cap_fails():
    report = _report()
    report["spec_name"] = "s" * (MAX_SPEC_NAME_CHARS + 1)
    assert "report spec_name exceeds the sealed maximum" \
        in narrative_problems(report)
    assert not document_ok(verify_document(report))


def test_report_adapter_over_the_sealed_cap_fails():
    report = _report()
    report["llm_adapter"] = "a" * (MAX_ADAPTER_NAME_CHARS + 1)
    assert "report llm_adapter exceeds the sealed maximum" \
        in narrative_problems(report)
    assert not document_ok(verify_document(report))


# -- ceilings are generous, not tight ---------------------------------------


def test_report_exactly_at_the_new_ceilings_verifies():
    report = _report()
    report["unsealed_llm_usage"] = [
        _call() for _ in range(MAX_CALLS_PER_USAGE_ENTRY)]
    report["llm_usage_total"] = derived_usage_total(report)
    report["attempts"][0]["candidate_dir"] = "d" * MAX_CANDIDATE_DIR_CHARS
    assert narrative_problems(report) == []
    assert document_ok(verify_document(report))


def test_all_committed_reports_verify():
    for path in ALL_REPORTS:
        report = _report(path)
        assert narrative_problems(report) == [], path
        assert document_ok(verify_document(report)), path


# -- CLI + web view surfaces -------------------------------------------------


def test_cli_verify_exits_1_on_inflated_unsealed_usage(tmp_path, capsys):
    report = _report()
    report["unsealed_llm_usage"] = [
        _call() for _ in range(MAX_CALLS_PER_USAGE_ENTRY + 1)]
    report["llm_usage_total"] = derived_usage_total(report)
    out = tmp_path / "report.json"
    out.write_text(json.dumps(report), encoding="utf-8")
    assert cli.main(["verify", "--record", str(out)]) == 1
    assert "report summary: MISMATCH" in capsys.readouterr().out


def test_webview_flags_inflated_candidate_dir():
    report = _report()
    report["attempts"][0]["candidate_dir"] = "z" * 500_000
    html = render_document(report)
    assert "REPORT SUMMARY MISMATCH" in html


def test_webview_no_banner_for_intact_report():
    html = render_document(_report())
    assert "REPORT SUMMARY MISMATCH" not in html
