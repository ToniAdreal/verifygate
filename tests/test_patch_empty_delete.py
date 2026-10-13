"""Empty-file deletion (v1.94.0).

Git emits no ---/+++ pair and no hunks when an empty file is
deleted: the whole block is a `diff --git` line, a
`deleted file mode` line, and an `index` line. That mode line
was not parsed at all, so the block produced no file patch —
alone it failed as "no unified diff found", and inside a
multi-file diff it was silently dropped while the rest
applied and reported success, leaving a file the diff had
deleted in the patched tree.
"""

from pathlib import Path

import pytest

from verifygate.patch import NoPatchFound, PatchError, apply_patch, parse_patch

DELETE_EMPTY_GIT = """\
diff --git a/e.txt b/e.txt
deleted file mode 100644
index e69de29..0000000
"""

DELETE_EMPTY_EXEC_GIT = """\
diff --git a/e.sh b/e.sh
deleted file mode 100755
index e69de29..0000000
"""

DELETE_NONEMPTY_GIT = """\
diff --git a/e.txt b/e.txt
deleted file mode 100644
index 0123456..0000000
--- a/e.txt
+++ /dev/null
@@ -1 +0,0 @@
-x
"""

MIXED_DELETE_THEN_MODIFY = """\
diff --git a/e.txt b/e.txt
deleted file mode 100644
index e69de29..0000000
diff --git a/a.txt b/a.txt
index 1111111..2222222 100644
--- a/a.txt
+++ b/a.txt
@@ -1,2 +1,2 @@
 one
-two
+TWO
"""

MIXED_DELETE_THEN_MODIFY_NO_GIT_LINE = """\
diff --git a/e.txt b/e.txt
deleted file mode 100644
index e69de29..0000000
--- a/a.txt
+++ b/a.txt
@@ -1,2 +1,2 @@
 one
-two
+TWO
"""


def test_empty_delete_git_block_parses_as_hunkless_deletion():
    (fp,) = parse_patch(DELETE_EMPTY_GIT)
    assert (fp.old_path, fp.new_path) == ("e.txt", "/dev/null")
    assert fp.hunks == []


def test_empty_delete_git_block_applies(tmp_path: Path):
    (tmp_path / "e.txt").write_text("")
    assert apply_patch(tmp_path, DELETE_EMPTY_GIT) == ["e.txt"]
    assert not (tmp_path / "e.txt").exists()


def test_empty_delete_executable_applies(tmp_path: Path):
    (tmp_path / "e.sh").write_text("")
    assert apply_patch(tmp_path, DELETE_EMPTY_EXEC_GIT) == ["e.sh"]
    assert not (tmp_path / "e.sh").exists()


def test_empty_delete_inside_multi_file_diff_is_not_dropped(tmp_path: Path):
    (tmp_path / "e.txt").write_text("")
    (tmp_path / "a.txt").write_text("one\ntwo\n")
    changed = apply_patch(tmp_path, MIXED_DELETE_THEN_MODIFY)
    assert changed == ["e.txt", "a.txt"]
    assert not (tmp_path / "e.txt").exists()
    assert (tmp_path / "a.txt").read_text() == "one\nTWO\n"


def test_empty_delete_flushed_before_following_pair_without_git_line(
        tmp_path: Path):
    (tmp_path / "e.txt").write_text("")
    (tmp_path / "a.txt").write_text("one\ntwo\n")
    changed = apply_patch(tmp_path, MIXED_DELETE_THEN_MODIFY_NO_GIT_LINE)
    assert changed == ["e.txt", "a.txt"]
    assert not (tmp_path / "e.txt").exists()
    assert (tmp_path / "a.txt").read_text() == "one\nTWO\n"


def test_nonempty_delete_with_hunks_still_applies(tmp_path: Path):
    (tmp_path / "e.txt").write_text("x\n")
    assert apply_patch(tmp_path, DELETE_NONEMPTY_GIT) == ["e.txt"]
    assert not (tmp_path / "e.txt").exists()


def test_hunkless_delete_of_nonempty_file_is_rejected(tmp_path: Path):
    (tmp_path / "e.txt").write_text("x\n")
    with pytest.raises(PatchError, match="leaves content"):
        apply_patch(tmp_path, DELETE_EMPTY_GIT)
    assert (tmp_path / "e.txt").read_text() == "x\n"


def test_pair_only_delete_of_nonempty_file_is_rejected(tmp_path: Path):
    (tmp_path / "e.txt").write_text("x\n")
    with pytest.raises(PatchError, match="leaves content"):
        apply_patch(tmp_path, "--- a/e.txt\n+++ /dev/null\n")
    assert (tmp_path / "e.txt").read_text() == "x\n"


def test_pair_only_delete_of_empty_file_applies(tmp_path: Path):
    (tmp_path / "e.txt").write_text("")
    assert apply_patch(tmp_path, "--- a/e.txt\n+++ /dev/null\n") == ["e.txt"]
    assert not (tmp_path / "e.txt").exists()


def test_pair_only_create_of_empty_file_applies(tmp_path: Path):
    changed = apply_patch(tmp_path, "--- /dev/null\n+++ b/new.txt\n")
    assert changed == ["new.txt"]
    assert (tmp_path / "new.txt").read_bytes() == b""


def test_delete_of_missing_file_is_rejected(tmp_path: Path):
    with pytest.raises(PatchError, match="not a file"):
        apply_patch(tmp_path, DELETE_EMPTY_GIT.replace("e.txt", "gone.txt"))


def test_delete_with_symlink_mode_is_rejected_at_parse(tmp_path: Path):
    (tmp_path / "e.txt").write_text("")
    with pytest.raises(PatchError, match="unsupported mode"):
        apply_patch(tmp_path, DELETE_EMPTY_GIT.replace("100644", "120000"))
    assert (tmp_path / "e.txt").exists()


def test_delete_with_malformed_mode_is_rejected_at_parse(tmp_path: Path):
    (tmp_path / "e.txt").write_text("")
    with pytest.raises(PatchError, match="malformed mode"):
        apply_patch(tmp_path, DELETE_EMPTY_GIT.replace("100644", "xyz"))
    assert (tmp_path / "e.txt").exists()


def test_failed_empty_delete_leaves_rest_of_diff_unapplied(tmp_path: Path):
    # The deletion targets a non-empty file, so the whole diff
    # must fail atomically: the modification in the same diff
    # may not land either.
    (tmp_path / "e.txt").write_text("x\n")
    (tmp_path / "a.txt").write_text("one\ntwo\n")
    with pytest.raises(PatchError, match="leaves content"):
        apply_patch(tmp_path, MIXED_DELETE_THEN_MODIFY)
    assert (tmp_path / "e.txt").read_text() == "x\n"
    assert (tmp_path / "a.txt").read_text() == "one\ntwo\n"
