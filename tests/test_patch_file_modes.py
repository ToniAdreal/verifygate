"""Git file-type mode validation (v1.92.0).

A git mode line was honoured for its permission bits only:
any octal value was masked with ``& 0o777`` at commit, so a
mode naming a non-regular file was silently mangled into a
regular one — a ``new file mode 120000`` (symlink) creation
produced a regular file holding the link target as its text
with mode ``000``, and gitlink (``160000``) and directory
(``040000``) modes did the same. A malformed (non-octal)
mode token was silently dropped and the patch applied
without its mode line at all. The patched tree was not the
tree the diff specified, yet the checks judged it as if it
were the model's fix. Mode lines now validate at parse
time: only regular-file modes (type ``100000``) are
accepted, with their permission bits honoured exactly as
before; anything else is a clean ``PatchError`` rejection
that the repair loop feeds back like any other
inapplicable diff, and the tree is left untouched.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from verifygate.llm import LLMAdapter
from verifygate.mock_llm import MockLLMAdapter
from verifygate.patch import PatchError, apply_patch, parse_patch
from verifygate.repair import run_repair
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup

APP = Path(__file__).resolve().parent.parent


def _create(rel: str, text: str = "content", mode: str = "100644") -> str:
    return (
        f"diff --git a/{rel} b/{rel}\n"
        f"new file mode {mode}\n"
        f"--- /dev/null\n"
        f"+++ b/{rel}\n"
        f"@@ -0,0 +1 @@\n"
        f"+{text}\n"
    )


def _mode_only(rel: str, old: str, new: str) -> str:
    return (
        f"diff --git a/{rel} b/{rel}\n"
        f"old mode {old}\n"
        f"new mode {new}\n"
    )


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def test_symlink_creation_is_rejected(tmp_path: Path) -> None:
    diff = _create("link.txt", "a.txt", mode="120000")
    with pytest.raises(PatchError, match="120000.*symlink"):
        parse_patch(diff)
    with pytest.raises(PatchError, match="symlink"):
        apply_patch(tmp_path, diff)
    assert _tree(tmp_path) == {}


def test_gitlink_creation_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PatchError, match="160000.*gitlink"):
        apply_patch(tmp_path, _create("sub", "abc123", mode="160000"))
    assert _tree(tmp_path) == {}


def test_directory_mode_creation_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PatchError, match="040000.*directory"):
        apply_patch(tmp_path, _create("z", "hi", mode="040000"))
    assert _tree(tmp_path) == {}


def test_unknown_file_type_mode_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PatchError, match="unsupported mode 777777"):
        apply_patch(tmp_path, _create("y.txt", "hi", mode="777777"))
    assert _tree(tmp_path) == {}


def test_mode_only_retype_to_symlink_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("hello\n")
    target.chmod(0o644)
    with pytest.raises(PatchError, match="symlink"):
        apply_patch(tmp_path, _mode_only("a.txt", "100644", "120000"))
    assert target.read_bytes() == b"hello\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_modification_with_symlink_new_mode_is_rejected(
    tmp_path: Path,
) -> None:
    target = tmp_path / "a.txt"
    target.write_text("hello\n")
    diff = (
        "diff --git a/a.txt b/a.txt\n"
        "old mode 100644\n"
        "new mode 120000\n"
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1 +1 @@\n"
        "-hello\n"
        "+goodbye\n"
    )
    with pytest.raises(PatchError, match="symlink"):
        apply_patch(tmp_path, diff)
    assert target.read_bytes() == b"hello\n"


def test_malformed_mode_token_is_rejected(tmp_path: Path) -> None:
    # Before the fix the unparseable token was silently dropped
    # and the creation applied without any mode at all.
    with pytest.raises(PatchError, match="malformed mode"):
        apply_patch(tmp_path, _create("x.txt", "hi", mode="banana"))
    assert _tree(tmp_path) == {}


def test_malformed_new_mode_token_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("hello\n")
    with pytest.raises(PatchError, match="malformed mode"):
        apply_patch(tmp_path, _mode_only("a.txt", "100644", "xyz"))
    assert target.read_bytes() == b"hello\n"


def test_non_regular_mode_in_second_file_is_atomic(
    tmp_path: Path,
) -> None:
    target = tmp_path / "keep.txt"
    target.write_text("keep\n")
    modify = (
        "diff --git a/keep.txt b/keep.txt\n"
        "--- a/keep.txt\n"
        "+++ b/keep.txt\n"
        "@@ -1 +1 @@\n"
        "-keep\n"
        "+changed\n"
    )
    diff = modify + _create("link.txt", "keep.txt", mode="120000")
    with pytest.raises(PatchError, match="symlink"):
        apply_patch(tmp_path, diff)
    assert target.read_bytes() == b"keep\n"
    assert _tree(tmp_path) == {"keep.txt": b"keep\n"}


def test_regular_modes_still_apply(tmp_path: Path) -> None:
    apply_patch(tmp_path, _create("run.sh", "echo hi", mode="100755"))
    assert stat.S_IMODE((tmp_path / "run.sh").stat().st_mode) == 0o755
    apply_patch(tmp_path, _create("plain.txt", "hi", mode="100644"))
    mode = stat.S_IMODE((tmp_path / "plain.txt").stat().st_mode)
    assert mode & 0o111 == 0


def test_regular_mode_permission_bits_are_kept(tmp_path: Path) -> None:
    # The rule is about the file *type*, not an exact permission
    # pair: a 100664 regular mode keeps its bits, as before.
    apply_patch(tmp_path, _create("g.txt", "hi", mode="100664"))
    assert stat.S_IMODE((tmp_path / "g.txt").stat().st_mode) == 0o664


def test_mode_only_regular_change_still_applies(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("hello\n")
    target.chmod(0o644)
    apply_patch(tmp_path, _mode_only("a.txt", "100644", "100755"))
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert target.read_bytes() == b"hello\n"


def test_parse_records_regular_mode_unchanged() -> None:
    (fp,) = parse_patch(_create("run.sh", "echo hi", mode="100755"))
    assert fp.new_mode == 0o100755


class _SymlinkFirstStub(LLMAdapter):
    """Proposes a symlink creation first, then the mock's real fix."""

    name = "symlink-first-stub"

    def __init__(self) -> None:
        self.calls = 0
        self.feedback: list = []
        self._mock = MockLLMAdapter()

    def generate_patch(self, brief, failing_evidence=None):
        self.calls += 1
        self.feedback.append(
            (failing_evidence or {}).get("patch_error"))
        if self.calls == 1:
            return _create("link.txt", "stringtools.py", mode="120000")
        return self._mock.generate_patch(brief, failing_evidence)

    def explain_failure(self, check_results):
        return self._mock.explain_failure(check_results)


def test_repair_loop_feeds_back_symlink_rejection_and_repairs() -> None:
    spec = load_spec(APP / "samples" / "spec.json")
    before = (APP / "samples" / "failing" / "stringtools.py").read_bytes()
    stub = _SymlinkFirstStub()
    report = run_repair(
        spec=spec,
        candidate_dir=APP / "samples" / "failing",
        mode="judge",
        llm=stub,
        references=MockReferenceLookup(),
        spec_label="samples/spec.json",
        candidate_label="samples/failing",
        max_rounds=3,
    )
    assert report["final_verdict"] == "PASS"
    assert report["rounds_used"] == 2
    # The rejection was fed back to the model on the second call.
    assert stub.feedback[0] is None
    assert "unsupported mode 120000" in (stub.feedback[1] or "")
    assert report["failed_patch"]["error_kind"] == "patch_rejected"
    assert "120000" in report["failed_patch"]["error"]
    assert (APP / "samples" / "failing"
            / "stringtools.py").read_bytes() == before
