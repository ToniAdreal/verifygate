"""Patch path fidelity: spaces and git-quoted paths (v1.85.0).

Before this, ``_clean_path`` cut a header path at its first space,
so a patch for ``my file.txt`` was parsed as a patch for ``my``:
a modification was rejected against a file that was never the
target, and a creation silently wrote a file named ``my`` — the
patched tree was not the tree the diff specified. Git's C-style
quoted paths (its default for non-ASCII names, e.g.
``"b/caf\\303\\251.txt"`` for ``b/café.txt``) were taken literally,
quotes and octal escapes included, so a real ``git diff`` for such
a file could never apply at all.
"""

from pathlib import Path

import pytest

from verifygate.patch import PatchError, apply_patch, parse_patch


def test_parse_keeps_spaces_in_header_paths():
    diff = (
        "--- a/my file.txt\n"
        "+++ b/my file.txt\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    (fp,) = parse_patch(diff)
    assert fp.old_path == "my file.txt"
    assert fp.new_path == "my file.txt"


def test_modify_spaced_file(tmp_path: Path):
    (tmp_path / "my file.txt").write_text("one\ntwo\nthree\n")
    diff = (
        "diff --git a/my file.txt b/my file.txt\n"
        "--- a/my file.txt\t\n"
        "+++ b/my file.txt\t\n"
        "@@ -1,3 +1,3 @@\n"
        " one\n"
        "-two\n"
        "+TWO\n"
        " three\n"
    )
    assert apply_patch(tmp_path, diff) == ["my file.txt"]
    assert (tmp_path / "my file.txt").read_text() == "one\nTWO\nthree\n"


def test_create_spaced_file_writes_the_named_file(tmp_path: Path):
    diff = (
        "--- /dev/null\n"
        "+++ b/my file.txt\n"
        "@@ -0,0 +1,2 @@\n"
        "+hello\n"
        "+world\n"
    )
    assert apply_patch(tmp_path, diff) == ["my file.txt"]
    assert (tmp_path / "my file.txt").read_text() == "hello\nworld\n"
    assert not (tmp_path / "my").exists()


def test_tab_timestamp_still_separates(tmp_path: Path):
    (tmp_path / "f.txt").write_text("old\n")
    diff = (
        "--- a/f.txt\t2026-10-10 12:00:00 +0000\n"
        "+++ b/f.txt\t2026-10-10 12:01:00 +0000\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    (fp,) = parse_patch(diff)
    assert (fp.old_path, fp.new_path) == ("f.txt", "f.txt")
    assert apply_patch(tmp_path, diff) == ["f.txt"]


def test_git_quoted_non_ascii_path_parses_and_applies(tmp_path: Path):
    (tmp_path / "café.txt").write_text("old\n")
    diff = (
        'diff --git "a/caf\\303\\251.txt" "b/caf\\303\\251.txt"\n'
        '--- "a/caf\\303\\251.txt"\n'
        '+++ "b/caf\\303\\251.txt"\n'
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    (fp,) = parse_patch(diff)
    assert (fp.old_path, fp.new_path) == ("café.txt", "café.txt")
    assert apply_patch(tmp_path, diff) == ["café.txt"]
    assert (tmp_path / "café.txt").read_text() == "new\n"


def test_git_quoted_path_with_space_and_tab(tmp_path: Path):
    name = "my\tcafé file.txt"
    (tmp_path / name).write_text("old\n")
    # git C-quoting: tab is \t, the é is \303\251, spaces stay raw.
    diff = (
        'diff --git "a/my\\tcaf\\303\\251 file.txt" '
        '"b/my\\tcaf\\303\\251 file.txt"\n'
        '--- "a/my\\tcaf\\303\\251 file.txt"\n'
        '+++ "b/my\\tcaf\\303\\251 file.txt"\n'
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    assert apply_patch(tmp_path, diff) == [name]
    assert (tmp_path / name).read_text() == "new\n"


def test_mode_only_diff_on_spaced_file(tmp_path: Path):
    target = tmp_path / "run script.sh"
    target.write_text("echo hi\n")
    diff = (
        "diff --git a/run script.sh b/run script.sh\n"
        "old mode 100644\n"
        "new mode 100755\n"
    )
    assert apply_patch(tmp_path, diff) == ["run script.sh"]
    assert target.stat().st_mode & 0o777 == 0o755
    assert target.read_text() == "echo hi\n"


def test_mode_only_diff_on_quoted_file(tmp_path: Path):
    target = tmp_path / "café.sh"
    target.write_text("echo hi\n")
    diff = (
        'diff --git "a/caf\\303\\251.sh" "b/caf\\303\\251.sh"\n'
        "old mode 100644\n"
        "new mode 100755\n"
    )
    assert apply_patch(tmp_path, diff) == ["café.sh"]
    assert target.stat().st_mode & 0o777 == 0o755


def test_spaced_rename_via_header_pair(tmp_path: Path):
    (tmp_path / "old name.txt").write_text("old\n")
    diff = (
        "diff --git a/old name.txt b/new name.txt\n"
        "--- a/old name.txt\n"
        "+++ b/new name.txt\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    (fp,) = parse_patch(diff)
    assert (fp.old_path, fp.new_path) == ("old name.txt", "new name.txt")


def test_escaped_traversal_in_quoted_path_still_rejected(tmp_path: Path):
    diff = (
        '--- "a/..\\057evil.txt"\n'
        '+++ "b/..\\057evil.txt"\n'
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    # \057 is '/', so the decoded path is ../evil.txt.
    with pytest.raises(PatchError):
        apply_patch(tmp_path, diff)
    assert not (tmp_path.parent / "evil.txt").exists()
