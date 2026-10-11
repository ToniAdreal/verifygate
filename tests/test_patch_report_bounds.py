"""Repair-report patch bounds: a report's patch linkage is bounded.

Before v1.30.0 the repair loop capped what it *stored* — the
preserved ``patch_text`` at 8,000 characters — but ``verify``'s
narrative check judged the report's unchained patch fields only
for type, and its ``changed_files`` lists and ``llm_usage``
snapshots not at all. Inflating a committed real-mode report's
preserved patch, its rejection error (with ``last_patch_error``
patched up to match), its changed-file list, or its usage
snapshot with 500,000-character strings still verified INTACT
(all four reproduced).

Contract pinned here:
* ``verify`` mirrors the source caps: a ``patch_text`` over
  ``MAX_PATCH_TEXT_CHARS``, a patch error / ``last_patch_error``
  over ``MAX_PATCH_ERROR_CHARS``, a ``changed_files`` list over
  the count/name ceilings, or an ``llm_usage`` snapshot outside
  the sealed-call ceilings each fail the narrative check (CLI
  exit 1, web view SUMMARY MISMATCH banner);
* a linkage exactly at every ceiling still verifies, and every
  committed report fixture verifies untampered;
* construction stays within the caps: a 500,000-character
  proposal is stored truncated to ``MAX_PATCH_TEXT_CHARS`` and
  its report verifies; an applier rejection whose message embeds
  an over-long path is stored truncated to
  ``MAX_PATCH_ERROR_CHARS`` and its report verifies.

Found on the way: a patch target whose name component is over
the filesystem limit made ``apply_patch`` raise a raw ``OSError``
(ENAMETOOLONG) out of its stat calls, escaping the repair loop's
``PatchError`` handling — the same escape class v1.11.0 closed
for binary targets. Unstatable/unresolvable targets and
uncreatable creation targets are now ``PatchError`` rejections,
raised before anything is staged or written.
"""

import json
import os
from pathlib import Path

import pytest

from verifygate import cli
from verifygate.mock_llm import MockLLMAdapter
from verifygate.patch import PatchError, apply_patch
from verifygate.repair import (
    MAX_PATCH_ERROR_CHARS,
    MAX_PATCH_TEXT_CHARS,
    run_repair,
)
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup
from verifygate.verify import (
    MAX_CHANGED_FILE_CHARS,
    MAX_CHANGED_FILES,
    document_ok,
    narrative_problems,
    verify_document,
)
from verifygate.webview import render_document

APP = Path(__file__).resolve().parent.parent
SPEC = load_spec(APP / "samples" / "spec.json")
FAILING_DIR = APP / "samples" / "failing"
REPORT_V2 = APP / "samples" / "evidence_real_repair_v2.json"


def _report() -> dict:
    return json.loads(REPORT_V2.read_text(encoding="utf-8"))


def _applied_patch(report: dict) -> dict:
    return report["attempts"][-1]["patch_from_previous"]


# -- the four reproduced inflations now fail --------------------------------


def test_inflated_applied_patch_text_fails():
    report = _report()
    _applied_patch(report)["patch_text"] = "x" * 500_000
    assert "patch text exceeds the stored maximum" in narrative_problems(report)
    assert not document_ok(verify_document(report))


def test_inflated_failed_patch_error_and_text_fail():
    report = _report()
    big = "E" * 500_000
    report["failed_patch"]["error"] = big
    report["last_patch_error"] = big  # patched up to match, as before
    report["failed_patch"]["patch_text"] = "y" * 500_000
    problems = narrative_problems(report)
    assert "failed_patch error exceeds the stored maximum" in problems
    assert "patch text exceeds the stored maximum" in problems
    assert "last_patch_error exceeds the stored maximum" in problems
    assert not document_ok(verify_document(report))


def test_inflated_changed_files_fail():
    report = _report()
    _applied_patch(report)["changed_files"] = ["f" * 500_000] * 5000
    assert "patch changed_files exceeds the stored bounds" in narrative_problems(report)
    assert not document_ok(verify_document(report))


def test_changed_files_over_the_count_ceiling_fails():
    report = _report()
    _applied_patch(report)["changed_files"] = ["a.py"] * (MAX_CHANGED_FILES + 1)
    assert "patch changed_files exceeds the stored bounds" in narrative_problems(report)
    assert not document_ok(verify_document(report))


def test_inflated_patch_usage_snapshot_fails():
    report = _report()
    _applied_patch(report)["llm_usage"] = [
        {"adapter": "a" * 500_000, "model": "m" * 500_000,
         "usage": {"total_tokens": 1}}] * 50
    assert "patch llm_usage exceeds the stored bounds" in narrative_problems(report)
    assert not document_ok(verify_document(report))


def test_patch_usage_snapshot_over_the_call_ceiling_fails():
    report = _report()
    call = {"adapter": "stub", "model": "m", "purpose": "generate_patch",
            "usage": {"total_tokens": 1}}
    _applied_patch(report)["llm_usage"] = [dict(call) for _ in range(51)]
    assert "patch llm_usage exceeds the stored bounds" in narrative_problems(report)
    assert not document_ok(verify_document(report))


def test_cli_verify_exits_1_on_an_inflated_patch(tmp_path):
    report = _report()
    _applied_patch(report)["patch_text"] = "x" * 500_000
    out = tmp_path / "report.json"
    out.write_text(json.dumps(report), encoding="utf-8")
    assert cli.main(["verify", "--record", str(out)]) == 1


def test_webview_flags_an_inflated_patch():
    report = _report()
    _applied_patch(report)["patch_text"] = "x" * 500_000
    assert "REPORT SUMMARY MISMATCH" in render_document(report)


# -- ceilings are generous, not tight ----------------------------------------


def test_linkage_exactly_at_every_ceiling_still_verifies():
    report = _report()
    patch = _applied_patch(report)
    patch["patch_text"] = "x" * MAX_PATCH_TEXT_CHARS
    patch["changed_files"] = ["f" * MAX_CHANGED_FILE_CHARS]
    call = {"adapter": "a" * 100, "model": "m" * 200,
            "purpose": "generate_patch", "usage": {"total_tokens": 3}}
    patch["llm_usage"] = [dict(call) for _ in range(50)]
    assert document_ok(verify_document(report))


def test_every_committed_report_still_verifies():
    paths = [
        APP / "samples" / "evidence_real_repair.json",
        REPORT_V2,
        APP / "samples" / "evidence_real_repair_v3.json",
        APP / "samples-js" / "evidence_real_repair.json",
        APP / "samples-js" / "evidence_real_repair_v3.json",
    ]
    for path in paths:
        report = json.loads(path.read_text(encoding="utf-8"))
        assert document_ok(verify_document(report)), path.name


# -- construction caps at the source -----------------------------------------


class HugePatchLLM(MockLLMAdapter):
    name = "stub-huge-patch"

    def generate_patch(self, brief, failing_evidence=None):
        return "x" * 500_000  # no diff in it: an ordinary rejection


def test_oversize_proposal_is_stored_truncated_and_verifies():
    report = run_repair(
        spec=SPEC, candidate_dir=FAILING_DIR, mode="judge",
        llm=HugePatchLLM(), references=MockReferenceLookup(),
        spec_label="samples/spec.json",
        candidate_label="samples/failing", max_rounds=1)
    failed = report["failed_patch"]
    assert len(failed["patch_text"]) == MAX_PATCH_TEXT_CHARS
    assert document_ok(verify_document(report))


class LongPathPatchLLM(MockLLMAdapter):
    name = "stub-long-path-patch"

    def generate_patch(self, brief, failing_evidence=None):
        # A diff whose target path is itself enormous: the applier's
        # rejection embeds the path verbatim.
        rel = "d" * 5000 + ".py"
        return f"--- a/{rel}\n+++ b/{rel}\n@@ -1 +1 @@\n-nope\n+fix\n"


def test_overlong_applier_error_is_stored_truncated_and_verifies():
    report = run_repair(
        spec=SPEC, candidate_dir=FAILING_DIR, mode="judge",
        llm=LongPathPatchLLM(), references=MockReferenceLookup(),
        spec_label="samples/spec.json",
        candidate_label="samples/failing", max_rounds=1)
    failed = report["failed_patch"]
    assert failed["error_kind"] == "patch_rejected"
    assert len(failed["error"]) == MAX_PATCH_ERROR_CHARS
    assert report["last_patch_error"] == failed["error"]
    assert document_ok(verify_document(report))
    # The original candidate was never touched. (listdir, not
    # Path.exists(): on some filesystems stat of an over-long name
    # raises ENAMETOOLONG instead of returning a clean False —
    # the dogfood sandbox lives on one of them.)
    assert ("d" * 5000 + ".py") not in os.listdir(FAILING_DIR)


def test_unstatable_patch_target_is_a_clean_rejection(tmp_path):
    rel = "d" * 5000 + ".py"
    patch = f"--- a/{rel}\n+++ b/{rel}\n@@ -1 +1 @@\n-nope\n+fix\n"
    with pytest.raises(PatchError):
        apply_patch(tmp_path, patch)
    assert list(tmp_path.iterdir()) == []


def test_uncreatable_patch_target_is_a_clean_rejection(tmp_path):
    rel = "d" * 5000 + ".py"
    patch = f"--- /dev/null\n+++ b/{rel}\n@@ -0,0 +1 @@\n+fix\n"
    with pytest.raises(PatchError):
        apply_patch(tmp_path, patch)
    assert list(tmp_path.iterdir()) == []
