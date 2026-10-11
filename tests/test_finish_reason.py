"""Token Factory finish-reason normalisation (v1.34.0).

``finish_reason`` was the one field the real adapter recorded from
a decoded Token Factory response with no validation at all: the
v1.18.0 response-shape hardening validated the message content and
the usage counts, and v1.29.0 capped the recorded model name, but
``choice.get("finish_reason")`` was copied into the usage log
verbatim. The evidence structure check requires a sealed call's
``finish_reason`` to be a string within ``MAX_FINISH_REASON_CHARS``
or ``None`` — so a response carrying an int / object / list / bool
finish reason, or a 500,000-character one, produced a perfectly
usable completion whose call was sealed into a record that then
failed its own verification (``verify_chain`` False on a record
VerifyGate had just emitted, all five shapes reproduced against
v1.33.0 before the fix).

The adapter now normalises the field in ``_response_parts``, the
same way it already handled the model name: a string within the
cap is kept verbatim, anything else is recorded as ``None`` (the
neutral value the structure check accepts for an unstated finish
reason). The completion is never rejected over this metadata.

All tests inject a fake transport — zero network, zero credentials.
"""

from pathlib import Path

import pytest

from verifygate.evidence import MAX_FINISH_REASON_CHARS, verify_chain
from verifygate.mock_llm import _SAMPLE_SLUGIFY_DIFF
from verifygate.repair import run_repair
from verifygate.runner import run_verification
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup
from verifygate.tokenfactory import DEFAULT_MODEL, TokenFactoryAdapter
from verifygate.verify import document_ok, verify_document

APP = Path(__file__).resolve().parent.parent
SPEC = load_spec(APP / "samples" / "spec.json")
FAILING_CHECKS = [{"name": "c", "verdict": "FAIL", "command": "pytest"}]


def _payload(finish_reason, content="the answer") -> dict:
    return {
        "model": DEFAULT_MODEL,
        "choices": [{
            "message": {"content": content},
            "finish_reason": finish_reason,
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                  "total_tokens": 15},
    }


def _adapter(finish_reason, content="the answer") -> TokenFactoryAdapter:
    payload = _payload(finish_reason, content)
    return TokenFactoryAdapter(transport=lambda body: payload)


# Shapes that must be normalised to None: anything that is not a
# string within the evidence cap. (None itself is in this list as
# the already-legitimate value — it passes through unchanged.)
BAD_FINISHES = [
    123, 0, True, {"reason": "stop"}, ["stop"], None,
    "x" * (MAX_FINISH_REASON_CHARS + 1), "x" * 500_000,
]

GOOD_FINISHES = ["stop", "length", "content_filter", "",
                 "x" * MAX_FINISH_REASON_CHARS]


@pytest.mark.parametrize("finish", BAD_FINISHES)
def test_bad_finish_reason_is_recorded_as_none(finish):
    adapter = _adapter(finish)
    assert adapter.explain_failure(FAILING_CHECKS) == "the answer"
    calls = adapter.pending_calls()
    assert len(calls) == 1
    assert calls[0]["finish_reason"] is None


@pytest.mark.parametrize("finish", GOOD_FINISHES)
def test_good_finish_reason_is_kept_verbatim(finish):
    adapter = _adapter(finish)
    assert adapter.explain_failure(FAILING_CHECKS) == "the answer"
    assert adapter.pending_calls()[0]["finish_reason"] == finish


@pytest.mark.parametrize("finish", BAD_FINISHES)
def test_bad_finish_reason_normalised_for_patch_generation_too(finish):
    adapter = _adapter(finish)
    assert adapter.generate_patch(
        "fix it", {"verdict": "FAIL", "checks": []}) == "the answer"
    assert adapter.pending_calls()[0]["finish_reason"] is None


@pytest.mark.parametrize("finish", BAD_FINISHES + GOOD_FINISHES)
def test_record_sealed_over_any_finish_reason_verifies(finish):
    adapter = _adapter(finish)
    record = run_verification(
        spec=SPEC, candidate_dir=APP / "samples" / "failing",
        mode="real", llm=adapter, references=MockReferenceLookup(),
        spec_label="samples/spec.json",
        candidate_label="samples/failing",
    )
    assert record["verdict"] == "FAIL"
    usage = next(e for e in record["entries"] if e["kind"] == "llm_usage")
    recorded = usage["calls"][0]["finish_reason"]
    assert recorded is None or (
        isinstance(recorded, str)
        and len(recorded) <= MAX_FINISH_REASON_CHARS)
    assert verify_chain(record)


def test_empty_completion_error_names_no_huge_finish_reason():
    # The empty-completion retry path formats the finish reason into
    # its RuntimeError; a 500,000-character reason must not ride into
    # that message (the runner/repair caps format exception text,
    # but the adapter's own message should be bounded at the source).
    adapter = _adapter("x" * 500_000, content="")
    with pytest.raises(RuntimeError, match="empty completion") as excinfo:
        adapter.explain_failure(FAILING_CHECKS)
    assert len(str(excinfo.value)) < 500
    assert "finish_reason=None" in str(excinfo.value)
    # Both attempts were still recorded — with the normalised value.
    assert [c["finish_reason"] for c in adapter.pending_calls()] == [None, None]


def test_repair_loop_seals_verifying_report_over_bad_finish_reason():
    # End to end through the repair loop: the transport answers the
    # patch call with the sample's known-good diff and the diagnosis
    # call with prose, both carrying a malformed finish reason.
    def transport(body):
        text = str(body)
        content = _SAMPLE_SLUGIFY_DIFF if "unified diff" in text \
            else "diagnosis text"
        return _payload(123, content=content)

    adapter = TokenFactoryAdapter(transport=transport)
    report = run_repair(
        spec=SPEC, candidate_dir=APP / "samples" / "failing",
        mode="real", llm=adapter, references=MockReferenceLookup(),
        spec_label="samples/spec.json",
        candidate_label="samples/failing", max_rounds=3,
    )
    assert report["final_verdict"] == "PASS"
    assert document_ok(verify_document(report))
    for attempt in report["attempts"]:
        for entry in attempt["evidence"]["entries"]:
            if entry["kind"] == "llm_usage":
                for call in entry["calls"]:
                    assert call["finish_reason"] is None
