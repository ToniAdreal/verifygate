"""Report shape bounds: unknown fields, kind/version, attempt count.

Before v1.32.0 a repair report's shape itself was never judged.
A single evidence record's top-level fields are held to an exact
field set (chain v3 seals them; legacy records get metadata
consistency rules), but a repair report — unchained at the top
level — could carry any extra field at any level: a
500,000-character string injected as a new top-level field, as a
new field on an attempt wrapper, or as a new field on a patch
linkage all verified INTACT, as did a rewritten ``kind``, a
500,000-character ``version``, and an attempt list far longer
than the loop's round budget can produce (all reproduced).

Contract pinned here:
* top-level keys are a subset of ``REPORT_KEYS``, attempt wrapper
  keys a subset of ``ATTEMPT_KEYS``, and patch linkage keys
  (applied or preserved) a subset of ``PATCH_KEYS`` — subset, not
  equality, so a legacy report omitting a later-added field is
  not failed for the omission;
* ``kind``, when present, is ``repair-report``; ``version``, when
  present, is a semver string;
* the attempt count fits the loop budget: at most
  ``MAX_ATTEMPTS`` = ``MAX_ROUNDS + 1``;
* every committed report fixture verifies untampered, and a fresh
  Judge Mode repair report verifies too.
"""

import copy
import json
from pathlib import Path

from verifygate import cli
from verifygate.repair import MAX_ROUNDS
from verifygate.verify import (
    ATTEMPT_KEYS,
    MAX_ATTEMPTS,
    PATCH_KEYS,
    REPORT_KEYS,
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


# -- the reproduced injections now fail -------------------------------------


def test_extra_top_level_field_fails():
    report = _report()
    report["evil_note"] = "x" * 500_000
    assert "repair report carries unknown top-level fields" \
        in narrative_problems(report)
    assert not document_ok(verify_document(report))


def test_extra_attempt_field_fails():
    report = _report()
    report["attempts"][0]["evil"] = "x" * 500_000
    assert "attempt carries unknown fields" in narrative_problems(report)
    assert not document_ok(verify_document(report))


def test_extra_patch_linkage_field_fails():
    report = _report()
    report["attempts"][1]["patch_from_previous"]["evil"] = "x" * 500_000
    assert "patch linkage carries unknown fields" \
        in narrative_problems(report)
    assert not document_ok(verify_document(report))


def test_extra_failed_patch_field_fails():
    report = _report(REPORT_V2)  # carries a preserved rejection
    assert report["failed_patch"] is not None
    report["failed_patch"]["evil"] = "x" * 500_000
    assert "failed_patch carries unknown fields" \
        in narrative_problems(report)
    assert not document_ok(verify_document(report))


def test_rewritten_kind_fails():
    report = _report()
    report["kind"] = "x" * 500_000
    assert "report kind is not repair-report" in narrative_problems(report)
    assert not document_ok(verify_document(report))


def test_rewritten_version_fails():
    report = _report()
    report["version"] = "x" * 500_000
    assert "report version is not a version string" \
        in narrative_problems(report)
    assert not document_ok(verify_document(report))


def test_non_string_version_fails():
    report = _report()
    report["version"] = 13200
    assert "report version is not a version string" \
        in narrative_problems(report)


def test_attempt_count_over_the_loop_maximum_fails():
    report = _report()
    # Duplicate the final attempt until the list is one over the
    # most a genuine loop can produce; the count bound must fire
    # (other problems, e.g. non-increasing rounds, may fire too).
    while len(report["attempts"]) <= MAX_ATTEMPTS:
        report["attempts"].append(copy.deepcopy(report["attempts"][-1]))
    assert len(report["attempts"]) == MAX_ATTEMPTS + 1
    assert "attempt count exceeds the loop maximum" \
        in narrative_problems(report)
    assert not document_ok(verify_document(report))


# -- the vocabularies are the loop's, and legacy omission is fine -----------


def test_vocabularies_match_the_emitted_shape():
    report = _report()
    assert set(report) <= REPORT_KEYS
    for attempt in report["attempts"]:
        assert set(attempt) == ATTEMPT_KEYS
        patch = attempt["patch_from_previous"]
        if patch is not None:
            assert set(patch) <= PATCH_KEYS
    assert MAX_ATTEMPTS == MAX_ROUNDS + 1


def test_legacy_omission_is_not_tamper():
    # The v0.2.0-era shape omits the fields added later; dropping
    # them (and a linkage's error_kind) must stay consistent.
    report = _report()
    for key in ("failed_patch", "llm_usage_total", "unsealed_llm_usage"):
        report.pop(key, None)
    report["attempts"][1]["patch_from_previous"].pop("error_kind", None)
    assert narrative_problems(report) == []
    assert document_ok(verify_document(report))


def test_all_committed_reports_verify():
    for path in ALL_REPORTS:
        report = _report(path)
        assert narrative_problems(report) == [], path
        assert document_ok(verify_document(report)), path


# -- CLI + web view surfaces -------------------------------------------------


def test_cli_verify_exits_1_on_extra_top_level_field(tmp_path, capsys):
    report = _report()
    report["evil_note"] = "injected"
    out = tmp_path / "report.json"
    out.write_text(json.dumps(report), encoding="utf-8")
    assert cli.main(["verify", "--record", str(out)]) == 1
    assert "report summary: MISMATCH" in capsys.readouterr().out


def test_webview_flags_extra_attempt_field():
    report = _report()
    report["attempts"][0]["evil"] = "injected"
    html = render_document(report)
    assert "REPORT SUMMARY MISMATCH" in html


def test_webview_no_banner_for_intact_report():
    html = render_document(_report())
    assert "REPORT SUMMARY MISMATCH" not in html
