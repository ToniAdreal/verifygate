"""Unhashable repair-report adapter (v1.63.0).

The narrative check's sealed-adapter cross-check compared the
sealed adapter names against ``{adapter}`` — a set built out of
the report's stated ``llm_adapter``, an untrusted field.  A
stated adapter rewritten as a list or an object is unhashable:
building the set raised ``TypeError`` and crashed
``verify_document`` and the web view (all reproduced against
v1.62.0); at the CLI the backstop reported it as a usage error
(exit 2) instead of the tampered-report answer (exit 1).

Contract pinned here:
* a list / object stated adapter is a narrative problem (not an
  adapter name, and not the sealed adapter), ``document_ok``
  fails, CLI ``verify`` exits 1, and the web view flags the
  report through its SUMMARY MISMATCH banner — no exception on
  any surface;
* hashable non-string adapters (int, bool) keep failing the
  same clean way they already did;
* a stated adapter that simply names a different string still
  fails as a mismatch, and every committed report fixture
  verifies untampered.
"""

import json
from pathlib import Path

import pytest

from verifygate import cli
from verifygate.verify import document_ok, narrative_problems, verify_document
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


UNHASHABLE = [[], ["nebius-token-factory"], {}, {"name": "x"}]


@pytest.mark.parametrize("value", UNHASHABLE,
                         ids=["empty-list", "list", "empty-dict", "dict"])
def test_unhashable_adapter_fails_narrative_without_crashing(value):
    report = _report()
    report["llm_adapter"] = value
    problems = narrative_problems(report)
    assert "report llm_adapter is not an adapter name" in problems
    assert "report adapter does not match the sealed attempts" in problems
    assert not document_ok(verify_document(report))


@pytest.mark.parametrize("value", UNHASHABLE,
                         ids=["empty-list", "list", "empty-dict", "dict"])
def test_unhashable_adapter_fails_on_every_committed_report(value):
    for path in ALL_REPORTS:
        report = _report(path)
        report["llm_adapter"] = value
        assert not document_ok(verify_document(report)), path


def test_cli_verify_exits_1_on_an_unhashable_adapter(tmp_path):
    for value in UNHASHABLE:
        report = _report()
        report["llm_adapter"] = value
        out = tmp_path / "report.json"
        out.write_text(json.dumps(report), encoding="utf-8")
        assert cli.main(["verify", "--record", str(out)]) == 1


def test_cli_view_renders_summary_mismatch_on_an_unhashable_adapter(tmp_path):
    report = _report()
    report["llm_adapter"] = ["not", "a", "name"]
    out = tmp_path / "report.json"
    out.write_text(json.dumps(report), encoding="utf-8")
    html_out = tmp_path / "report.html"
    assert cli.main(
        ["view", "--record", str(out), "--out", str(html_out)]) == 0
    assert "REPORT SUMMARY MISMATCH" in html_out.read_text(encoding="utf-8")


def test_webview_flags_an_unhashable_adapter():
    for value in UNHASHABLE:
        report = _report()
        report["llm_adapter"] = value
        assert "REPORT SUMMARY MISMATCH" in render_document(report)


@pytest.mark.parametrize("value", [123, True, 1.5],
                         ids=["int", "bool", "float"])
def test_hashable_non_string_adapters_still_fail_cleanly(value):
    report = _report()
    report["llm_adapter"] = value
    assert "report llm_adapter is not an adapter name" \
        in narrative_problems(report)
    assert not document_ok(verify_document(report))


def test_a_different_string_adapter_still_fails_as_a_mismatch():
    report = _report()
    report["llm_adapter"] = "some-other-adapter"
    assert "report adapter does not match the sealed attempts" \
        in narrative_problems(report)
    assert not document_ok(verify_document(report))


def test_committed_reports_verify_untampered():
    for path in ALL_REPORTS:
        report = _report(path)
        assert narrative_problems(report) == [], path
        assert document_ok(verify_document(report)), path
        assert "REPORT SUMMARY MISMATCH" not in render_document(report), path
