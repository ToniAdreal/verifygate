"""Present-but-null repair-report fields (v1.61.0).

The narrative check's legacy policy — only fields that are
present are judged, so a report that *omits* a later-added field
is not failed for the omission — was implemented with
``is not None`` guards, which conflate "absent" with "present
but null".  The repair loop never emits a null for the fields
it always writes, yet nulling any of them verified INTACT
(all reproduced against v1.60.0): the report's ``kind``,
``version``, ``max_rounds``, ``stopped_reason`` and
``llm_adapter``; each attempt's ``candidate_dir``; the report's
``unsealed_llm_usage`` list; and each patch linkage's
``changed_files``, ``patch_text``, ``llm_usage`` and
``llm_adapter`` (applied linkage and preserved rejection alike).

Contract pinned here:
* a judged-when-present field is judged whenever its key is
  present, null included — nulling it is a narrative problem,
  ``document_ok`` fails, CLI ``verify`` exits 1, and the web
  view flags the report through its SUMMARY MISMATCH banner;
* a genuinely *omitted* field stays unjudged, exactly as
  before — omission and nulling are no longer the same edit;
* fields the loop itself emits as null stay legitimate:
  ``failed_patch`` / ``last_patch_error`` with no rejection,
  an applied patch's ``error`` / ``error_kind``, and attempt
  0's ``patch_from_previous``;
* every committed report fixture verifies untampered.
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


def _applied(report) -> dict:
    return report["attempts"][1]["patch_from_previous"]


# -- the reproduced nullings now fail ---------------------------------------


def _null_top(field):
    def tamper(report):
        report[field] = None
    return tamper


TOP_LEVEL_NULLS = [
    ("kind", "report kind is not repair-report"),
    ("version", "report version is not a version string"),
    ("max_rounds", "max_rounds is not a non-negative integer"),
    ("stopped_reason", "stopped_reason is not a stop reason"),
    ("llm_adapter", "report llm_adapter is not an adapter name"),
    ("unsealed_llm_usage", "unsealed_llm_usage exceeds the stored bounds"),
]


@pytest.mark.parametrize("field,problem", TOP_LEVEL_NULLS,
                         ids=[f for f, _ in TOP_LEVEL_NULLS])
def test_nulled_top_level_field_fails(field, problem):
    report = _report()
    report[field] = None
    assert problem in narrative_problems(report)
    assert not document_ok(verify_document(report))


def test_nulled_attempt_candidate_dir_fails():
    report = _report()
    report["attempts"][1]["candidate_dir"] = None
    assert "attempt candidate_dir exceeds the stored bounds" \
        in narrative_problems(report)
    assert not document_ok(verify_document(report))


def test_nulled_applied_patch_fields_fail():
    cases = [
        ("changed_files",
         "patch changed_files is not a list of file names"),
        ("patch_text", "patch text is not a string"),
        ("llm_usage", "patch llm_usage exceeds the stored bounds"),
        ("llm_adapter", "patch adapter is not an adapter name"),
    ]
    for field, problem in cases:
        report = _report()
        _applied(report)[field] = None
        assert problem in narrative_problems(report), field
        assert not document_ok(verify_document(report)), field


def test_nulled_failed_patch_fields_fail():
    # The v2/v3 reports carry a preserved round-1 rejection.
    for path in (REPORT_V2, REPORT_V3):
        base = _report(path)
        assert base["failed_patch"] is not None
        cases = [
            ("changed_files",
             "patch changed_files exceeds the stored bounds"),
            ("patch_text", "failed_patch patch text is not a string"),
            ("llm_usage", "patch llm_usage exceeds the stored bounds"),
            ("llm_adapter", "failed_patch adapter is not an adapter name"),
        ]
        for field, problem in cases:
            report = _report(path)
            report["failed_patch"][field] = None
            assert problem in narrative_problems(report), (path, field)
            assert not document_ok(verify_document(report)), (path, field)


def test_cli_verify_exits_1_on_a_nulled_field(tmp_path):
    report = _report()
    report["stopped_reason"] = None
    out = tmp_path / "report.json"
    out.write_text(json.dumps(report), encoding="utf-8")
    assert cli.main(["verify", "--record", str(out)]) == 1


def test_webview_flags_a_nulled_field():
    report = _report()
    report["max_rounds"] = None
    assert "REPORT SUMMARY MISMATCH" in render_document(report)


# -- omission stays legacy; the loop's own nulls stay legitimate ------------


def test_omitted_fields_stay_unjudged():
    # Deleting the same fields the tampers above null must behave
    # exactly as it did before v1.61.0: no narrative problem names
    # the omission, and the report still verifies.
    for field, _problem in TOP_LEVEL_NULLS:
        report = _report()
        del report[field]
        assert document_ok(verify_document(report)), field
    report = _report()
    del report["attempts"][1]["candidate_dir"]
    assert document_ok(verify_document(report))
    for field in ("changed_files", "patch_text", "llm_usage",
                  "llm_adapter"):
        report = _report()
        del _applied(report)[field]
        assert document_ok(verify_document(report)), field


def test_the_loops_own_nulls_stay_legitimate():
    # The committed reports already carry them: attempt 0's
    # patch_from_previous is null, an applied patch's error and
    # error_kind are null, and reports without a rejection carry
    # failed_patch / last_patch_error as null.
    for path in ALL_REPORTS:
        report = _report(path)
        assert document_ok(verify_document(report)), path
        assert narrative_problems(report) == [], path
