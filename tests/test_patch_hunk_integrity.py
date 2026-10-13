"""Hunk integrity (v1.96.0).

A numbered hunk header declares exactly how many old and new
lines its body must carry, but the parser read the body only
*up to* the declared counts and never checked a shortfall: a
hunk declaring five old / five new lines but carrying one of
each applied as if it had declared one, and a corrupt line
inside a hunk silently ended it mid-body (dropping its
remaining lines, additions included) while the partial
change applied. Numbered hunks are now held to their
declared counts at parse time. Lines *beyond* the counts
stay tolerated by design — a committed real-mode report's
applied patch carries a trailing context line beyond its
counts, and replay depends on reconstructing the counted
hunk exactly — and bare ``@@`` hunks (no counts declared)
and stale start numbers are unchanged.
"""

from pathlib import Path

import pytest

from verifygate.llm import LLMAdapter
from verifygate.patch import PatchError, apply_patch, parse_patch
from verifygate.repair import run_repair
from verifygate.spec import parse_spec
from verifygate.tavily import MockReferenceLookup

TRUNCATED_AT_EOF = """\
--- a/a.txt
+++ b/a.txt
@@ -1,5 +1,5 @@
-old
+new
"""

TRUNCATED_THEN_NEXT_FILE = """\
diff --git a/a.txt b/a.txt
--- a/a.txt
+++ b/a.txt
@@ -1,5 +1,5 @@
-old
+new
diff --git a/b.txt b/b.txt
--- a/b.txt
+++ b/b.txt
@@ -1 +1 @@
-b1
+b2
"""

CORRUPT_LINE_INSIDE = """\
--- a/a.txt
+++ b/a.txt
@@ -1,2 +1,2 @@
-old
!corrupt
+new
"""

EXCESS_TRAILING_CONTEXT = """\
--- a/a.txt
+++ b/a.txt
@@ -1 +1 @@
-old
+new
 trailing context the header did not count
"""

WELL_FORMED_TWO_HUNKS = """\
--- a/a.txt
+++ b/a.txt
@@ -1,3 +1,3 @@
 a
-old
+new
 b
@@ -5 +5 @@
-x
+y
"""

NO_NEWLINE_AT_END = (
    "--- a/a.txt\n"
    "+++ b/a.txt\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "\\ No newline at end of file\n"
    "+new\n"
    "\\ No newline at end of file\n"
)

BARE_HUNK = """\
--- a/a.txt
+++ b/a.txt
@@
-old
+new
"""


def test_truncated_hunk_at_eof_is_rejected(tmp_path: Path):
    (tmp_path / "a.txt").write_text("old\n")
    with pytest.raises(PatchError, match="corrupt patch"):
        apply_patch(tmp_path, TRUNCATED_AT_EOF)
    assert (tmp_path / "a.txt").read_text() == "old\n"


def test_truncated_hunk_before_next_file_is_rejected(tmp_path: Path):
    (tmp_path / "a.txt").write_text("old\n")
    (tmp_path / "b.txt").write_text("b1\n")
    with pytest.raises(PatchError, match="corrupt patch"):
        apply_patch(tmp_path, TRUNCATED_THEN_NEXT_FILE)
    # Atomic: neither file was touched.
    assert (tmp_path / "a.txt").read_text() == "old\n"
    assert (tmp_path / "b.txt").read_text() == "b1\n"


def test_corrupt_line_inside_hunk_is_rejected(tmp_path: Path):
    (tmp_path / "a.txt").write_text("old\nz\n")
    with pytest.raises(PatchError, match="corrupt patch"):
        apply_patch(tmp_path, CORRUPT_LINE_INSIDE)
    assert (tmp_path / "a.txt").read_text() == "old\nz\n"


def test_parse_patch_rejects_truncated_hunk():
    with pytest.raises(PatchError, match="carries only 1 old and 1 new"):
        parse_patch(TRUNCATED_AT_EOF)


def test_excess_trailing_context_beyond_counts_stays_tolerated(
        tmp_path: Path):
    # The established tolerance committed real-mode evidence
    # relies on: lines beyond the declared counts are not
    # applied, and the counted hunk applies exactly.
    (tmp_path / "a.txt").write_text("old\ntrailing context the header did not count\n")
    assert apply_patch(tmp_path, EXCESS_TRAILING_CONTEXT) == ["a.txt"]
    assert (tmp_path / "a.txt").read_text() == (
        "new\ntrailing context the header did not count\n")


def test_well_formed_multi_hunk_patch_still_applies(tmp_path: Path):
    (tmp_path / "a.txt").write_text("a\nold\nb\nc\nx\n")
    assert apply_patch(tmp_path, WELL_FORMED_TWO_HUNKS) == ["a.txt"]
    assert (tmp_path / "a.txt").read_text() == "a\nnew\nb\nc\ny\n"


def test_bare_hunk_still_derives_counts_from_body(tmp_path: Path):
    (tmp_path / "a.txt").write_text("old\n")
    assert apply_patch(tmp_path, BARE_HUNK) == ["a.txt"]
    assert (tmp_path / "a.txt").read_text() == "new\n"


def test_stale_start_numbers_still_tolerated(tmp_path: Path):
    (tmp_path / "a.txt").write_text("a\nold\nb\nc\nx\n")
    stale = WELL_FORMED_TWO_HUNKS.replace(
        "@@ -1,3 +1,3 @@", "@@ -7,3 +9,3 @@").replace(
        "@@ -5 +5 @@", "@@ -42 +42 @@")
    assert apply_patch(tmp_path, stale) == ["a.txt"]
    assert (tmp_path / "a.txt").read_text() == "a\nnew\nb\nc\ny\n"


def test_no_newline_marker_after_final_line_still_honoured(
        tmp_path: Path):
    (tmp_path / "a.txt").write_bytes(b"old")
    assert apply_patch(tmp_path, NO_NEWLINE_AT_END) == ["a.txt"]
    assert (tmp_path / "a.txt").read_bytes() == b"new"


class _TruncatedStub(LLMAdapter):
    """Always proposes a truncated (count-lying) diff."""

    name = "truncated-stub"

    def generate_patch(self, brief, failing_evidence=None):
        return TRUNCATED_AT_EOF.replace("a.txt", "a.txt")

    def explain_failure(self, check_results):
        return "stub diagnosis"


def test_repair_loop_feeds_back_truncated_proposal(tmp_path: Path):
    candidate = tmp_path / "cand"
    candidate.mkdir()
    (candidate / "a.txt").write_text("old\n")
    (candidate / "check.py").write_text("raise SystemExit(1)\n")
    spec = parse_spec({
        "name": "truncated-proposal",
        "brief": "Fix it somehow.",
        "checks": [{"name": "c", "command": "python3 check.py",
                    "timeout_s": 10}],
    })
    report = run_repair(
        spec=spec, candidate_dir=candidate, mode="judge",
        llm=_TruncatedStub(), references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label=str(candidate),
        max_rounds=2,
    )
    # The truncated diff is a clean rejection fed back each
    # round, not a silent partial application.
    assert report["final_verdict"] == "FAIL"
    assert report["stopped_reason"] == "patch_not_applicable"
    assert "corrupt patch" in report["last_patch_error"]
    assert (candidate / "a.txt").read_text() == "old\n"
