"""Diagnosis resilience: an LLM explanation failure can never
destroy a verdict the deterministic checks already decided.

Before v1.19.0 the runner called ``llm.explain_failure`` unguarded,
so a real-adapter failure (network error, malformed Token Factory
response, empty completion after the retry — all RuntimeError)
propagated out of the run: a deterministically FAILed candidate
produced no evidence record at all (CLI exit 2, "adapter error"),
the repair loop aborted on its first attempt, and replaying a FAIL
record in real mode reported "not reproduced" even though every
check reproduced — only the words had failed, and words are never
compared. The Tavily lookup right next to it already degraded this
way (sealed ``reference_error``); the diagnosis now does the same
(sealed ``diagnosis_error`` + a deterministic fallback explanation
rendered from the real check results).
"""

import copy
import json
from pathlib import Path

import pytest

from verifygate import cli
from verifygate.evidence import verify_chain
from verifygate.mock_llm import MockLLMAdapter
from verifygate.repair import run_repair
from verifygate.replay import replay_document, replay_ok
from verifygate.runner import run_verification
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup
from verifygate.verify import document_ok, verify_document
from verifygate.webview import render_document

APP = Path(__file__).resolve().parent.parent
SPEC = load_spec(APP / "samples" / "spec.json")
FAILING_DIR = APP / "samples" / "failing"
PASSING_DIR = APP / "samples" / "passing"
REAL_FAIL_V2 = APP / "samples" / "evidence_real_fail_v2.json"

USAGE_ENTRY = {
    "adapter": "exploding-explain",
    "model": "nvidia/test-model",
    "purpose": "explain_failure",
    "finish_reason": "length",
    "usage": {"prompt_tokens": 10, "completion_tokens": 20,
              "total_tokens": 30},
}


class ExplodingExplain(MockLLMAdapter):
    """Patch proposals work (inherited mock); explanations raise.

    One call is recorded before the raise, mirroring the real
    adapter, which records a completed-but-empty completion attempt
    before its retry also fails — that spend must still be sealed.
    """

    name = "exploding-explain"

    def explain_failure(self, check_results):
        self.record_call(copy.deepcopy(USAGE_ENTRY))
        raise RuntimeError("Token Factory call failed (URLError): boom")


class NonStringExplain(MockLLMAdapter):
    name = "nonstring-explain"

    def explain_failure(self, check_results):
        return {"not": "a string"}


def _run(llm, candidate=FAILING_DIR, mode="judge"):
    return run_verification(
        spec=SPEC, candidate_dir=candidate, mode=mode, llm=llm,
        references=MockReferenceLookup(),
        spec_label="samples/spec.json",
        candidate_label="samples/failing")


def _diagnosis(record):
    return next(e for e in record["entries"] if e["kind"] == "diagnosis")


# -- the run survives a diagnosis failure ---------------------------------


def test_exploding_explanation_still_seals_fail_record():
    record = _run(ExplodingExplain(), mode="real")
    assert record["verdict"] == "FAIL"
    diag = _diagnosis(record)
    assert diag["diagnosis_error"] == (
        "diagnosis failed (RuntimeError): "
        "Token Factory call failed (URLError): boom")
    # The fallback is rendered from the real check results and says
    # plainly that no model explanation exists.
    assert "Diagnosis unavailable" in diag["explanation"]
    assert "unit_tests" in diag["explanation"]
    assert "repro_script" in diag["explanation"]
    # References were still fetched: only the explanation degraded.
    assert diag["references"] and diag["reference_error"] is None
    assert verify_chain(record)


def test_usage_recorded_before_the_raise_is_still_sealed():
    record = _run(ExplodingExplain(), mode="real")
    usage = next(e for e in record["entries"] if e["kind"] == "llm_usage")
    assert usage["calls"] == [USAGE_ENTRY]
    assert usage["total_tokens"] == 30
    results = verify_document(record)
    assert document_ok(results)


def test_nonstring_explanation_degrades_the_same_way():
    record = _run(NonStringExplain())
    diag = _diagnosis(record)
    assert diag["diagnosis_error"] is not None
    assert "TypeError" in diag["diagnosis_error"]
    assert "Diagnosis unavailable" in diag["explanation"]
    assert verify_chain(record)


def test_healthy_explanation_seals_diagnosis_error_none():
    record = _run(MockLLMAdapter())
    diag = _diagnosis(record)
    assert diag["diagnosis_error"] is None
    assert "Diagnosis (mock" in diag["explanation"]
    assert verify_chain(record)


def test_passing_candidate_needs_no_diagnosis_at_all():
    record = _run(ExplodingExplain(), candidate=PASSING_DIR)
    assert record["verdict"] == "PASS"
    assert all(e["kind"] != "diagnosis" for e in record["entries"])
    assert verify_chain(record)


def test_cli_run_with_exploding_adapter_writes_the_fail_record(
        monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli, "_adapters",
        lambda mode, model=None: (ExplodingExplain(),
                                  MockReferenceLookup()))
    out = tmp_path / "evidence.json"
    rc = cli.main(["run", "--spec", str(APP / "samples" / "spec.json"),
                   "--candidate", str(FAILING_DIR), "--mode", "real",
                   "--out", str(out)])
    assert rc == 1  # FAIL verdict, not the exit-2 adapter error
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["verdict"] == "FAIL"
    assert _diagnosis(record)["diagnosis_error"] is not None
    assert verify_chain(record)


# -- the repair loop and replay survive it too ------------------------------


def test_repair_loop_repairs_past_a_dead_diagnosis():
    report = run_repair(
        spec=SPEC, candidate_dir=FAILING_DIR, mode="judge",
        llm=ExplodingExplain(), references=MockReferenceLookup(),
        spec_label="samples/spec.json",
        candidate_label="samples/failing", max_rounds=3)
    assert report["final_verdict"] == "PASS"
    assert report["rounds_used"] == 1
    attempt0 = report["attempts"][0]["evidence"]
    assert _diagnosis(attempt0)["diagnosis_error"] is not None
    assert all(verify_chain(a["evidence"]) for a in report["attempts"])
    assert document_ok(verify_document(report))


def test_replay_matches_despite_a_dead_diagnosis():
    record = _run(MockLLMAdapter())
    results = replay_document(
        record, spec=SPEC, candidate_dir=FAILING_DIR, mode="real",
        llm=ExplodingExplain(), references=MockReferenceLookup(),
        spec_label="samples/spec.json")
    assert results[0]["error"] is None
    assert replay_ok(results)


# -- rendering ----------------------------------------------------------------


def test_webview_shows_the_diagnosis_degradation_note():
    html = render_document(_run(ExplodingExplain(), mode="real"))
    assert "Diagnosis unavailable:" in html
    assert "does not depend on the diagnosis" in html
    assert "Hash chain verified" in html
    assert "CHAIN INVALID" not in html


# -- the committed real-mode record under the new shape -----------------------


@pytest.mark.skipif(not REAL_FAIL_V2.exists(),
                    reason="real-mode sample not generated yet")
def test_committed_real_fail_v2_record():
    record = json.loads(REAL_FAIL_V2.read_text(encoding="utf-8"))
    assert record["version"] == "1.19.0"
    assert record["verdict"] == "FAIL"
    header = record["entries"][0]
    assert header["mode"] == "real"
    diag = _diagnosis(record)
    # A healthy real diagnosis under the new shape: the field is
    # present and null, the explanation is the model's own text, and
    # the no-Tavily-key degradation sits sealed beside it.
    assert diag["diagnosis_error"] is None
    assert diag["llm_adapter"] == "nebius-token-factory"
    assert diag["explanation"].strip()
    assert diag["reference_error"] is not None
    assert "TAVILY_API_KEY" in diag["reference_error"]
    usage = next(e for e in record["entries"] if e["kind"] == "llm_usage")
    assert usage["total_tokens"] > 0
    assert all(c["adapter"] == "nebius-token-factory"
               and "Nemotron" in c["model"] for c in usage["calls"])
    assert verify_chain(record)
    assert document_ok(verify_document(
        record, candidate_dir=FAILING_DIR, spec_hash=SPEC.hash))
