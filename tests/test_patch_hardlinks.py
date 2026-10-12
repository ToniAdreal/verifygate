"""Patch-commit hardlink fidelity (v1.87.0).

_safe_target judges a patch path by its resolved location, but
a target file can share its inode with a file *outside* the
workspace — or with a second name inside it — through a
hardlink, and writing in place truncates the shared inode:
patching the target rewrote the outside file too (reproduced
against v1.86.0: an outside file hardlinked to the target came
back with the patched content). The commit now unlinks a
multiply-linked target before writing, so the patched path
gets its own inode and every other name keeps its content —
the semantics the workspace copy always had, since it
materialises each verified file as its own inode.
"""

import os
import stat

import pytest

from verifygate.patch import PatchError, apply_patch

MODIFY = """\
--- a/a.txt
+++ b/a.txt
@@ -1,3 +1,3 @@
 line1
-line2
+CHANGED
 line3
"""

MODE_ONLY = """\
diff --git a/a.txt b/a.txt
old mode 100644
new mode 100755
"""


def _root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    return root


def _linked(tmp_path):
    root = _root(tmp_path)
    outside = tmp_path / "outside.txt"
    os.link(root / "a.txt", outside)
    return root, outside


def test_modify_does_not_write_through_hardlink_to_outside(tmp_path):
    root, outside = _linked(tmp_path)
    assert apply_patch(root, MODIFY) == ["a.txt"]
    assert (root / "a.txt").read_text(encoding="utf-8") == \
        "line1\nCHANGED\nline3\n"
    assert outside.read_text(encoding="utf-8") == "line1\nline2\nline3\n"
    assert (root / "a.txt").stat().st_ino != outside.stat().st_ino


def test_modify_preserves_mode_when_breaking_hardlink(tmp_path):
    root, outside = _linked(tmp_path)
    (root / "a.txt").chmod(0o640)
    apply_patch(root, MODIFY)
    assert stat.S_IMODE((root / "a.txt").stat().st_mode) == 0o640
    assert stat.S_IMODE(outside.stat().st_mode) == 0o640


def test_mode_only_patch_does_not_chmod_outside_hardlink(tmp_path):
    root, outside = _linked(tmp_path)
    initial_mode = stat.S_IMODE(outside.stat().st_mode)
    apply_patch(root, MODE_ONLY)
    assert stat.S_IMODE((root / "a.txt").stat().st_mode) == 0o755
    assert stat.S_IMODE(outside.stat().st_mode) == initial_mode
    assert outside.read_text(encoding="utf-8") == "line1\nline2\nline3\n"
    assert (root / "a.txt").read_text(encoding="utf-8") == \
        "line1\nline2\nline3\n"


def test_second_name_inside_root_keeps_its_content(tmp_path):
    root = _root(tmp_path)
    alias = root / "alias.txt"
    os.link(root / "a.txt", alias)
    apply_patch(root, MODIFY)
    assert (root / "a.txt").read_text(encoding="utf-8") == \
        "line1\nCHANGED\nline3\n"
    assert alias.read_text(encoding="utf-8") == "line1\nline2\nline3\n"


def test_unlinked_file_patches_in_place_as_before(tmp_path):
    root = _root(tmp_path)
    inode_before = (root / "a.txt").stat().st_ino
    apply_patch(root, MODIFY)
    assert (root / "a.txt").read_text(encoding="utf-8") == \
        "line1\nCHANGED\nline3\n"
    assert (root / "a.txt").stat().st_ino == inode_before


def test_failed_patch_leaves_hardlinked_files_untouched(tmp_path):
    root, outside = _linked(tmp_path)
    bad = MODIFY.replace("line1", "nosuch")
    with pytest.raises(PatchError):
        apply_patch(root, bad)
    assert (root / "a.txt").read_text(encoding="utf-8") == \
        "line1\nline2\nline3\n"
    assert outside.read_text(encoding="utf-8") == "line1\nline2\nline3\n"
    assert (root / "a.txt").stat().st_ino == outside.stat().st_ino


def test_deleting_one_hardlink_name_keeps_outside_content(tmp_path):
    root, outside = _linked(tmp_path)
    delete = """\
--- a/a.txt
+++ /dev/null
@@ -1,3 +0,0 @@
-line1
-line2
-line3
"""
    apply_patch(root, delete)
    assert not (root / "a.txt").exists()
    assert outside.read_text(encoding="utf-8") == "line1\nline2\nline3\n"
