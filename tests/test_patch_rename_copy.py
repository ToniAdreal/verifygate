"""Rename/copy support (v1.93.0).

Git rename/copy headers were ignored entirely: a pure rename
or copy produced no file patch at all ("no unified diff
found"), and a rename with hunks was applied as a modification
of the new path — which still lives at the old path — and
rejected with "cannot modify <new>: not a file".
"""

from pathlib import Path

import pytest

from verifygate.patch import PatchError, apply_patch, parse_patch

RENAME_ONLY = """\
diff --git a/old.txt b/new.txt
similarity index 100%
rename from old.txt
rename to new.txt
"""

RENAME_MODIFY = """\
diff --git a/old.txt b/new.txt
similarity index 50%
rename from old.txt
rename to new.txt
--- a/old.txt
+++ b/new.txt
@@ -1 +1 @@
-old
+new
"""

PLAIN_RENAME = """\
--- a/old.txt
+++ b/new.txt
@@ -1 +1 @@
-old
+new
"""

COPY_ONLY = """\
diff --git a/old.txt b/copy.txt
similarity index 100%
copy from old.txt
copy to copy.txt
"""


def test_pure_rename_parses_as_hunkless_file_patch():
    (fp,) = parse_patch(RENAME_ONLY)
    assert (fp.old_path, fp.new_path) == ("old.txt", "new.txt")
    assert fp.is_rename and not fp.is_copy and fp.hunks == []


def test_pure_rename_applies(tmp_path: Path):
    (tmp_path / "old.txt").write_text("hello\n")
    changed = apply_patch(tmp_path, RENAME_ONLY)
    assert changed == ["old.txt", "new.txt"]
    assert not (tmp_path / "old.txt").exists()
    assert (tmp_path / "new.txt").read_text() == "hello\n"


def test_rename_with_modify_git_headers(tmp_path: Path):
    (tmp_path / "old.txt").write_text("old\n")
    assert apply_patch(tmp_path, RENAME_MODIFY) == ["old.txt", "new.txt"]
    assert not (tmp_path / "old.txt").exists()
    assert (tmp_path / "new.txt").read_text() == "new\n"


def test_rename_via_plain_header_pair(tmp_path: Path):
    (tmp_path / "old.txt").write_text("old\n")
    assert apply_patch(tmp_path, PLAIN_RENAME) == ["old.txt", "new.txt"]
    assert (tmp_path / "new.txt").read_text() == "new\n"


def test_rename_with_mode_change(tmp_path: Path):
    (tmp_path / "old.sh").write_text("echo hi\n")
    diff = (
        "diff --git a/old.sh b/new.sh\n"
        "old mode 100644\n"
        "new mode 100755\n"
        "similarity index 100%\n"
        "rename from old.sh\n"
        "rename to new.sh\n"
    )
    apply_patch(tmp_path, diff)
    assert (tmp_path / "new.sh").stat().st_mode & 0o777 == 0o755


def test_rename_preserves_executable_bit_without_mode_line(tmp_path: Path):
    target = tmp_path / "old.sh"
    target.write_text("echo hi\n")
    target.chmod(0o755)
    diff = RENAME_ONLY.replace("old.txt", "old.sh").replace(
        "new.txt", "new.sh")
    apply_patch(tmp_path, diff)
    assert (tmp_path / "new.sh").stat().st_mode & 0o777 == 0o755


def test_rename_preserves_trailing_newline_state_and_crlf(tmp_path: Path):
    (tmp_path / "old.txt").write_bytes(b"a\r\nb\r\n")
    diff = (
        "diff --git a/old.txt b/new.txt\n"
        "rename from old.txt\n"
        "rename to new.txt\n"
        "--- a/old.txt\n"
        "+++ b/new.txt\n"
        "@@ -1,2 +1,2 @@\n"
        " a\r\n-b\r\n+c\r\n"
    )
    apply_patch(tmp_path, diff)
    assert (tmp_path / "new.txt").read_bytes() == b"a\r\nc\r\n"
    (tmp_path / "bare.txt").write_bytes(b"no-newline")
    apply_patch(tmp_path, RENAME_ONLY.replace("old.txt", "bare.txt")
                .replace("new.txt", "moved.txt"))
    assert (tmp_path / "moved.txt").read_bytes() == b"no-newline"


def test_rename_into_new_subdirectory(tmp_path: Path):
    (tmp_path / "old.txt").write_text("a\n")
    diff = (
        "diff --git a/old.txt b/sub/new.txt\n"
        "rename from old.txt\n"
        "rename to sub/new.txt\n"
    )
    assert apply_patch(tmp_path, diff) == ["old.txt", "sub/new.txt"]
    assert (tmp_path / "sub" / "new.txt").read_text() == "a\n"


def test_rename_onto_path_deleted_in_same_diff(tmp_path: Path):
    (tmp_path / "old.txt").write_text("a\n")
    (tmp_path / "new.txt").write_text("stale\n")
    diff = (
        "--- a/new.txt\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-stale\n"
        "diff --git a/old.txt b/new.txt\n"
        "rename from old.txt\n"
        "rename to new.txt\n"
    )
    apply_patch(tmp_path, diff)
    assert not (tmp_path / "old.txt").exists()
    assert (tmp_path / "new.txt").read_text() == "a\n"


def test_rename_then_modify_new_name_in_same_diff(tmp_path: Path):
    (tmp_path / "old.txt").write_text("one\ntwo\n")
    diff = RENAME_ONLY + (
        "--- a/new.txt\n"
        "+++ b/new.txt\n"
        "@@ -1,2 +1,2 @@\n"
        " one\n"
        "-two\n"
        "+TWO\n"
    )
    apply_patch(tmp_path, diff)
    assert (tmp_path / "new.txt").read_text() == "one\nTWO\n"


def test_quoted_rename_paths(tmp_path: Path):
    (tmp_path / "café.txt").write_text("hi\n")
    diff = (
        'diff --git "a/caf\\303\\251.txt" "b/moved.txt"\n'
        'rename from "caf\\303\\251.txt"\n'
        "rename to moved.txt\n"
    )
    assert apply_patch(tmp_path, diff) == ["café.txt", "moved.txt"]
    assert (tmp_path / "moved.txt").read_text() == "hi\n"


def test_copy_only_keeps_source(tmp_path: Path):
    (tmp_path / "old.txt").write_text("hello\n")
    (fp,) = parse_patch(COPY_ONLY)
    assert fp.is_copy and not fp.is_rename
    assert apply_patch(tmp_path, COPY_ONLY) == ["copy.txt"]
    assert (tmp_path / "old.txt").read_text() == "hello\n"
    assert (tmp_path / "copy.txt").read_text() == "hello\n"


def test_copy_with_modify_leaves_source_content(tmp_path: Path):
    (tmp_path / "old.txt").write_text("old\n")
    diff = (
        "diff --git a/old.txt b/copy.txt\n"
        "copy from old.txt\n"
        "copy to copy.txt\n"
        "--- a/old.txt\n"
        "+++ b/copy.txt\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    assert apply_patch(tmp_path, diff) == ["copy.txt"]
    assert (tmp_path / "old.txt").read_text() == "old\n"
    assert (tmp_path / "copy.txt").read_text() == "new\n"


def test_rename_destination_occupied_is_atomic_rejection(tmp_path: Path):
    (tmp_path / "old.txt").write_text("a\n")
    (tmp_path / "new.txt").write_text("b\n")
    with pytest.raises(PatchError, match="already exists"):
        apply_patch(tmp_path, RENAME_ONLY)
    assert (tmp_path / "old.txt").read_text() == "a\n"
    assert (tmp_path / "new.txt").read_text() == "b\n"


def test_rename_context_mismatch_is_atomic_rejection(tmp_path: Path):
    (tmp_path / "old.txt").write_text("zzz\n")
    with pytest.raises(PatchError, match="does not match"):
        apply_patch(tmp_path, RENAME_MODIFY)
    assert (tmp_path / "old.txt").read_text() == "zzz\n"
    assert not (tmp_path / "new.txt").exists()


def test_rename_missing_source_is_rejection(tmp_path: Path):
    with pytest.raises(PatchError, match="not a file"):
        apply_patch(tmp_path, RENAME_ONLY)
    assert not (tmp_path / "new.txt").exists()


def test_rename_destination_traversal_rejected(tmp_path: Path):
    (tmp_path / "old.txt").write_text("a\n")
    diff = (
        "diff --git a/old.txt b/../evil.txt\n"
        "rename from old.txt\n"
        "rename to ../evil.txt\n"
    )
    with pytest.raises(PatchError, match="unsafe path"):
        apply_patch(tmp_path, diff)
    assert (tmp_path / "old.txt").read_text() == "a\n"
    assert not (tmp_path.parent / "evil.txt").exists()
