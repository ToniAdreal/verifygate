"""Staged path conflicts (v1.90.0).

Staging judged each file patch independently, so a diff that
staged both a file and a path beneath it — creating ``x`` and
``x/y``, in either order — staged cleanly and then failed in
the commit loop, after the first target had already been
written: a raw ``FileExistsError`` / ``IsADirectoryError``
(not a ``PatchError``, so it escaped the repair loop's
rejection handling and lost the whole report) over a
half-applied tree. Staged targets are now checked against
each other before anything is written: no staged target may
be a proper ancestor of another staged target. Such a diff
is a clean ``PatchError``, the tree is left untouched, and
the repair loop feeds the rejection back like any other.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from verifygate.mock_llm import MockLLMAdapter
from verifygate.patch import PatchError, apply_patch
from verifygate.repair import run_repair
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup
from verifygate.verify import document_ok, verify_document

APP = Path(__file__).resolve().parents[1]
SPEC = load_spec(APP / "samples" / "spec.json")
FAILING_DIR = APP / "samples" / "failing"


def _create(rel: str, text: str = "content") -> str:
    return (
        f"diff --git a/{rel} b/{rel}\n"
        f"new file mode 100644\n"
        f"--- /dev/null\n"
        f"+++ b/{rel}\n"
        f"@@ -0,0 +1 @@\n"
        f"+{text}\n"
    )


def _modify(rel: str, old: str, new: str) -> str:
    return (
        f"diff --git a/{rel} b/{rel}\n"
        f"--- a/{rel}\n"
        f"+++ b/{rel}\n"
        f"@@ -1 +1 @@\n"
        f"-{old}\n"
        f"+{new}\n"
    )


def _tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


# -- the conflict is a clean, atomic rejection ----------------------------


def test_file_then_child_creation_is_a_patch_error(tmp_path):
    with pytest.raises(PatchError, match="stages both x and x/y"):
        apply_patch(tmp_path, _create("x") + _create("x/y"))
    assert _tree(tmp_path) == {}


def test_child_then_file_creation_is_a_patch_error(tmp_path):
    with pytest.raises(PatchError, match="stages both x and x/y"):
        apply_patch(tmp_path, _create("x/y") + _create("x"))
    assert _tree(tmp_path) == {}


def test_modify_file_and_create_child_is_a_patch_error(tmp_path):
    # The child creation is rejected at staging already: stat-ing
    # x/y through the regular file x is an unusable path. Either
    # way the contract is one PatchError and an untouched tree —
    # never the pre-v1.90.0 raw OSError over a half-applied tree.
    (tmp_path / "x").write_text("old\n")
    diff = _modify("x", "old", "new") + _create("x/y")
    with pytest.raises(PatchError, match="unusable path"):
        apply_patch(tmp_path, diff)
    assert _tree(tmp_path) == {"x": b"old\n"}


def test_create_child_and_modify_file_is_a_patch_error(tmp_path):
    (tmp_path / "x").write_text("old\n")
    diff = _create("x/y") + _modify("x", "old", "new")
    with pytest.raises(PatchError, match="unusable path"):
        apply_patch(tmp_path, diff)
    assert _tree(tmp_path) == {"x": b"old\n"}


def test_deep_descendant_conflicts_too(tmp_path):
    with pytest.raises(PatchError, match="stages both a and a/b/c"):
        apply_patch(tmp_path, _create("a") + _create("a/b/c"))
    assert _tree(tmp_path) == {}


def test_conflict_with_an_unrelated_valid_file_writes_nothing(tmp_path):
    diff = _create("fine.txt") + _create("x") + _create("x/y")
    with pytest.raises(PatchError):
        apply_patch(tmp_path, diff)
    assert _tree(tmp_path) == {}


# -- non-conflicts still apply --------------------------------------------


def test_sibling_creations_apply(tmp_path):
    changed = apply_patch(tmp_path, _create("x/y") + _create("x/z"))
    assert changed == ["x/y", "x/z"]
    assert _tree(tmp_path) == {"x/y": b"content\n", "x/z": b"content\n"}


def test_string_prefix_is_not_ancestry(tmp_path):
    # "xy" shares a string prefix with "x/y" but no path component.
    changed = apply_patch(tmp_path, _create("xy") + _create("x/y"))
    assert changed == ["xy", "x/y"]
    assert _tree(tmp_path) == {"x/y": b"content\n", "xy": b"content\n"}


def test_file_beside_a_created_directory_path_applies(tmp_path):
    changed = apply_patch(tmp_path, _create("x/y") + _create("w"))
    assert changed == ["x/y", "w"]


# -- the repair loop survives the rejection --------------------------------


class ConflictingThenGood(MockLLMAdapter):
    """First proposal stages a file and a path beneath it; the
    loop's feedback retry then gets the mock's known-good fix."""

    def __init__(self):
        self.calls = 0

    def generate_patch(self, brief, failing_evidence=None):
        self.calls += 1
        if self.calls == 1:
            return _create("x") + _create("x/y")
        return super().generate_patch(brief, failing_evidence)


def test_repair_loop_feeds_back_a_path_conflict_and_repairs():
    before = hashlib.md5(
        (FAILING_DIR / "stringtools.py").read_bytes()).hexdigest()
    report = run_repair(
        spec=SPEC, candidate_dir=FAILING_DIR, mode="judge",
        llm=ConflictingThenGood(), references=MockReferenceLookup(),
        spec_label="samples/spec.json",
        candidate_label="samples/failing", max_rounds=3)
    assert report["final_verdict"] == "PASS"
    assert report["rounds_used"] == 2
    failed = report["failed_patch"]
    assert failed["applied"] is False
    assert failed["error_kind"] == "patch_rejected"
    assert "stages both x and x/y" in failed["error"]
    results = verify_document(report)
    assert document_ok(results)
    after = hashlib.md5(
        (FAILING_DIR / "stringtools.py").read_bytes()).hexdigest()
    assert after == before
