"""Workspace lifecycle: temp copies are deleted, never leaked.

Regression tests for a real failure found while dogfooding v1.0.0:
``prepare_workspace`` copies were never removed, so every verification
leaked a full candidate copy into the temp filesystem (744 orphaned
``verifygate-*`` directories on the build machine). A long nested run —
VerifyGate's own suite running inside a VerifyGate sandbox — filled
the 512 MB tmpfs and failed with ``OSError: [Errno 28]``.

Contract pinned here:
* ``run_verification`` removes its internal workspace, always.
* The repair loop removes rejected patch copies and superseded
  intermediate copies; it keeps exactly the final patched copy (the
  CLI's ``--patched-out`` copies from it) and never touches the
  caller's own candidate directory.
"""

import tempfile
from pathlib import Path

import pytest

from verifygate.llm import LLMAdapter
from verifygate.mock_llm import MockLLMAdapter, _SAMPLE_SLUGIFY_DIFF
from verifygate.repair import run_repair
from verifygate.runner import run_verification
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup

APP = Path(__file__).resolve().parent.parent
SPEC = load_spec(APP / "samples" / "spec.json")


@pytest.fixture()
def temp_root(tmp_path, monkeypatch):
    """Redirect tempfile (hence every sandbox workspace) into tmp_path."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


def _workspaces(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir()
                  if p.is_dir() and p.name.startswith("verifygate-"))


def _run(candidate: str) -> dict:
    return run_verification(
        spec=SPEC,
        candidate_dir=APP / "samples" / candidate,
        mode="judge",
        llm=MockLLMAdapter(),
        references=MockReferenceLookup(),
        spec_label="samples/spec.json",
        candidate_label=f"samples/{candidate}",
    )


def _repair(candidate: str, llm, max_rounds: int = 3) -> dict:
    return run_repair(
        spec=SPEC,
        candidate_dir=APP / "samples" / candidate,
        mode="judge",
        llm=llm,
        references=MockReferenceLookup(),
        spec_label="samples/spec.json",
        candidate_label=f"samples/{candidate}",
        max_rounds=max_rounds,
    )


def test_run_verification_leaves_no_workspace(temp_root):
    _run("passing")
    _run("failing")
    assert _workspaces(temp_root) == []


class _BadThenGoodPatch(LLMAdapter):
    """First proposal is an unapplicable diff, second is the real fix."""

    name = "bad-then-good-stub"

    def __init__(self):
        self.calls = 0

    def generate_patch(self, brief, failing_evidence=None):
        self.calls += 1
        if self.calls == 1:
            return ("--- a/stringtools.py\n+++ b/stringtools.py\n"
                    "@@ -1,1 +1,1 @@\n-no such line on disk\n+replacement\n")
        return _SAMPLE_SLUGIFY_DIFF

    def explain_failure(self, check_results):
        return "stub diagnosis"


def test_repair_keeps_only_the_final_patched_copy(temp_root):
    before = (APP / "samples" / "failing" / "stringtools.py").read_bytes()
    report = _repair("failing", _BadThenGoodPatch())
    assert report["final_verdict"] == "PASS"
    assert report["rounds_used"] == 2  # 1 rejected diff + 1 applied fix
    remaining = _workspaces(temp_root)
    # Exactly one survivor: the final patched copy the CLI copies from.
    assert len(remaining) == 1
    assert str(remaining[0]) == report["attempts"][-1]["candidate_dir"]
    assert "def slugify" in (remaining[0] / "stringtools.py").read_text()
    # The caller's candidate is untouched and was never a workspace.
    assert (APP / "samples" / "failing" / "stringtools.py").read_bytes() == before


def test_repair_that_never_patches_leaves_nothing(temp_root):
    class _NoDiff(LLMAdapter):
        name = "no-diff-stub"

        def generate_patch(self, brief, failing_evidence=None):
            return "Plan only, no diff."

        def explain_failure(self, check_results):
            return "stub diagnosis"

    report = _repair("failing", _NoDiff(), max_rounds=2)
    assert report["final_verdict"] == "FAIL"
    assert _workspaces(temp_root) == []
