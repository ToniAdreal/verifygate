"""Binary patch blocks (v1.95.0).

A `GIT binary patch` block (git diff --binary) and the
`Binary files a/<path> and b/<path> differ` line (plain git
diff) carry no ---/+++ pair and no hunks, so the parser
produced no file patch for them: alone they failed
misleadingly as "no unified diff found", and inside a
multi-file diff they were silently dropped while the text
changes applied and reported success — the patched tree was
not the tree the diff specified. Both markers are now a
clean PatchError at parse time, so the whole diff fails
atomically and the repair loop feeds the rejection back.
"""

from pathlib import Path

import pytest

from verifygate.llm import LLMAdapter
from verifygate.patch import PatchError, apply_patch, parse_patch
from verifygate.repair import run_repair
from verifygate.spec import parse_spec
from verifygate.tavily import MockReferenceLookup

# Real `git diff --cached --binary` output shapes, captured from
# a scratch repo (text change + binary change in one diff).
BINARY_ONLY_GIT = """\
diff --git a/b.bin b/b.bin
index 8352675..2071727 100644
GIT binary patch
literal 3
KcmZSJ{0{&D6afhU

literal 3
KcmZQzWC8#H2LJ>B

"""

DIFFER_LINE_ONLY = """\
diff --git a/b.bin b/b.bin
index 8352675..2071727 100644
Binary files a/b.bin and b/b.bin differ
"""

TEXT_THEN_BINARY_GIT = """\
diff --git a/a.txt b/a.txt
index ce01362..3b18e51 100644
--- a/a.txt
+++ b/a.txt
@@ -1 +1 @@
-hello
+hello world
diff --git a/b.bin b/b.bin
index 8352675..2071727 100644
GIT binary patch
literal 3
KcmZSJ{0{&D6afhU

literal 3
KcmZQzWC8#H2LJ>B

"""

BINARY_THEN_TEXT_GIT = """\
diff --git a/b.bin b/b.bin
index 8352675..2071727 100644
GIT binary patch
literal 3
KcmZSJ{0{&D6afhU
diff --git a/a.txt b/a.txt
index ce01362..3b18e51 100644
--- a/a.txt
+++ b/a.txt
@@ -1 +1 @@
-hello
+hello world
"""

TEXT_THEN_DIFFER_LINE = """\
diff --git a/a.txt b/a.txt
index ce01362..3b18e51 100644
--- a/a.txt
+++ b/a.txt
@@ -1 +1 @@
-hello
+hello world
diff --git a/b.bin b/b.bin
index 8352675..2071727 100644
Binary files a/b.bin and b/b.bin differ
"""

BINARY_CREATE_GIT = """\
diff --git a/new.bin b/new.bin
new file mode 100644
index 0000000..2071727
GIT binary patch
literal 3
KcmZSJ{0{&D6afhU

"""

# A text patch whose content legitimately adds the marker text:
# the marker arrives as a "+" line inside a hunk, never as a
# top-level line, and must apply exactly as before.
MARKER_AS_CONTENT = """\
diff --git a/notes.txt b/notes.txt
index 1111111..2222222 100644
--- a/notes.txt
+++ b/notes.txt
@@ -1 +1,2 @@
 intro
+GIT binary patch
"""

DIFFER_AS_CONTENT = """\
diff --git a/notes.txt b/notes.txt
index 1111111..2222222 100644
--- a/notes.txt
+++ b/notes.txt
@@ -1 +1,2 @@
 intro
+Binary files a/x and b/y differ
"""


def _seed(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello\n")
    (tmp_path / "b.bin").write_bytes(b"\x00\x01\x02")


def test_binary_only_git_block_is_a_binary_patch_error():
    with pytest.raises(PatchError, match="binary patch.*b\\.bin"):
        parse_patch(BINARY_ONLY_GIT)


def test_differ_line_only_is_a_binary_patch_error():
    with pytest.raises(PatchError, match="binary patch"):
        parse_patch(DIFFER_LINE_ONLY)


def test_mixed_text_then_binary_is_rejected_atomically(tmp_path: Path):
    _seed(tmp_path)
    with pytest.raises(PatchError, match="binary patch"):
        apply_patch(tmp_path, TEXT_THEN_BINARY_GIT)
    # The text change may not land either: parse fails before
    # anything is staged or written.
    assert (tmp_path / "a.txt").read_text() == "hello\n"
    assert (tmp_path / "b.bin").read_bytes() == b"\x00\x01\x02"


def test_mixed_binary_then_text_is_rejected_atomically(tmp_path: Path):
    _seed(tmp_path)
    with pytest.raises(PatchError, match="binary patch"):
        apply_patch(tmp_path, BINARY_THEN_TEXT_GIT)
    assert (tmp_path / "a.txt").read_text() == "hello\n"
    assert (tmp_path / "b.bin").read_bytes() == b"\x00\x01\x02"


def test_mixed_differ_line_is_rejected_atomically(tmp_path: Path):
    _seed(tmp_path)
    with pytest.raises(PatchError, match="binary patch"):
        apply_patch(tmp_path, TEXT_THEN_DIFFER_LINE)
    assert (tmp_path / "a.txt").read_text() == "hello\n"
    assert (tmp_path / "b.bin").read_bytes() == b"\x00\x01\x02"


def test_binary_creation_is_rejected_and_creates_nothing(tmp_path: Path):
    with pytest.raises(PatchError, match="binary patch.*new\\.bin"):
        apply_patch(tmp_path, BINARY_CREATE_GIT)
    assert not (tmp_path / "new.bin").exists()


def test_marker_as_hunk_content_still_applies(tmp_path: Path):
    (tmp_path / "notes.txt").write_text("intro\n")
    assert apply_patch(tmp_path, MARKER_AS_CONTENT) == ["notes.txt"]
    assert (tmp_path / "notes.txt").read_text() == (
        "intro\nGIT binary patch\n")


def test_differ_line_as_hunk_content_still_applies(tmp_path: Path):
    (tmp_path / "notes.txt").write_text("intro\n")
    assert apply_patch(tmp_path, DIFFER_AS_CONTENT) == ["notes.txt"]
    assert (tmp_path / "notes.txt").read_text() == (
        "intro\nBinary files a/x and b/y differ\n")


class _MixedBinaryStub(LLMAdapter):
    """Always proposes a mixed text+binary diff."""

    name = "mixed-binary-stub"

    def generate_patch(self, brief, failing_evidence=None):
        return TEXT_THEN_BINARY_GIT

    def explain_failure(self, check_results):
        return "stub diagnosis"


def test_repair_loop_survives_mixed_binary_proposal(tmp_path: Path):
    candidate = tmp_path / "cand"
    candidate.mkdir()
    (candidate / "a.txt").write_text("hello\n")
    (candidate / "b.bin").write_bytes(b"\x00\x01\x02")
    (candidate / "check.py").write_text("raise SystemExit(1)\n")
    spec = parse_spec({
        "name": "mixed-binary",
        "brief": "Fix it somehow.",
        "checks": [{"name": "c", "command": "python3 check.py",
                    "timeout_s": 10}],
    })
    report = run_repair(
        spec=spec, candidate_dir=candidate, mode="judge",
        llm=_MixedBinaryStub(), references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label=str(candidate),
        max_rounds=2,
    )
    # The binary block is a clean rejection fed back each round,
    # not a silent partial application reported as a repair.
    assert report["final_verdict"] == "FAIL"
    assert report["stopped_reason"] == "patch_not_applicable"
    assert "binary patch" in report["last_patch_error"]
    assert (candidate / "a.txt").read_text() == "hello\n"
    assert (candidate / "b.bin").read_bytes() == b"\x00\x01\x02"
