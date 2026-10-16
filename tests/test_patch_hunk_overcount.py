"""Hunk over-count rejection (v1.136.0).

A numbered hunk header's declared counts are its contract in
both directions. v1.96.0 held the body to the declared minimum
(a shortfall is corrupt), but lines arriving *after* the counts
were met stayed tolerated wholesale — a tolerance only a
trailing *context* line deserves. On the first live .fsi
candidate, Nano twice proposed a hunk whose header
under-declared its body (``@@ -4,1 +4,1 @@`` over one context
line plus the ``-``/``+`` pair that was the entire change):
the counted reader stopped at the context line, the change
lines beyond the counts were silently dropped, the fragment
applied as a no-op that still reported the file as changed,
and the patched tree was byte-identical to the original —
same tree hash, same failing checks — while the loop burned
its rounds. Change lines beyond the declared counts are now
a clean :class:`PatchError` at parse time, fed back by the
repair loop like any other inapplicable diff; trailing
context lines remain tolerated, and a well-formed patch
followed by excess context still applies.
"""

from pathlib import Path

import pytest

from verifygate.llm import LLMAdapter
from verifygate.patch import PatchError, apply_patch, parse_patch
from verifygate.repair import run_repair
from verifygate.spec import parse_spec
from verifygate.tavily import MockReferenceLookup

# The exact live shape: header counts only the context line;
# the -/+ pair carrying the change sits beyond the counts.
UNDER_DECLARED = """\
--- a/rates.txt
+++ b/rates.txt
@@ -4,1 +4,1 @@
 // rate table below
-old rate
+new rate
"""

# The second live shape: header counts all three context
# lines; the change pair sits beyond them.
UNDER_DECLARED_CONTEXT_FIRST = """\
--- a/rates.txt
+++ b/rates.txt
@@ -1,3 +1,3 @@
 // line one
 // line two
 // line three
-old rate
+new rate
"""

EXCESS_CONTEXT_MULTI_HUNK = """\
--- a/a.txt
+++ b/a.txt
@@ -1 +1 @@
-old
+new
 trailing context the header did not count
@@ -3 +3 @@
-x
+y
"""

MULTI_FILE_SECOND_UNDER_DECLARED = """\
--- a/a.txt
+++ b/a.txt
@@ -1 +1 @@
-a1
+a2
--- a/b.txt
+++ b/b.txt
@@ -1,1 +1,1 @@
 // context
-b1
+b2
"""


def test_under_declared_hunk_is_rejected_and_file_unchanged(
        tmp_path: Path):
    (tmp_path / "rates.txt").write_text("// rate table below\nold rate\n")
    with pytest.raises(PatchError, match="beyond the declared counts"):
        apply_patch(tmp_path, UNDER_DECLARED)
    assert (tmp_path / "rates.txt").read_text() == (
        "// rate table below\nold rate\n")


def test_under_declared_context_first_hunk_is_rejected(
        tmp_path: Path):
    (tmp_path / "rates.txt").write_text(
        "// line one\n// line two\n// line three\nold rate\n")
    with pytest.raises(PatchError, match="corrupt patch"):
        apply_patch(tmp_path, UNDER_DECLARED_CONTEXT_FIRST)
    assert (tmp_path / "rates.txt").read_text().endswith("old rate\n")


def test_parse_patch_rejects_under_declared_hunk():
    with pytest.raises(PatchError,
                       match="1 more old and 1 more new"):
        parse_patch(UNDER_DECLARED)


def test_excess_context_before_next_hunk_still_applies(
        tmp_path: Path):
    (tmp_path / "a.txt").write_text(
        "old\ntrailing context the header did not count\nx\n")
    assert apply_patch(tmp_path, EXCESS_CONTEXT_MULTI_HUNK) == ["a.txt"]
    assert (tmp_path / "a.txt").read_text() == (
        "new\ntrailing context the header did not count\ny\n")


def test_under_declared_second_file_rejects_whole_diff_atomically(
        tmp_path: Path):
    (tmp_path / "a.txt").write_text("a1\n")
    (tmp_path / "b.txt").write_text("// context\nb1\n")
    with pytest.raises(PatchError, match="corrupt patch"):
        apply_patch(tmp_path, MULTI_FILE_SECOND_UNDER_DECLARED)
    assert (tmp_path / "a.txt").read_text() == "a1\n"
    assert (tmp_path / "b.txt").read_text() == "// context\nb1\n"


class _UnderDeclaredStub(LLMAdapter):
    """Always proposes the under-declared (count-lying) diff."""

    name = "under-declared-stub"

    def generate_patch(self, brief, failing_evidence=None):
        return UNDER_DECLARED.replace("rates.txt", "a.txt")

    def explain_failure(self, check_results):
        return "stub diagnosis"


def test_repair_loop_feeds_back_under_declared_proposal(
        tmp_path: Path):
    candidate = tmp_path / "cand"
    candidate.mkdir()
    (candidate / "a.txt").write_text("// rate table below\nold rate\n")
    (candidate / "check.py").write_text("raise SystemExit(1)\n")
    spec = parse_spec({
        "name": "under-declared-proposal",
        "brief": "Fix it somehow.",
        "checks": [{"name": "c", "command": "python3 check.py",
                    "timeout_s": 10}],
    })
    report = run_repair(
        spec=spec, candidate_dir=candidate, mode="judge",
        llm=_UnderDeclaredStub(), references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label=str(candidate),
        max_rounds=2,
    )
    # The under-declared diff is a clean rejection fed back each
    # round, not a silent no-op application.
    assert report["final_verdict"] == "FAIL"
    assert report["stopped_reason"] == "patch_not_applicable"
    assert "beyond the declared counts" in report["last_patch_error"]
    assert (candidate / "a.txt").read_text() == (
        "// rate table below\nold rate\n")
