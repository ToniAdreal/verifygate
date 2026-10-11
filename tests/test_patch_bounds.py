"""Patch-result bounds: a patch may not grow the candidate past
the candidate size ceilings.

Before v1.25.0 ``apply_patch`` staged and wrote a diff of any
size: a patch could grow the candidate beyond the very bounds
v1.23.0 put on submitted candidates (``MAX_CANDIDATE_FILES`` /
``MAX_CANDIDATE_BYTES`` in ``sandbox.py``). In the repair loop
that growth was then fatal in the wrong way — re-verifying the
patched copy raised ``CandidateTooLargeError`` out of
``prepare_workspace``'s pre-flight, which is not a ``PatchError``,
so it escaped the loop and destroyed the whole report (attempt
0's already-sealed FAIL evidence included), surfacing at the CLI
as a usage error (exit 2, nothing written) for a run whose
verdict was already decided. A 5,001-byte creation against a
bound 100 bytes above the original candidate reproduced both
halves before the fix.

Contract pinned here:
* ``apply_patch`` measures the staged result — the containment
  traversal's verified files, with staged creations /
  modifications / deletions folded over the on-disk baseline —
  against the candidate ceilings *before* writing anything, and
  an oversize result raises ``PatchError`` with the tree
  byte-for-byte unchanged (atomicity is preserved);
* exactly-at-the-ceiling results apply; deletions free budget;
  files the traversal does not verify (housekeeping names,
  ``*.pyc``) do not count, mirroring ``check_candidate_bounds``;
* in the repair loop an oversize proposal is an ordinary
  rejection: preserved as ``failed_patch`` with
  ``error_kind: patch_rejected``, fed back on retry, and the
  report is delivered (CLI exit 1, report written);
* replaying a report whose recorded patch outgrows the bounds
  reports the patch as no longer applying, never a crash.
"""

import json
from pathlib import Path

import pytest

from verifygate import cli, sandbox
from verifygate.mock_llm import MockLLMAdapter
from verifygate.patch import PatchError, apply_patch
from verifygate.repair import run_repair
from verifygate.replay import replay_document, replay_ok
from verifygate.sandbox import check_candidate_bounds
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup
from verifygate.verify import document_ok, verify_document

APP = Path(__file__).resolve().parent.parent
SPEC = load_spec(APP / "samples" / "spec.json")
FAILING_DIR = APP / "samples" / "failing"

BIG_PATCH = (
    "--- /dev/null\n"
    "+++ b/big.txt\n"
    "@@ -0,0 +1 @@\n"
    "+" + "x" * 5000 + "\n"
)


def _candidate(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "cand"
    root.mkdir()
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


# -- apply_patch: the staged result is bounded ------------------------------


def test_create_beyond_byte_bound_is_rejected_atomically(tmp_path):
    root = _candidate(tmp_path, {"a.txt": "hello\n"})
    with pytest.raises(PatchError, match="verified bytes"):
        apply_patch(root, BIG_PATCH, max_bytes=100)
    # Nothing was written: the tree is byte-for-byte the original.
    assert sorted(p.name for p in root.iterdir()) == ["a.txt"]
    assert (root / "a.txt").read_text(encoding="utf-8") == "hello\n"


def test_result_exactly_at_the_byte_bound_applies(tmp_path):
    root = _candidate(tmp_path, {"a.txt": "hello\n"})  # 6 bytes
    patch = ("--- /dev/null\n+++ b/b.txt\n@@ -0,0 +1 @@\n"
             "+" + "y" * 93 + "\n")  # 94 bytes -> total exactly 100
    assert apply_patch(root, patch, max_bytes=100) == ["b.txt"]
    assert check_candidate_bounds(root, max_bytes=100) == (2, 100)


def test_modify_beyond_byte_bound_is_rejected(tmp_path):
    root = _candidate(tmp_path, {"a.txt": "hello\n"})
    patch = ("--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n"
             "-hello\n+" + "z" * 500 + "\n")
    with pytest.raises(PatchError, match="verified bytes"):
        apply_patch(root, patch, max_bytes=100)
    assert (root / "a.txt").read_text(encoding="utf-8") == "hello\n"


def test_create_beyond_file_bound_is_rejected_atomically(tmp_path):
    root = _candidate(tmp_path, {"a.txt": "hello\n"})
    patch = "".join(
        f"--- /dev/null\n+++ b/f{i}.txt\n@@ -0,0 +1 @@\n+x\n"
        for i in range(5))
    with pytest.raises(PatchError, match="verified files"):
        apply_patch(root, patch, max_files=3)
    assert sorted(p.name for p in root.iterdir()) == ["a.txt"]


def test_deletion_frees_budget_for_creation(tmp_path):
    root = _candidate(tmp_path, {"big.txt": "b" * 1000, "a.txt": "hi\n"})
    patch = (
        "--- a/big.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n"
        "-" + "b" * 1000 + "\n"
        "--- /dev/null\n+++ b/mid.txt\n@@ -0,0 +1 @@\n"
        "+" + "m" * 500 + "\n"
    )
    # Without the deletion the result would be 1,504 bytes; with
    # it, 504 — under the 600-byte bound.
    assert apply_patch(root, patch, max_bytes=600) == ["big.txt", "mid.txt"]
    assert check_candidate_bounds(root, max_bytes=600) == (2, 504)


def test_unverified_files_do_not_count_toward_the_bound(tmp_path):
    root = _candidate(tmp_path, {"a.txt": "hello\n"})
    patch = ("--- /dev/null\n+++ b/__pycache__/junk.pyc\n"
             "@@ -0,0 +1 @@\n+" + "x" * 5000 + "\n")
    # The traversal never verifies housekeeping files, so the same
    # 5,001 bytes that are rejected as big.txt apply here — the
    # bound mirrors check_candidate_bounds exactly.
    assert apply_patch(root, patch, max_bytes=100) == [
        "__pycache__/junk.pyc"]


def test_defaults_are_the_sandbox_ceilings(tmp_path, monkeypatch):
    root = _candidate(tmp_path, {"a.txt": "hello\n"})
    monkeypatch.setattr(sandbox, "MAX_CANDIDATE_BYTES", 100)
    with pytest.raises(PatchError, match="verified bytes"):
        apply_patch(root, BIG_PATCH)  # no per-call override
    assert not (root / "big.txt").exists()


# -- the repair loop: oversize is an ordinary rejection ---------------------


class BigPatchLLM(MockLLMAdapter):
    name = "stub-big-patch"

    def generate_patch(self, brief, failing_evidence=None):
        return BIG_PATCH


def _tight_bound(monkeypatch, slack: int = 100) -> int:
    _count, total = check_candidate_bounds(FAILING_DIR)
    # The original candidate stays in bounds; the +5,001-byte
    # patched result does not.
    monkeypatch.setattr(sandbox, "MAX_CANDIDATE_BYTES", total + slack)
    return total


def test_repair_report_survives_an_oversize_patch(monkeypatch):
    _tight_bound(monkeypatch)
    report = run_repair(
        spec=SPEC, candidate_dir=FAILING_DIR, mode="judge",
        llm=BigPatchLLM(), references=MockReferenceLookup(),
        spec_label="samples/spec.json",
        candidate_label="samples/failing", max_rounds=1)
    assert report["final_verdict"] == "FAIL"
    assert report["stopped_reason"] == "patch_not_applicable"
    assert report["rounds_used"] == 1
    assert len(report["attempts"]) == 1  # attempt 0's evidence kept
    failed = report["failed_patch"]
    assert failed["applied"] is False
    assert failed["error_kind"] == "patch_rejected"
    assert "verified bytes" in failed["error"]
    assert report["last_patch_error"] == failed["error"]
    assert document_ok(verify_document(report))
    # The original candidate was never grown.
    assert not (FAILING_DIR / "big.txt").exists()


def test_oversize_rejection_is_fed_back_and_retried(monkeypatch):
    # Slack 1,000 admits the mock's genuine slugify fix (a small
    # size delta) while still rejecting the +5,001-byte proposal.
    _tight_bound(monkeypatch, slack=1000)
    seen_errors = []

    class BigThenFix(MockLLMAdapter):
        name = "stub-big-then-fix"

        def generate_patch(self, brief, failing_evidence=None):
            if failing_evidence and failing_evidence.get("patch_error"):
                seen_errors.append(failing_evidence["patch_error"])
                return MockLLMAdapter().generate_patch(
                    brief, failing_evidence)
            return BIG_PATCH

    report = run_repair(
        spec=SPEC, candidate_dir=FAILING_DIR, mode="judge",
        llm=BigThenFix(), references=MockReferenceLookup(),
        spec_label="samples/spec.json",
        candidate_label="samples/failing", max_rounds=3)
    assert report["final_verdict"] == "PASS"
    assert report["rounds_used"] == 2
    assert len(seen_errors) == 1
    assert "verified bytes" in seen_errors[0]
    assert document_ok(verify_document(report))


def test_cli_repair_writes_the_report_and_exits_1(monkeypatch, tmp_path):
    _tight_bound(monkeypatch)
    monkeypatch.setattr(
        cli, "_adapters",
        lambda mode, model=None: (BigPatchLLM(), MockReferenceLookup()))
    out = tmp_path / "repair.json"
    rc = cli.main(["repair", "--spec", str(APP / "samples" / "spec.json"),
                   "--candidate", str(FAILING_DIR), "--mode", "real",
                   "--max-rounds", "1", "--out", str(out)])
    assert rc == 1  # FAIL verdict, not the exit-2 usage error
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["stopped_reason"] == "patch_not_applicable"
    assert report["failed_patch"]["error_kind"] == "patch_rejected"


# -- replay: an oversize recorded patch no longer applies -------------------


def test_replay_of_oversize_recorded_patch_reports_cleanly(monkeypatch):
    # Craft the report under the real (generous) bounds: the big
    # patch applies, the candidate still fails its checks.
    report = run_repair(
        spec=SPEC, candidate_dir=FAILING_DIR, mode="judge",
        llm=BigPatchLLM(), references=MockReferenceLookup(),
        spec_label="samples/spec.json",
        candidate_label="samples/failing", max_rounds=1)
    assert report["attempts"][1]["patch_from_previous"]["applied"]
    # Replay under a tightened bound: reconstruction must surface
    # the PatchError as a per-attempt error, never raise it.
    _tight_bound(monkeypatch)
    results = replay_document(
        report, spec=SPEC, candidate_dir=FAILING_DIR, mode="judge",
        llm=MockLLMAdapter(), references=MockReferenceLookup(),
        spec_label="samples/spec.json")
    assert len(results) == 2
    assert results[0]["error"] is None
    assert results[1]["error"] is not None
    assert "recorded patch no longer applies" in results[1]["error"]
    assert not replay_ok(results)
