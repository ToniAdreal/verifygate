"""Multi-file patch atomicity (v1.10.0).

The applier's contract is "a diff that does not apply cleanly is
never partially forced in". Before v1.10.0 that held only within a
single file: files were written one by one, so a multi-file diff
whose second file failed left the first file already rewritten.
These tests pin the staged, all-or-nothing behaviour: on any
PatchError the tree is byte-for-byte and mode-for-mode unchanged,
and a fully valid multi-file diff still applies completely.
"""

from __future__ import annotations

import os
import stat

import pytest

from verifygate.patch import PatchError, apply_patch

MODIFY_A = """\
--- a/a.txt
+++ b/a.txt
@@ -1,2 +1,2 @@
 one
-two
+TWO
"""

FAILING_B = """\
--- a/b.txt
+++ b/b.txt
@@ -1,2 +1,2 @@
 alpha
-WRONG
+BETA
"""

CREATE_C = """\
--- /dev/null
+++ b/c.txt
@@ -0,0 +1 @@
+created
"""

DELETE_B = """\
--- a/b.txt
+++ /dev/null
@@ -1,2 +0,0 @@
-alpha
-beta
"""

MODE_A = """\
diff --git a/a.txt b/a.txt
old mode 100644
new mode 100755
"""


def _seed(tmp_path):
    (tmp_path / "a.txt").write_text("one\ntwo\n")
    (tmp_path / "b.txt").write_text("alpha\nbeta\n")


def test_failing_second_file_leaves_first_untouched(tmp_path):
    _seed(tmp_path)
    with pytest.raises(PatchError):
        apply_patch(tmp_path, MODIFY_A + FAILING_B)
    assert (tmp_path / "a.txt").read_text() == "one\ntwo\n"
    assert (tmp_path / "b.txt").read_text() == "alpha\nbeta\n"


def test_failing_patch_creates_no_new_file(tmp_path):
    _seed(tmp_path)
    with pytest.raises(PatchError):
        apply_patch(tmp_path, CREATE_C + FAILING_B)
    assert not (tmp_path / "c.txt").exists()
    assert (tmp_path / "b.txt").read_text() == "alpha\nbeta\n"


def test_failing_patch_deletes_nothing(tmp_path):
    _seed(tmp_path)
    with pytest.raises(PatchError):
        apply_patch(tmp_path, DELETE_B + FAILING_B.replace("b.txt", "a.txt")
                    .replace("-WRONG", "-WRONG"))
    # The deletion was staged first; the failing modify targets a.txt
    # (whose content does not contain the expected lines either).
    assert (tmp_path / "b.txt").read_text() == "alpha\nbeta\n"


def test_failing_patch_leaves_modes_unchanged(tmp_path):
    _seed(tmp_path)
    os.chmod(tmp_path / "a.txt", 0o644)
    with pytest.raises(PatchError):
        apply_patch(tmp_path, MODE_A + FAILING_B)
    assert stat.S_IMODE(os.stat(tmp_path / "a.txt").st_mode) == 0o644


def test_successful_multi_file_patch_applies_completely(tmp_path):
    _seed(tmp_path)
    changed = apply_patch(tmp_path, MODIFY_A + CREATE_C + DELETE_B)
    assert changed == ["a.txt", "c.txt", "b.txt"]
    assert (tmp_path / "a.txt").read_text() == "one\nTWO\n"
    assert (tmp_path / "c.txt").read_text() == "created\n"
    assert not (tmp_path / "b.txt").exists()


def test_sequential_patches_to_same_file_apply_in_order(tmp_path):
    _seed(tmp_path)
    second = """\
--- a/a.txt
+++ b/a.txt
@@ -1,2 +1,2 @@
 one
-TWO
+2
"""
    changed = apply_patch(tmp_path, MODIFY_A + second)
    assert changed == ["a.txt", "a.txt"]
    assert (tmp_path / "a.txt").read_text() == "one\n2\n"


def test_second_patch_to_same_file_failing_leaves_it_untouched(tmp_path):
    _seed(tmp_path)
    bad_second = """\
--- a/a.txt
+++ b/a.txt
@@ -1,2 +1,2 @@
 one
-NEVER-THERE
+2
"""
    with pytest.raises(PatchError):
        apply_patch(tmp_path, MODIFY_A + bad_second)
    assert (tmp_path / "a.txt").read_text() == "one\ntwo\n"
