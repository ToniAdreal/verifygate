"""Patch-applier root validation (v1.57.0).

``apply_patch`` coerced its root with a bare ``Path(...)``: a
non-path root crashed with a raw ``TypeError`` (not a
``PatchError``, so it escaped the repair loop's rejection
handling), an empty string silently meant the current directory,
and a creation diff silently materialised a missing root. The
root is now judged through ``sandbox.validate_candidate_dir``
like every other entry point's directory, before anything is
parsed, staged, or written.
"""

import pytest

from verifygate import sandbox
from verifygate.patch import PatchError, apply_patch

MODIFY_DIFF = """\
--- a/a.txt
+++ b/a.txt
@@ -1 +1 @@
-hello
+hello world
"""

CREATE_DIFF = """\
--- /dev/null
+++ b/new.txt
@@ -0,0 +1 @@
+hello
"""

CREATE_NESTED_DIFF = """\
--- /dev/null
+++ b/sub/dir/new.txt
@@ -0,0 +1 @@
+hello
"""


def _candidate(tmp_path):
    root = tmp_path / "cand"
    root.mkdir()
    (root / "a.txt").write_text("hello\n")
    return root


# -- genuine roots still apply ------------------------------------------------


def test_str_root_applies(tmp_path):
    root = _candidate(tmp_path)
    assert apply_patch(str(root), MODIFY_DIFF) == ["a.txt"]
    assert (root / "a.txt").read_text() == "hello world\n"


def test_creation_in_new_subdirectory_still_applies(tmp_path):
    # Directory creation *inside* a validated root is unchanged:
    # only the root itself may not spring into existence.
    root = _candidate(tmp_path)
    assert apply_patch(root, CREATE_NESTED_DIFF) == ["sub/dir/new.txt"]
    assert (root / "sub" / "dir" / "new.txt").read_text() == "hello\n"


# -- non-path roots: one ValueError, deliberately not a PatchError ------------


@pytest.mark.parametrize("bad_root", [
    None, 42, True, 2.5, ["cand"], {"cand": 1},
])
def test_non_path_root_is_a_value_error(tmp_path, bad_root):
    root = _candidate(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        apply_patch(bad_root, MODIFY_DIFF)
    # A malformed root is a usage error about the call, not a
    # rejection of the patch (the v1.53.0 override doctrine).
    assert not isinstance(excinfo.value, PatchError)
    assert (root / "a.txt").read_text() == "hello\n"


def test_empty_root_never_means_current_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        apply_patch("", CREATE_DIFF)
    # Nothing was written into the working directory the empty
    # string used to name.
    assert not (tmp_path / "new.txt").exists()


def test_over_long_root_is_a_value_error(tmp_path):
    root = _candidate(tmp_path)
    long_root = str(root) + "/x" * sandbox.MAX_CANDIDATE_DIR_CHARS
    with pytest.raises(ValueError, match="root"):
        apply_patch(long_root, MODIFY_DIFF)
    assert (root / "a.txt").read_text() == "hello\n"


# -- well-formed paths that do not name a directory ----------------------------


def test_missing_root_modify_is_file_not_found(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(FileNotFoundError):
        apply_patch(missing, MODIFY_DIFF)
    assert not missing.exists()


def test_missing_root_is_never_created(tmp_path):
    # The pre-fix probe: a creation diff materialised the root
    # itself (and the file inside it) wherever the path pointed.
    missing = tmp_path / "missing" / "deep"
    with pytest.raises(FileNotFoundError):
        apply_patch(missing, CREATE_DIFF)
    assert not (tmp_path / "missing").exists()


def test_file_root_is_file_not_found_and_untouched(tmp_path):
    f = tmp_path / "plain.txt"
    f.write_text("data\n")
    with pytest.raises(FileNotFoundError):
        apply_patch(f, CREATE_DIFF)
    with pytest.raises(FileNotFoundError):
        apply_patch(f, MODIFY_DIFF)
    assert f.read_text() == "data\n"


def test_missing_file_under_existing_root_stays_a_patch_error(tmp_path):
    # The root exists; the *target file* is what is missing —
    # that remains a PatchError rejection, exactly as before.
    root = _candidate(tmp_path)
    diff = MODIFY_DIFF.replace("a.txt", "ghost.txt")
    with pytest.raises(PatchError):
        apply_patch(root, diff)
    assert (root / "a.txt").read_text() == "hello\n"


# -- ordering: the call is judged before the proposal --------------------------


def test_bad_root_wins_over_bad_patch_text(tmp_path):
    with pytest.raises(ValueError) as excinfo:
        apply_patch(None, "this is not a diff at all")
    assert not isinstance(excinfo.value, PatchError)


def test_bad_override_wins_over_bad_root(tmp_path):
    with pytest.raises(ValueError, match="max_files"):
        apply_patch(None, MODIFY_DIFF, max_files="3")
