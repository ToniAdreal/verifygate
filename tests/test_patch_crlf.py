"""Line-ending fidelity of the patch applier (v1.83.0).

Before the fix, lines were read with ``splitlines()`` and always
written back joined by ``\\n``, so patching one line of a CRLF
file silently rewrote every line ending in it, and a mode-only
commit on a CRLF file did the same while changing no content at
all. A modified file now keeps its dominant line ending; a
created file takes the patch text's own ending.
"""

from pathlib import Path

from verifygate.patch import _dominant_line_ending, apply_patch

LF_DIFF = (
    "--- a/a.txt\n+++ b/a.txt\n@@ -1,3 +1,3 @@\n"
    " line1\n-line2\n+CHANGED\n line3\n"
)


def _write(root: Path, name: str, data: bytes) -> Path:
    p = root / name
    p.write_bytes(data)
    return p


def test_dominant_ending_rules():
    assert _dominant_line_ending(b"a\r\nb\r\n") == "\r\n"
    assert _dominant_line_ending(b"a\nb\n") == "\n"
    assert _dominant_line_ending(b"a\r\nb\n") == "\r\n"  # tie: CRLF wins
    assert _dominant_line_ending(b"a\r\nb\nc\n") == "\n"
    assert _dominant_line_ending(b"no breaks") == "\n"
    assert _dominant_line_ending(b"") == "\n"


def test_crlf_file_stays_crlf_after_modify(tmp_path):
    _write(tmp_path, "a.txt", b"line1\r\nline2\r\nline3\r\n")
    assert apply_patch(tmp_path, LF_DIFF) == ["a.txt"]
    assert (tmp_path / "a.txt").read_bytes() == (
        b"line1\r\nCHANGED\r\nline3\r\n")


def test_lf_file_stays_lf_after_modify(tmp_path):
    _write(tmp_path, "a.txt", b"line1\nline2\nline3\n")
    apply_patch(tmp_path, LF_DIFF)
    assert (tmp_path / "a.txt").read_bytes() == b"line1\nCHANGED\nline3\n"


def test_crlf_diff_applied_to_crlf_file_stays_crlf(tmp_path):
    _write(tmp_path, "a.txt", b"line1\r\nline2\r\nline3\r\n")
    diff = LF_DIFF.replace("\n", "\r\n")
    apply_patch(tmp_path, diff)
    assert (tmp_path / "a.txt").read_bytes() == (
        b"line1\r\nCHANGED\r\nline3\r\n")


def test_crlf_file_without_trailing_newline_keeps_both(tmp_path):
    _write(tmp_path, "a.txt", b"line1\r\nline2\r\nline3")
    diff = (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,1 @@\n"
        "-line1\n+CHANGED\n"
    )
    apply_patch(tmp_path, diff)
    assert (tmp_path / "a.txt").read_bytes() == (
        b"CHANGED\r\nline2\r\nline3")


def test_mode_only_patch_on_crlf_file_is_byte_identical(tmp_path):
    original = b"echo hi\r\necho bye\r\n"
    _write(tmp_path, "run.sh", original)
    diff = (
        "diff --git a/run.sh b/run.sh\n"
        "old mode 100644\nnew mode 100755\n"
    )
    apply_patch(tmp_path, diff)
    assert (tmp_path / "run.sh").read_bytes() == original


def test_created_file_takes_crlf_from_crlf_patch(tmp_path):
    diff = (
        "diff --git a/new.txt b/new.txt\r\n"
        "new file mode 100644\r\n"
        "--- /dev/null\r\n+++ b/new.txt\r\n"
        "@@ -0,0 +1,2 @@\r\n+one\r\n+two\r\n"
    )
    apply_patch(tmp_path, diff)
    assert (tmp_path / "new.txt").read_bytes() == b"one\r\ntwo\r\n"


def test_created_file_takes_lf_from_lf_patch(tmp_path):
    diff = (
        "diff --git a/new.txt b/new.txt\n"
        "new file mode 100644\n"
        "--- /dev/null\n+++ b/new.txt\n"
        "@@ -0,0 +1,2 @@\n+one\n+two\n"
    )
    apply_patch(tmp_path, diff)
    assert (tmp_path / "new.txt").read_bytes() == b"one\ntwo\n"


def test_mixed_file_follows_dominant_ending(tmp_path):
    _write(tmp_path, "a.txt", b"line1\r\nline2\r\nline3\n")
    apply_patch(tmp_path, LF_DIFF)
    assert (tmp_path / "a.txt").read_bytes() == (
        b"line1\r\nCHANGED\r\nline3\r\n")
