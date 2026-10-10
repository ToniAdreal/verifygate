"""End-to-end: the bundled sample must PASS with the good candidate and
FAIL (with a diagnosis) with the broken one — decided by real test runs."""

from pathlib import Path

from verifygate.evidence import verify_chain
from verifygate.mock_llm import MockLLMAdapter
from verifygate.runner import run_verification
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup

APP = Path(__file__).resolve().parent.parent
SPEC = load_spec(APP / "samples" / "spec.json")


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


def test_passing_candidate_passes():
    record = _run("passing")
    assert record["verdict"] == "PASS"
    assert verify_chain(record) is True
    checks = [e for e in record["entries"] if e["kind"] == "check"]
    assert len(checks) == 2 and all(c["verdict"] == "PASS" for c in checks)


def test_failing_candidate_fails_with_diagnosis():
    record = _run("failing")
    assert record["verdict"] == "FAIL"
    assert verify_chain(record) is True
    kinds = [e["kind"] for e in record["entries"]]
    assert "diagnosis" in kinds
    diagnosis = next(e for e in record["entries"] if e["kind"] == "diagnosis")
    assert "slugify" in diagnosis["explanation"] or \
        "unit_tests" in diagnosis["explanation"]
    assert diagnosis["references"], "diagnosis should carry references"
    assert diagnosis["references"][0]["source"] == "mock"


def test_mock_llm_is_deterministic():
    llm = MockLLMAdapter()
    assert llm.generate_patch("brief") == llm.generate_patch("brief")
