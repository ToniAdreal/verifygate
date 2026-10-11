"""Adapter validation: llm / references are validated up front.

Before v1.42.0 the ``llm`` and ``references`` arguments were the
last run parameters taken on trust, on every entry point:

* ``None``, a string, or a bare object on a PASS candidate sealed
  a verdict with no adapter at all — the diagnosis path is
  skipped for PASS and the usage drain is ``hasattr``-guarded,
  so nothing noticed the adapter was missing;
* the same values on a FAIL candidate crashed with an unhandled
  ``AttributeError`` on the adapter's ``name`` — after the
  guarded ``explain_failure`` / ``lookup`` call had degraded as
  designed (v1.19.0), the fallback / diagnosis assembly read
  ``llm.name`` / ``references.name`` unguarded and lost the
  record the deterministic checks had already decided;
* in the repair loop the same crash happened before the first
  run, at ``llm.name`` for the credited adapter, with no report;
* in replay it escaped mid-replay, past the per-record path's
  ``OSError`` / ``RuntimeError`` handling.

Every entry point now validates both adapters before any
workspace is prepared, any check runs, or any record is
replayed: ``llm.validate_llm`` requires a callable
``explain_failure`` and ``generate_patch``;
``tavily.validate_references`` requires a callable ``lookup``.
Both are duck-typed — subclassing the ABCs is not required, and
``name`` / ``last_error`` / the usage-log methods stay optional,
exactly as earlier stubs rely on. A malformed adapter is a
``ValueError`` (the CLI maps it to exit 2). Validation is about
shape, not behaviour: adapters whose methods raise still
degrade under the v1.19.0 / v1.20.0 resilience rules.
"""

from pathlib import Path

import pytest

from verifygate.evidence import verify_chain
from verifygate.llm import LLMAdapter, validate_llm
from verifygate.mock_llm import MockLLMAdapter
from verifygate.repair import run_repair
from verifygate.replay import replay_document, replay_ok
from verifygate.runner import run_verification
from verifygate.spec import load_spec
from verifygate.tavily import (
    MockReferenceLookup,
    TavilyReferenceLookup,
    validate_references,
)

APP = Path(__file__).resolve().parent.parent
SPEC = load_spec(APP / "samples" / "spec.json")
FAILING = APP / "samples" / "failing"
PASSING = APP / "samples" / "passing"


class _CountingStub(LLMAdapter):
    name = "counting-stub"

    def __init__(self):
        self.explain_calls = 0
        self.patch_calls = 0

    def generate_patch(self, brief, failing_evidence=None):
        self.patch_calls += 1
        return ""

    def explain_failure(self, check_results):
        self.explain_calls += 1
        return "stub diagnosis"


class _DuckLLM:
    """Not an LLMAdapter subclass: duck typing must keep working."""

    name = "duck-llm"

    def generate_patch(self, brief, failing_evidence=None):
        return ""

    def explain_failure(self, check_results):
        return "duck diagnosis"


class _DuckLookup:
    """Not a ReferenceLookup subclass (the _StubLookup precedent)."""

    name = "duck-lookup"
    last_error = None

    def lookup(self, query, max_results=3):
        return []


class _OnlyExplain:
    name = "only-explain"

    def explain_failure(self, check_results):
        return "x"


class _OnlyPatch:
    name = "only-patch"

    def generate_patch(self, brief, failing_evidence=None):
        return ""


class _NonCallableExplain:
    name = "non-callable"
    explain_failure = "not-a-method"

    def generate_patch(self, brief, failing_evidence=None):
        return ""


BAD_LLMS = [None, "mock", 123, True, 2.5, object(), _OnlyExplain(),
            _OnlyPatch(), _NonCallableExplain(), ["llm"], {"llm": 1}]
BAD_REFS = [None, "tavily", 123, True, 2.5, object(), ["refs"],
            {"lookup": 1}]


class _NonCallableLookup:
    lookup = "not-a-method"


BAD_REFS = BAD_REFS + [_NonCallableLookup()]


def _run(candidate, llm, references):
    return run_verification(
        spec=SPEC, candidate_dir=candidate, mode="judge",
        llm=llm, references=references,
        spec_label="samples/spec.json", candidate_label="cand")


def _repair(llm, references):
    return run_repair(
        spec=SPEC, candidate_dir=FAILING, mode="judge",
        llm=llm, references=references,
        spec_label="samples/spec.json", candidate_label="cand",
        max_rounds=3)


# -- the helpers themselves ----------------------------------------------


@pytest.mark.parametrize("good", [MockLLMAdapter(), _CountingStub(),
                                  _DuckLLM()])
def test_validate_llm_accepts_real_and_duck_adapters(good):
    assert validate_llm(good) is good


@pytest.mark.parametrize("bad", BAD_LLMS)
def test_validate_llm_rejects_malformed(bad):
    with pytest.raises(ValueError):
        validate_llm(bad)


@pytest.mark.parametrize("good", [MockReferenceLookup(), _DuckLookup()])
def test_validate_references_accepts_real_and_duck_lookups(good):
    assert validate_references(good) is good


def test_validate_references_accepts_tavily_lookup():
    # Construction reads no key and makes no network call.
    lookup = TavilyReferenceLookup()
    assert validate_references(lookup) is lookup


@pytest.mark.parametrize("bad", BAD_REFS)
def test_validate_references_rejects_malformed(bad):
    with pytest.raises(ValueError):
        validate_references(bad)


def test_validate_error_names_the_missing_method():
    with pytest.raises(ValueError, match="generate_patch"):
        validate_llm(_OnlyExplain())
    with pytest.raises(ValueError, match="explain_failure"):
        validate_llm(_OnlyPatch())
    with pytest.raises(ValueError, match="lookup"):
        validate_references(object())


# -- the runner ------------------------------------------------------------


@pytest.mark.parametrize("bad", BAD_LLMS)
@pytest.mark.parametrize("candidate", [PASSING, FAILING])
def test_run_rejects_bad_llm_for_pass_and_fail(bad, candidate):
    # Before the fix PASS silently sealed and FAIL crashed with
    # AttributeError; both are now the one ValueError, up front.
    with pytest.raises(ValueError):
        _run(candidate, bad, MockReferenceLookup())


@pytest.mark.parametrize("bad", BAD_REFS)
@pytest.mark.parametrize("candidate", [PASSING, FAILING])
def test_run_rejects_bad_references_for_pass_and_fail(bad, candidate):
    with pytest.raises(ValueError):
        _run(candidate, MockLLMAdapter(), bad)


def test_run_with_duck_adapters_still_works():
    record = _run(FAILING, _DuckLLM(), _DuckLookup())
    assert record["verdict"] == "FAIL"
    assert verify_chain(record)
    record = _run(PASSING, _DuckLLM(), _DuckLookup())
    assert record["verdict"] == "PASS"
    assert verify_chain(record)


def test_run_validates_before_any_check_or_adapter_call():
    stub = _CountingStub()
    with pytest.raises(ValueError):
        _run(PASSING, stub, None)
    # The good adapter was never consulted for a run that the
    # malformed lookup already invalidated.
    assert stub.explain_calls == 0 and stub.patch_calls == 0


def test_exploding_adapter_still_degrades_not_rejected():
    # Shape validation must not become behaviour validation: an
    # adapter with callable methods that raise is the v1.19.0
    # resilience case, and its FAIL record is still delivered.
    class Exploding(MockLLMAdapter):
        def explain_failure(self, check_results):
            raise RuntimeError("boom")

    record = _run(FAILING, Exploding(), MockReferenceLookup())
    assert record["verdict"] == "FAIL"
    assert verify_chain(record)
    diagnosis = next(e for e in record["entries"]
                     if e["kind"] == "diagnosis")
    assert diagnosis["diagnosis_error"] is not None


# -- the repair loop -------------------------------------------------------


@pytest.mark.parametrize("bad", BAD_LLMS)
def test_repair_rejects_bad_llm_before_first_run(bad):
    stub_refs = MockReferenceLookup()
    with pytest.raises(ValueError):
        _repair(bad, stub_refs)


@pytest.mark.parametrize("bad", BAD_REFS)
def test_repair_rejects_bad_references_before_first_run(bad):
    with pytest.raises(ValueError):
        _repair(MockLLMAdapter(), bad)


def test_repair_validates_before_any_generation():
    stub = _CountingStub()
    with pytest.raises(ValueError):
        _repair(stub, None)
    assert stub.patch_calls == 0 and stub.explain_calls == 0


def test_repair_with_duck_adapters_still_repairs():
    # The mock's known-good diff is not available from the duck,
    # so the loop stops cleanly — but it runs, delivers a report,
    # and the report's attempt 0 is a genuine FAIL record.
    report = _repair(_DuckLLM(), _DuckLookup())
    assert report["attempts"][0]["verdict"] == "FAIL"
    assert verify_chain(report["attempts"][0]["evidence"])


# -- replay ------------------------------------------------------------------


def _genuine_record():
    return _run(FAILING, MockLLMAdapter(), MockReferenceLookup())


def _replay(data, llm, references):
    return replay_document(
        data, spec=SPEC, candidate_dir=FAILING, mode="judge",
        llm=llm, references=references,
        spec_label="samples/spec.json")


@pytest.mark.parametrize("bad", BAD_LLMS)
def test_replay_rejects_bad_llm_up_front(bad):
    with pytest.raises(ValueError):
        _replay(_genuine_record(), bad, MockReferenceLookup())


@pytest.mark.parametrize("bad", BAD_REFS)
def test_replay_rejects_bad_references_up_front(bad):
    with pytest.raises(ValueError):
        _replay(_genuine_record(), MockLLMAdapter(), bad)


def test_replay_with_good_adapters_still_matches():
    results = _replay(_genuine_record(), MockLLMAdapter(),
                      MockReferenceLookup())
    assert replay_ok(results)


def test_replay_with_duck_adapters_still_matches():
    results = _replay(_genuine_record(), _DuckLLM(), _DuckLookup())
    assert replay_ok(results)
