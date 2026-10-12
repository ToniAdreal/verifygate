"""Trailing-newline fidelity of the patch applier (v1.82.0).

Before the fix, the parser dropped the ``\\ No newline at end of
file`` marker that follows a hunk's *final* line (it arrives only
once the hunk's line counts are exhausted), and application
rewrote the file's trailing-newline state from the surviving
markers alone. A patch whose new side must end without a newline
silently gained one, and a patch that touched only the first
lines of a file without a trailing newline added one at its end
too — the patched tree was not the tree the diff specified.
"""

from pathlib import Path

from verifygate.patch import apply_patch, parse_patch

MARKER = "\\ No newline at end of file"


def _write(root: Path, name: str, data: bytes) -> Path:
    p = root / name
    p.write_bytes(data)
    return p


def test_parser_captures_marker_after_final_added_line():
    (fp,) = parse_patch(
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,1 @@\n"
        "-hello\n+hello!\n" + MARKER + "\n"
    )
    (hunk,) = fp.hunks
    assert hunk.new_no_trailing_newline is True
    assert hunk.old_no_trailing_newline is False


def test_parser_captures_marker_after_final_context_line():
    (fp,) = parse_patch(
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,2 @@\n"
        "-hello\n+hello!\n world\n" + MARKER + "\n"
    )
    (hunk,) = fp.hunks
    assert hunk.new_no_trailing_newline is True
    assert hunk.old_no_trailing_newline is True


def test_parser_marker_after_removed_line_is_old_side_only():
    (fp,) = parse_patch(
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,1 @@\n"
        " hello\n-world\n" + MARKER + "\n"
    )
    (hunk,) = fp.hunks
    assert hunk.old_no_trailing_newline is True
    assert hunk.new_no_trailing_newline is False


def test_parser_marker_mid_hunk_still_captured():
    # The marker after a removed line, followed by the added line:
    # this shape the in-loop handling has always seen.
    (fp,) = parse_patch(
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,1 @@\n"
        "-world\n" + MARKER + "\n+world!\n"
    )
    (hunk,) = fp.hunks
    assert hunk.old_no_trailing_newline is True
    assert hunk.new_no_trailing_newline is False


def test_modify_last_line_keeps_missing_trailing_newline(tmp_path):
    p = _write(tmp_path, "a.txt", b"hello\nworld")
    apply_patch(tmp_path, (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,2 @@\n"
        " hello\n-world\n" + MARKER + "\n+world!\n" + MARKER + "\n"
    ))
    assert p.read_bytes() == b"hello\nworld!"


def test_modify_last_line_can_add_trailing_newline(tmp_path):
    p = _write(tmp_path, "a.txt", b"hello\nworld")
    apply_patch(tmp_path, (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,2 @@\n"
        " hello\n-world\n" + MARKER + "\n+world\n"
    ))
    assert p.read_bytes() == b"hello\nworld\n"


def test_modify_last_line_can_remove_trailing_newline(tmp_path):
    p = _write(tmp_path, "a.txt", b"hello\nworld\n")
    apply_patch(tmp_path, (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,2 @@\n"
        " hello\n-world\n+world!\n" + MARKER + "\n"
    ))
    assert p.read_bytes() == b"hello\nworld!"


def test_patch_not_touching_end_preserves_missing_trailing_newline(tmp_path):
    p = _write(tmp_path, "a.txt", b"hello\nworld")
    apply_patch(tmp_path, (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,1 @@\n-hello\n+hello!\n"
    ))
    assert p.read_bytes() == b"hello!\nworld"


def test_patch_not_touching_end_preserves_trailing_newline(tmp_path):
    p = _write(tmp_path, "a.txt", b"hello\nworld\n")
    apply_patch(tmp_path, (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,1 @@\n-hello\n+hello!\n"
    ))
    assert p.read_bytes() == b"hello!\nworld\n"


def test_context_final_marker_keeps_missing_trailing_newline(tmp_path):
    p = _write(tmp_path, "a.txt", b"hello\nworld")
    apply_patch(tmp_path, (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,2 @@\n"
        "-hello\n+hello!\n world\n" + MARKER + "\n"
    ))
    assert p.read_bytes() == b"hello!\nworld"


def test_creation_honours_marker(tmp_path):
    apply_patch(tmp_path, (
        "--- /dev/null\n+++ b/b.txt\n@@ -0,0 +1,2 @@\n"
        "+one\n+two\n" + MARKER + "\n"
    ))
    assert (tmp_path / "b.txt").read_bytes() == b"one\ntwo"


def test_creation_without_marker_ends_with_newline(tmp_path):
    apply_patch(tmp_path, (
        "--- /dev/null\n+++ b/b.txt\n@@ -0,0 +1,2 @@\n+one\n+two\n"
    ))
    assert (tmp_path / "b.txt").read_bytes() == b"one\ntwo\n"


def test_deleting_unterminated_last_line_leaves_newline(tmp_path):
    p = _write(tmp_path, "a.txt", b"hello\nworld")
    apply_patch(tmp_path, (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,1 @@\n"
        " hello\n-world\n" + MARKER + "\n"
    ))
    assert p.read_bytes() == b"hello\n"


def test_append_at_end_decides_trailing_state(tmp_path):
    p = _write(tmp_path, "a.txt", b"hello\nworld")
    apply_patch(tmp_path, (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,3 @@\n"
        " hello\n world\n" + MARKER + "\n+again\n" + MARKER + "\n"
    ))
    assert p.read_bytes() == b"hello\nworld\nagain"
