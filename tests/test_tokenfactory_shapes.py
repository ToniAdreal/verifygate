"""Token Factory response-shape hardening (v1.18.0).

The adapter's decoded response was trusted field by field: several
plausible malformed shapes — ``message`` as a bare string or ``None``,
``usage`` as a string or list, a non-string ``content`` — escaped as
unhandled ``AttributeError`` tracebacks, crashing ``verifygate run``
/ ``repair`` in real mode instead of failing as the clean adapter
error (exit 2) the CLI contract promises. A malformed ``usage`` that
*was* a dict (string / negative / float / bool token counts) would
have been recorded verbatim and sealed into a record whose own
consistency rules then fail that honestly-emitted record's chain.

Every malformed shape must raise one ``RuntimeError`` from either
advisory call, before anything is recorded; well-formed-but-unusual
responses (missing usage, a non-string ``model``) still work and seal
records that verify.

All tests inject a fake transport — zero network, zero credentials.
"""

from pathlib import Path

import pytest

from verifygate.evidence import verify_chain
from verifygate.runner import run_verification
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup
from verifygate.tokenfactory import DEFAULT_MODEL, TokenFactoryAdapter

APP = Path(__file__).resolve().parent.parent
SPEC = load_spec(APP / "samples" / "spec.json")
FAILING_CHECKS = [{"name": "c", "verdict": "FAIL", "command": "pytest"}]


def _good(**overrides) -> dict:
    data = {
        "model": DEFAULT_MODEL,
        "choices": [{
            "message": {"content": "the answer"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                  "total_tokens": 15},
    }
    data.update(overrides)
    return data


def _adapter(payload) -> TokenFactoryAdapter:
    return TokenFactoryAdapter(transport=lambda body: payload)


MALFORMED = {
    "top level is a list": ["not", "a", "dict"],
    "top level is a string": "not a dict",
    "top level is None": None,
    "choices missing": {"model": DEFAULT_MODEL, "usage": {}},
    "choices empty": _good(choices=[]),
    "choices not a list": _good(choices={"0": {}}),
    "choice not an object": _good(choices=["hello"]),
    "choice is None": _good(choices=[None]),
    "message missing": _good(choices=[{"finish_reason": "stop"}]),
    "message is a string": _good(choices=[
        {"message": "hello", "finish_reason": "stop"}]),
    "message is None": _good(choices=[
        {"message": None, "finish_reason": "stop"}]),
    "content not a string": _good(choices=[
        {"message": {"content": 42}, "finish_reason": "stop"}]),
    "reasoning_content not a string": _good(choices=[
        {"message": {"content": "", "reasoning_content": ["x"]},
         "finish_reason": "stop"}]),
    "usage is a string": _good(usage="lots"),
    "usage is a list": _good(usage=[1, 2]),
    "total_tokens is a string": _good(usage={
        "prompt_tokens": 10, "completion_tokens": 5,
        "total_tokens": "15"}),
    "total_tokens negative": _good(usage={
        "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": -1}),
    "total_tokens is a float": _good(usage={
        "prompt_tokens": 10, "completion_tokens": 5,
        "total_tokens": 15.0}),
    "prompt_tokens is a bool": _good(usage={
        "prompt_tokens": True, "completion_tokens": 5,
        "total_tokens": 15}),
}


@pytest.mark.parametrize("payload", MALFORMED.values(), ids=MALFORMED.keys())
def test_malformed_response_is_a_clean_adapter_error(payload):
    adapter = _adapter(payload)
    with pytest.raises(RuntimeError, match="response shape"):
        adapter.explain_failure(FAILING_CHECKS)
    # Nothing was recorded: a malformed response must never reach
    # the usage log that gets sealed into an evidence record.
    assert adapter.pending_calls() == []


@pytest.mark.parametrize("payload", MALFORMED.values(), ids=MALFORMED.keys())
def test_malformed_response_fails_patch_generation_too(payload):
    adapter = _adapter(payload)
    with pytest.raises(RuntimeError, match="response shape"):
        adapter.generate_patch("fix it", {"verdict": "FAIL", "checks": []})
    assert adapter.pending_calls() == []


def test_runner_propagates_the_clean_error_not_a_traceback():
    """The CLI maps RuntimeError to exit 2 ('adapter error'); any
    other exception type would escape as a traceback (probed pre-fix
    at the CLI level with the message-is-a-string shape)."""
    adapter = _adapter(_good(choices=[
        {"message": "hello", "finish_reason": "stop"}]))
    with pytest.raises(RuntimeError, match="response shape"):
        run_verification(
            spec=SPEC, candidate_dir=APP / "samples" / "failing",
            mode="real", llm=adapter, references=MockReferenceLookup(),
            spec_label="samples/spec.json",
            candidate_label="samples/failing",
        )


def test_missing_usage_seals_none_counts_and_still_verifies():
    payload = _good()
    del payload["usage"]
    adapter = _adapter(payload)
    record = run_verification(
        spec=SPEC, candidate_dir=APP / "samples" / "failing", mode="real",
        llm=adapter, references=MockReferenceLookup(),
        spec_label="samples/spec.json", candidate_label="samples/failing",
    )
    assert record["verdict"] == "FAIL"
    usage_entries = [e for e in record["entries"] if e["kind"] == "llm_usage"]
    assert len(usage_entries) == 1
    assert usage_entries[0]["calls"][0]["usage"] == {
        "prompt_tokens": None, "completion_tokens": None,
        "total_tokens": None}
    assert usage_entries[0]["total_tokens"] == 0
    assert verify_chain(record) is True


def test_partial_usage_keeps_the_counts_it_has():
    adapter = _adapter(_good(usage={"total_tokens": 7}))
    assert adapter.explain_failure(FAILING_CHECKS) == "the answer"
    assert adapter.pending_calls()[0]["usage"] == {
        "prompt_tokens": None, "completion_tokens": None,
        "total_tokens": 7}


def test_non_string_model_falls_back_to_the_configured_model():
    adapter = _adapter(_good(model=12345))
    assert adapter.explain_failure(FAILING_CHECKS) == "the answer"
    assert adapter.pending_calls()[0]["model"] == DEFAULT_MODEL


def test_zero_token_counts_are_legitimate():
    adapter = _adapter(_good(usage={
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}))
    assert adapter.explain_failure(FAILING_CHECKS) == "the answer"
    assert adapter.pending_calls()[0]["usage"]["total_tokens"] == 0
