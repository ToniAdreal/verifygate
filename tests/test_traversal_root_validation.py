"""Traversal-root validation (v1.58.0).

``sandbox.iter_candidate_files`` — the single traversal behind the
workspace copy, ``evidence.tree_hash``, and the repair loop's
prompt snapshot — coerced its root with a bare ``Path(...)``: a
non-path root crashed with a raw ``TypeError``, an empty string
silently traversed the *current directory*, and a regular file
raised ``NotADirectoryError`` instead of the one
``FileNotFoundError`` every other path raises. Every production
caller happened to validate first; the primitive now validates
its own root through ``validate_candidate_dir``, eagerly at call
time even though it returns an iterator.
"""

import pytest

from verifygate import sandbox
from verifygate.evidence import tree_hash
from verifygate.repair import _candidate_files
from verifygate.sandbox import iter_candidate_files


def _candidate(tmp_path):
    root = tmp_path / "cand"
    root.mkdir()
    (root / "a.txt").write_text("hello\n")
    (root / "b.py").write_text("x = 1\n")
    return root


# -- genuine roots still traverse ----------------------------------------------


def test_str_root_traverses(tmp_path):
    root = _candidate(tmp_path)
    rels = [rel for rel, _src in iter_candidate_files(str(root))]
    assert rels == ["a.txt", "b.py"]


def test_path_root_traverses(tmp_path):
    root = _candidate(tmp_path)
    rels = [rel for rel, _src in iter_candidate_files(root)]
    assert rels == ["a.txt", "b.py"]


def test_dot_root_traverses_current_directory_when_named(tmp_path, monkeypatch):
    # "." is a legitimate, explicitly named directory: only the
    # *empty* string's silent aliasing of it was the bug.
    _candidate(tmp_path)
    monkeypatch.chdir(tmp_path / "cand")
    rels = [rel for rel, _src in iter_candidate_files(".")]
    assert rels == ["a.txt", "b.py"]


def test_traversal_containment_unchanged(tmp_path):
    root = _candidate(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("host secret\n")
    (root / "inside_link.txt").symlink_to(root / "a.txt")
    (root / "outside_link.txt").symlink_to(outside)
    sub = root / "sub"
    sub.mkdir()
    (sub / "c.txt").write_text("nested\n")
    (root / "dir_link").symlink_to(sub)
    rels = [rel for rel, _src in iter_candidate_files(root)]
    assert rels == ["a.txt", "b.py", "inside_link.txt", "sub/c.txt"]


# -- non-path roots: one ValueError, raised by the call itself -----------------


@pytest.mark.parametrize("bad_root", [
    None, 42, True, 2.5, b"cand", ["cand"], {"cand": 1},
])
def test_non_path_root_is_a_value_error_at_call_time(tmp_path, bad_root):
    _candidate(tmp_path)
    # No list(): the validation is eager, so the call itself —
    # not the first iteration — raises, and it is a ValueError,
    # never the raw TypeError Path(...) used to raise.
    with pytest.raises(ValueError):
        iter_candidate_files(bad_root)


def test_empty_root_never_means_current_directory(tmp_path, monkeypatch):
    _candidate(tmp_path)
    monkeypatch.chdir(tmp_path / "cand")
    with pytest.raises(ValueError):
        iter_candidate_files("")
    # The pre-fix shape silently yielded this directory's files.


def test_over_long_root_is_a_value_error_at_call_time(tmp_path):
    root = _candidate(tmp_path)
    long_root = str(root) + "/x" * sandbox.MAX_CANDIDATE_DIR_CHARS
    with pytest.raises(ValueError, match="root"):
        iter_candidate_files(long_root)


# -- well-formed paths that do not name a directory ----------------------------


def test_missing_root_is_file_not_found_at_call_time(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(FileNotFoundError):
        iter_candidate_files(missing)
    assert not missing.exists()


def test_file_root_is_file_not_found_not_not_a_directory(tmp_path):
    root = _candidate(tmp_path)
    with pytest.raises(FileNotFoundError):
        iter_candidate_files(root / "a.txt")


def test_unstatable_root_is_file_not_found(tmp_path):
    # A component over the filesystem's name limit cannot be
    # stat'ed; validate_candidate_dir judges it not-a-directory.
    root = tmp_path / ("a" * 300)
    with pytest.raises(FileNotFoundError):
        iter_candidate_files(root)


# -- the consumers through the primitive still work -----------------------------


def test_consumers_still_work(tmp_path):
    root = _candidate(tmp_path)
    assert sandbox.check_candidate_bounds(root) == (2, 12)
    ws = sandbox.prepare_workspace(root)
    try:
        assert (ws / "a.txt").read_text() == "hello\n"
    finally:
        import shutil
        shutil.rmtree(ws)
    assert tree_hash(root)
    assert _candidate_files(root) == {
        "a.txt": "hello\n", "b.py": "x = 1\n"}


def test_consumer_entry_points_reject_bad_roots_as_before(tmp_path):
    # tree_hash already validated through check_candidate_bounds;
    # its judgement is unchanged now that the traversal validates
    # too (the same ValueError / FileNotFoundError pair).
    with pytest.raises(ValueError):
        tree_hash(None)
    with pytest.raises(FileNotFoundError):
        tree_hash(tmp_path / "missing")
