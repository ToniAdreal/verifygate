"""Second sample (JavaScript/Node): the harness is language-agnostic.

VerifyGate itself is Python, but check commands are arbitrary shell
commands. This sample's checks are Node's built-in test runner and a
Node repro script — no Python runs inside the candidate at all — and
the full spine (PASS, FAIL + diagnosis, repair FAIL -> PASS) must
behave exactly as it does for the Python sample. Skipped only when
Node is not on PATH.
"""

import shutil
from pathlib import Path

import pytest

from verifygate.evidence import verify_chain
from verifygate.mock_llm import MockLLMAdapter
from verifygate.repair import run_repair
from verifygate.runner import run_verification
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not on PATH")

APP = Path(__file__).resolve().parent.parent
JS = APP / "samples-js"
SPEC = load_spec(JS / "spec.json")


def _run(candidate: str) -> dict:
    return run_verification(
        spec=SPEC,
        candidate_dir=JS / candidate,
        mode="judge",
        llm=MockLLMAdapter(),
        references=MockReferenceLookup(),
        spec_label="samples-js/spec.json",
        candidate_label=f"samples-js/{candidate}",
    )


def test_js_passing_candidate_passes():
    record = _run("passing")
    assert record["verdict"] == "PASS"
    assert verify_chain(record) is True
    checks = [e for e in record["entries"] if e["kind"] == "check"]
    assert len(checks) == 2 and all(c["verdict"] == "PASS" for c in checks)
    assert all(c["command"].startswith("node") for c in checks)


def test_js_failing_candidate_fails_with_diagnosis():
    record = _run("failing")
    assert record["verdict"] == "FAIL"
    assert verify_chain(record) is True
    kinds = [e["kind"] for e in record["entries"]]
    assert "diagnosis" in kinds


def test_js_repair_loop_repairs_failing_candidate():
    before = (JS / "failing" / "cart.js").read_bytes()
    report = run_repair(
        spec=SPEC,
        candidate_dir=JS / "failing",
        mode="judge",
        llm=MockLLMAdapter(),
        references=MockReferenceLookup(),
        spec_label="samples-js/spec.json",
        candidate_label="samples-js/failing",
        max_rounds=3,
    )
    assert report["final_verdict"] == "PASS"
    assert report["rounds_used"] == 1
    assert report["attempts"][0]["verdict"] == "FAIL"
    assert report["attempts"][1]["verdict"] == "PASS"
    for attempt in report["attempts"]:
        assert verify_chain(attempt["evidence"]) is True
    patch = report["attempts"][1]["patch_from_previous"]
    assert patch["applied"] is True
    assert patch["changed_files"] == ["cart.js"]
    # The original candidate on disk is never modified.
    assert (JS / "failing" / "cart.js").read_bytes() == before
