"""Repair loop: FAIL -> mock patch -> re-verify, all verdicts by real runs."""

import json
from pathlib import Path

from verifygate.evidence import verify_chain
from verifygate.llm import LLMAdapter
from verifygate.mock_llm import MockLLMAdapter
from verifygate.repair import run_repair
from verifygate.spec import load_spec, parse_spec
from verifygate.tavily import MockReferenceLookup

APP = Path(__file__).resolve().parent.parent
SPEC = load_spec(APP / "samples" / "spec.json")


def _repair(candidate: str, max_rounds: int = 3, llm=None, spec=SPEC) -> dict:
    return run_repair(
        spec=spec,
        candidate_dir=APP / "samples" / candidate,
        mode="judge",
        llm=llm or MockLLMAdapter(),
        references=MockReferenceLookup(),
        spec_label="samples/spec.json",
        candidate_label=f"samples/{candidate}",
        max_rounds=max_rounds,
    )


def test_failing_sample_is_repaired_to_pass():
    report = _repair("failing")
    assert report["final_verdict"] == "PASS"
    assert report["stopped_reason"] == "passed"
    assert report["rounds_used"] == 1
    assert [a["verdict"] for a in report["attempts"]] == ["FAIL", "PASS"]
    patch = report["attempts"][1]["patch_from_previous"]
    assert patch["applied"] is True
    assert patch["changed_files"] == ["stringtools.py"]
    for a in report["attempts"]:
        assert verify_chain(a["evidence"]) is True
        assert a["record_hash"] == a["evidence"]["record_hash"]


def test_original_candidate_on_disk_is_never_modified():
    before = (APP / "samples" / "failing" / "stringtools.py").read_bytes()
    _repair("failing")
    assert (APP / "samples" / "failing" / "stringtools.py").read_bytes() == before


def test_passing_sample_needs_no_repair():
    report = _repair("passing")
    assert report["final_verdict"] == "PASS"
    assert report["rounds_used"] == 0
    assert len(report["attempts"]) == 1


def test_zero_max_rounds_means_single_attempt():
    report = _repair("failing", max_rounds=0)
    assert report["final_verdict"] == "FAIL"
    assert report["rounds_used"] == 0
    assert report["stopped_reason"] == "max_rounds"


def test_unpatchable_failure_stops_cleanly(tmp_path):
    candidate = tmp_path / "cand"
    candidate.mkdir()
    (candidate / "check.py").write_text("raise SystemExit(1)\n")
    spec = parse_spec({
        "name": "always-fails",
        "brief": "Make the check pass somehow.",  # no slugify -> mock sends no diff
        "checks": [{"name": "c", "command": "python3 check.py", "timeout_s": 10}],
    })
    report = run_repair(
        spec=spec, candidate_dir=candidate, mode="judge",
        llm=MockLLMAdapter(), references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label=str(candidate), max_rounds=3,
    )
    assert report["final_verdict"] == "FAIL"
    assert report["stopped_reason"] == "patch_not_applicable"
    assert "no unified diff" in report["last_patch_error"]
    assert len(report["attempts"]) == 1


class _NoteStub(LLMAdapter):
    """Always proposes a valid patch that never fixes the failing check."""

    name = "note-stub"

    def __init__(self):
        self.calls = 0

    def generate_patch(self, brief, failing_evidence=None):
        self.calls += 1
        return (
            "--- /dev/null\n"
            f"+++ b/note{self.calls}.txt\n"
            "@@ -0,0 +1,1 @@\n"
            "+still broken\n"
        )

    def explain_failure(self, check_results):
        return "stub diagnosis"


def test_max_rounds_caps_patch_attempts(tmp_path):
    candidate = tmp_path / "cand"
    candidate.mkdir()
    (candidate / "check.py").write_text("raise SystemExit(1)\n")
    spec = parse_spec({
        "name": "always-fails",
        "brief": "Make the check pass somehow.",
        "checks": [{"name": "c", "command": "python3 check.py", "timeout_s": 10}],
    })
    stub = _NoteStub()
    report = run_repair(
        spec=spec, candidate_dir=candidate, mode="judge",
        llm=stub, references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label=str(candidate), max_rounds=2,
    )
    assert stub.calls == 2
    assert report["rounds_used"] == 2
    assert report["stopped_reason"] == "max_rounds"
    assert [a["verdict"] for a in report["attempts"]] == ["FAIL", "FAIL", "FAIL"]
    # each patched attempt is a real directory carrying the stub's new file
    last_dir = Path(report["attempts"][-1]["candidate_dir"])
    assert (last_dir / "note1.txt").exists() and (last_dir / "note2.txt").exists()
    json.dumps(report)  # report must stay JSON-serializable end to end
