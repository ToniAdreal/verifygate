"""CR line-ending fidelity of the patch applier (v1.84.0).

Before the fix, ``_dominant_line_ending`` knew only CRLF and LF,
so a file using lone CR breaks (the classic Mac ending) fell
through to LF: patching one line rewrote every ending in it, and
``load()`` read its trailing CR as "no trailing newline", so the
file also lost its final break. A CR file now keeps CR endings
and its trailing state, and a file created by a CR-carried diff
is a CR file.
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


def test_dominant_ending_cr_rules():
    assert _dominant_line_ending(b"a\rb\r") == "\r"
    assert _dominant_line_ending(b"a\rb\rc\n") == "\r"  # CR majority
    assert _dominant_line_ending(b"a\rb\n") == "\r"  # tie: CR beats LF
    assert _dominant_line_ending(b"a\rb\nc\n") == "\n"  # LF majority
    assert _dominant_line_ending(b"a\r\nb\r") == "\r\n"  # CRLF wins ties


def test_cr_file_stays_cr_after_modify(tmp_path):
    _write(tmp_path, "a.txt", b"line1\rline2\rline3\r")
    assert apply_patch(tmp_path, LF_DIFF) == ["a.txt"]
    assert (tmp_path / "a.txt").read_bytes() == (
        b"line1\rCHANGED\rline3\r")


def test_cr_file_without_trailing_break_keeps_it_missing(tmp_path):
    _write(tmp_path, "a.txt", b"line1\rline2\rline3")
    diff = ("--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,1 @@\n"
            "-line1\n+CHANGED\n")
    assert apply_patch(tmp_path, diff) == ["a.txt"]
    assert (tmp_path / "a.txt").read_bytes() == (
        b"CHANGED\rline2\rline3")


def test_mode_only_patch_on_cr_file_is_byte_identical(tmp_path):
    original = b"echo hi\recho bye\r"
    _write(tmp_path, "a.txt", original)
    diff = ("diff --git a/a.txt b/a.txt\n"
            "old mode 100644\nnew mode 100755\n")
    assert apply_patch(tmp_path, diff) == ["a.txt"]
    assert (tmp_path / "a.txt").read_bytes() == original


def test_created_file_takes_cr_from_cr_diff(tmp_path):
    diff = (
        "diff --git a/new.txt b/new.txt\r"
        "new file mode 100644\r"
        "--- /dev/null\r+++ b/new.txt\r"
        "@@ -0,0 +1,2 @@\r+one\r+two\r"
    )
    assert apply_patch(tmp_path, diff) == ["new.txt"]
    assert (tmp_path / "new.txt").read_bytes() == b"one\rtwo\r"


def test_mixed_cr_lf_file_follows_dominant_cr(tmp_path):
    _write(tmp_path, "a.txt", b"line1\rline2\rline3\n")
    assert apply_patch(tmp_path, LF_DIFF) == ["a.txt"]
    assert (tmp_path / "a.txt").read_bytes() == (
        b"line1\rCHANGED\rline3\r")
