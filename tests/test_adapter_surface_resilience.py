"""Adapter-surface resilience: an adapter's *optional* surface —
its ``name`` attribute and its usage-log methods — can never
destroy a verdict the deterministic checks already decided.

Before v1.66.0 the runner and the repair loop read that surface
directly, although ``validate_llm`` / ``validate_references``
promise it stays optional. A duck-typed adapter with no ``name``
crashed a FAIL run with an ``AttributeError`` while the diagnosis
entry was being assembled (a PASS run, which skips the diagnosis,
noticed nothing) and crashed the repair loop before its first
run. A ``drain_calls`` / ``pending_calls`` that raised — or a
non-callable attribute standing where one was expected —
propagated out of the sealing path: the checks had run, the
verdict was decided, and no record (or repair report) was
delivered. The optional surface is now read only through
``getattr`` and the ``llm`` module's safe helpers: a missing name
degrades to the fallback label, and a missing, broken, or
non-list usage log degrades to no recorded usage.
"""

import copy
from pathlib import Path

from verifygate.evidence import verify_chain
from verifygate.llm import drain_calls_safely, pending_calls_safely
from verifygate.mock_llm import MockLLMAdapter
from verifygate.repair import run_repair
from verifygate.runner import run_verification
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup
from verifygate.verify import document_ok, verify_document

APP = Path(__file__).resolve().parent.parent
SPEC = load_spec(APP / "samples" / "spec.json")
FAILING_DIR = APP / "samples" / "failing"
PASSING_DIR = APP / "samples" / "passing"

USAGE_ENTRY = {
    "adapter": "surface-stub",
    "model": "nvidia/test-model",
    "purpose": "generate_patch",
    "finish_reason": "stop",
    "usage": {"prompt_tokens": 5, "completion_tokens": 7,
              "total_tokens": 12},
}


class NamelessLLM:
    """Duck-typed adapter with no ``name`` and no usage-log
    methods at all; its advisory pair delegates to the mock, so a
    repair through it can genuinely reach PASS."""

    def __init__(self):
        self._mock = MockLLMAdapter()

    def explain_failure(self, check_results):
        return self._mock.explain_failure(check_results)

    def generate_patch(self, brief, failing_evidence=None):
        return self._mock.generate_patch(brief, failing_evidence)


class NamelessReferences:
    """Duck-typed lookup with no ``name`` (and no ``last_error``)."""

    def lookup(self, query):
        return []


class ExplodingUsageLog(MockLLMAdapter):
    """Advisory pair works (inherited mock); both usage-log
    methods raise, as a third-party adapter's broken log might."""

    name = "exploding-usage-log"

    def drain_calls(self):
        raise RuntimeError("usage log exploded")

    def pending_calls(self):
        raise RuntimeError("usage log exploded")


class NonListUsageLog(MockLLMAdapter):
    """Usage-log methods return shapes that are not lists."""

    name = "nonlist-usage-log"

    def drain_calls(self):
        return "not a list"

    def pending_calls(self):
        return {"not": "a list"}


class NonCallableUsageLog(MockLLMAdapter):
    """Non-callable attributes stand where the log methods belong."""

    name = "noncallable-usage-log"
    drain_calls = None
    pending_calls = 42


class GenerationFailsUsageBroken(MockLLMAdapter):
    """Generation raises *and* the pending snapshot raises: the
    loop's generation-failure path snapshots the log too."""

    name = "generation-fails-usage-broken"

    def generate_patch(self, brief, failing_evidence=None):
        raise RuntimeError("Token Factory call failed: boom")

    def pending_calls(self):
        raise RuntimeError("usage log exploded")

    def drain_calls(self):
        raise RuntimeError("usage log exploded")


def _run(llm, candidate=FAILING_DIR, references=None):
    return run_verification(
        spec=SPEC, candidate_dir=candidate, mode="judge", llm=llm,
        references=references or MockReferenceLookup(),
        spec_label="samples/spec.json",
        candidate_label=str(candidate))


def _repair(llm, candidate="failing", max_rounds=3):
    return run_repair(
        spec=SPEC, candidate_dir=APP / "samples" / candidate,
        mode="judge", llm=llm, references=MockReferenceLookup(),
        spec_label="samples/spec.json",
        candidate_label=f"samples/{candidate}",
        max_rounds=max_rounds)


def _diagnosis(record):
    return next(e for e in record["entries"] if e["kind"] == "diagnosis")


# -- helpers -------------------------------------------------------


def test_safe_helpers_return_genuine_calls_and_drain():
    llm = MockLLMAdapter()
    llm.record_call(copy.deepcopy(USAGE_ENTRY))
    assert pending_calls_safely(llm) == [USAGE_ENTRY]
    # the snapshot did not drain
    assert pending_calls_safely(llm) == [USAGE_ENTRY]
    assert drain_calls_safely(llm) == [USAGE_ENTRY]
    assert drain_calls_safely(llm) == []


def test_safe_helpers_degrade_on_missing_broken_and_nonlist_logs():
    assert drain_calls_safely(NamelessLLM()) == []
    assert pending_calls_safely(NamelessLLM()) == []
    assert drain_calls_safely(ExplodingUsageLog()) == []
    assert pending_calls_safely(ExplodingUsageLog()) == []
    assert drain_calls_safely(NonListUsageLog()) == []
    assert pending_calls_safely(NonListUsageLog()) == []
    assert drain_calls_safely(NonCallableUsageLog()) == []
    assert pending_calls_safely(NonCallableUsageLog()) == []
    assert drain_calls_safely(None) == []
    assert pending_calls_safely(None) == []


# -- runner: names -------------------------------------------------


def test_nameless_adapter_fail_run_is_delivered_with_fallback_name():
    record = _run(NamelessLLM())
    assert record["verdict"] == "FAIL"
    assert verify_chain(record) is True
    assert _diagnosis(record)["llm_adapter"] == "unknown-llm-adapter"


def test_nameless_adapter_pass_run_is_delivered():
    record = _run(NamelessLLM(), candidate=PASSING_DIR)
    assert record["verdict"] == "PASS"
    assert verify_chain(record) is True


def test_nameless_lookup_fail_run_is_delivered_with_fallback_source():
    record = _run(MockLLMAdapter(), references=NamelessReferences())
    assert record["verdict"] == "FAIL"
    assert verify_chain(record) is True
    diagnosis = _diagnosis(record)
    assert diagnosis["reference_source"] == "unknown-reference-lookup"
    assert diagnosis["llm_adapter"] == "mock-deterministic"


# -- runner: usage log ---------------------------------------------


def test_exploding_drain_cannot_destroy_a_pass_or_fail_record():
    for candidate, verdict in ((PASSING_DIR, "PASS"),
                               (FAILING_DIR, "FAIL")):
        record = _run(ExplodingUsageLog(), candidate=candidate)
        assert record["verdict"] == verdict
        assert verify_chain(record) is True
        # the broken log degraded to no recorded usage entry
        assert all(e["kind"] != "llm_usage" for e in record["entries"])


def test_nonlist_and_noncallable_drains_cannot_destroy_a_record():
    for llm in (NonListUsageLog(), NonCallableUsageLog()):
        record = _run(llm)
        assert record["verdict"] == "FAIL"
        assert verify_chain(record) is True


def test_genuine_usage_is_still_sealed_after_the_guard():
    llm = MockLLMAdapter()
    llm.record_call(copy.deepcopy(USAGE_ENTRY))
    record = _run(llm, candidate=PASSING_DIR)
    usage = next(e for e in record["entries"] if e["kind"] == "llm_usage")
    assert usage["total_tokens"] == 12
    assert usage["calls"] == [USAGE_ENTRY]
    assert verify_chain(record) is True


# -- repair loop ----------------------------------------------------


def test_repair_with_nameless_adapter_repairs_and_credits_fallback():
    report = _repair(NamelessLLM())
    assert report["final_verdict"] == "PASS"
    assert report["llm_adapter"] == "unknown-llm-adapter"
    assert report["attempts"][1]["patch_from_previous"]["llm_adapter"] \
        == "unknown-llm-adapter"
    assert document_ok(verify_document(report)) is True


def test_repair_with_exploding_usage_log_still_delivers_its_report():
    report = _repair(ExplodingUsageLog(), max_rounds=1)
    assert report["final_verdict"] == "PASS"
    assert report["llm_usage_total"]["total_tokens"] == 0
    assert report["unsealed_llm_usage"] == []
    assert document_ok(verify_document(report)) is True


def test_repair_with_nonlist_usage_log_still_delivers_its_report():
    report = _repair(NonListUsageLog(), max_rounds=1)
    assert report["final_verdict"] == "PASS"
    assert document_ok(verify_document(report)) is True


def test_repair_generation_failure_with_broken_log_is_preserved():
    report = _repair(GenerationFailsUsageBroken(), max_rounds=2)
    assert report["final_verdict"] == "FAIL"
    assert report["stopped_reason"] == "generation_failed"
    assert report["failed_patch"]["error_kind"] == "generation_failed"
    assert report["failed_patch"]["llm_usage"] == []
    assert report["unsealed_llm_usage"] == []
    assert document_ok(verify_document(report)) is True


def test_repair_passing_candidate_with_exploding_drain_is_delivered():
    report = _repair(ExplodingUsageLog(), candidate="passing")
    assert report["final_verdict"] == "PASS"
    assert report["rounds_used"] == 0
    assert document_ok(verify_document(report)) is True
