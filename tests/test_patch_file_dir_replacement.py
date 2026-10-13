"""File/directory replacement (v1.91.0).

A real ``git diff`` can replace a file with a directory
(delete ``x``, create ``x/y``) or a directory with a file
(delete every file under ``x``, create ``x``). Both shapes
were clean rejections the applier could not honour: the
creation beneath a still-present file died as an "unusable
path", and the creation at a still-present directory as
"already exists" — in git's canonical path order the
directory-to-file creation is even emitted before the
deletions that make room for it. Staging now judges a
creation against the staged world plus the set of paths the
same diff deletes, the staged-conflict check exempts exactly
the replacement pairs, and the commit lands in phases
(deletions, replaced-directory removal, writes). A directory
with any entry the diff does not delete keeps the rejection,
and the tree is left byte-for-byte untouched.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from verifygate.patch import PatchError, apply_patch


def _create(rel: str, text: str = "content", mode: str = "100644") -> str:
    lines = text.split("\n")
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{rel} b/{rel}\n"
        f"new file mode {mode}\n"
        f"--- /dev/null\n"
        f"+++ b/{rel}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        f"{body}"
    )


def _delete(rel: str, text: str) -> str:
    lines = text.split("\n")
    body = "".join(f"-{line}\n" for line in lines)
    return (
        f"diff --git a/{rel} b/{rel}\n"
        f"deleted file mode 100644\n"
        f"--- a/{rel}\n"
        f"+++ /dev/null\n"
        f"@@ -1,{len(lines)} +0,0 @@\n"
        f"{body}"
    )


def _tree(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        out[rel] = path.read_bytes() if path.is_file() else b"<dir>"
    return out


def test_file_replaced_by_directory_applies(tmp_path: Path) -> None:
    (tmp_path / "x").write_text("old\n")
    changed = apply_patch(tmp_path, _delete("x", "old") + _create("x/y.txt"))
    assert changed == ["x", "x/y.txt"]
    assert _tree(tmp_path) == {"x": b"<dir>", "x/y.txt": b"content\n"}


def test_file_replaced_by_directory_creation_first_applies(
    tmp_path: Path,
) -> None:
    # The proposal's order does not matter: the deletion named
    # later in the same diff still makes room for the creation.
    (tmp_path / "x").write_text("old\n")
    changed = apply_patch(tmp_path, _create("x/y.txt") + _delete("x", "old"))
    assert changed == ["x/y.txt", "x"]
    assert _tree(tmp_path) == {"x": b"<dir>", "x/y.txt": b"content\n"}


def test_file_replaced_by_two_files_beneath_it(tmp_path: Path) -> None:
    (tmp_path / "x").write_text("old\n")
    diff = _delete("x", "old") + _create("x/a.txt", "a") + _create("x/b.txt", "b")
    apply_patch(tmp_path, diff)
    assert _tree(tmp_path) == {
        "x": b"<dir>", "x/a.txt": b"a\n", "x/b.txt": b"b\n"}


def test_directory_replaced_by_file_applies_git_order(
    tmp_path: Path,
) -> None:
    # Git's canonical order emits the creation of x before the
    # deletion of x/y.txt; the replacement must still apply.
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "y.txt").write_text("inner\n")
    diff = _create("x") + _delete("x/y.txt", "inner")
    changed = apply_patch(tmp_path, diff)
    assert changed == ["x", "x/y.txt"]
    assert _tree(tmp_path) == {"x": b"content\n"}


def test_directory_replaced_by_file_deletions_first(
    tmp_path: Path,
) -> None:
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "y.txt").write_text("inner\n")
    (tmp_path / "x" / "z.txt").write_text("inner2\n")
    diff = (
        _delete("x/y.txt", "inner") + _delete("x/z.txt", "inner2")
        + _create("x", "flat")
    )
    apply_patch(tmp_path, diff)
    assert _tree(tmp_path) == {"x": b"flat\n"}


def test_directory_with_undeleted_entry_still_rejected(
    tmp_path: Path,
) -> None:
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "a.txt").write_text("a\n")
    (tmp_path / "x" / "b.txt").write_text("b\n")
    before = _tree(tmp_path)
    with pytest.raises(PatchError, match="already exists"):
        apply_patch(tmp_path, _delete("x/a.txt", "a") + _create("x"))
    assert _tree(tmp_path) == before


def test_directory_with_symlink_entry_still_rejected(
    tmp_path: Path,
) -> None:
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "a.txt").write_text("a\n")
    os.symlink("/etc/hostname", tmp_path / "x" / "link")
    before = _tree(tmp_path)
    with pytest.raises(PatchError, match="already exists"):
        apply_patch(tmp_path, _delete("x/a.txt", "a") + _create("x"))
    assert _tree(tmp_path) == before


def test_creation_beneath_live_file_still_rejected(tmp_path: Path) -> None:
    # No deletion in the diff: the v1.90.0 conflict rule stands.
    (tmp_path / "x").write_text("old\n")
    before = _tree(tmp_path)
    with pytest.raises(PatchError, match="unusable path"):
        apply_patch(tmp_path, _create("x/y.txt"))
    assert _tree(tmp_path) == before


def test_two_creations_sharing_a_path_still_conflict(
    tmp_path: Path,
) -> None:
    with pytest.raises(PatchError, match="cannot both be written"):
        apply_patch(tmp_path, _create("x") + _create("x/y.txt"))
    assert _tree(tmp_path) == {}


def test_replacement_with_failing_deletion_is_atomic(
    tmp_path: Path,
) -> None:
    # The deletion the replacement relies on must itself stage:
    # a context mismatch fails the whole diff, tree untouched.
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "y.txt").write_text("real\n")
    before = _tree(tmp_path)
    with pytest.raises(PatchError, match="does not match"):
        apply_patch(tmp_path, _create("x") + _delete("x/y.txt", "wrong"))
    assert _tree(tmp_path) == before


def test_created_file_beneath_replaced_file_keeps_mode(
    tmp_path: Path,
) -> None:
    (tmp_path / "x").write_text("old\n")
    diff = _delete("x", "old") + _create("x/run.sh", "echo hi", mode="100755")
    apply_patch(tmp_path, diff)
    mode = stat.S_IMODE((tmp_path / "x" / "run.sh").stat().st_mode)
    assert mode == 0o755


def test_modify_beneath_replaced_directory_still_rejected(
    tmp_path: Path,
) -> None:
    # Creating x as a file while modifying a file under it is
    # not a replacement: the descendant is not a deletion.
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "y.txt").write_text("inner\n")
    modify = (
        "diff --git a/x/y.txt b/x/y.txt\n"
        "--- a/x/y.txt\n"
        "+++ b/x/y.txt\n"
        "@@ -1 +1 @@\n"
        "-inner\n"
        "+changed\n"
    )
    before = _tree(tmp_path)
    with pytest.raises(PatchError):
        apply_patch(tmp_path, _create("x") + modify)
    assert _tree(tmp_path) == before
