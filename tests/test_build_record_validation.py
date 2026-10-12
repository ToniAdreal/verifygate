"""Sealing-argument validation for ``build_record`` (v1.65.0).

``build_record`` is the one function that assembles and seals an
evidence record. The runner normalises everything it seals, but
that protects the runner's callers, not this function's: a direct
caller handing the assembler a malformed ``llm_usage`` crashed
inside its own usage sum — a non-object call or a non-object
``usage`` with an ``AttributeError``, a string token count with a
``TypeError`` — and a negative token count sealed a usage entry
whose total the consistency mirror then rejects, so the record
failed its own verification the moment it was written. A
non-object check entry or diagnosis crashed the same way, on the
mapping spread.

The assembler now validates exactly the shapes it computes over:
``check_results`` a list of objects, ``diagnosis`` an object or
``None``, and ``llm_usage`` a list of objects whose ``usage`` is
an object or ``None`` carrying only non-negative real-int (never
bool) token counts or ``None``. Anything else is one
``ValueError`` before anything is sealed. Field-*value*
consistency (a verdict against its checks, the caps, the header's
mode and hashes) deliberately stays the verification mirror's
job — the mirror's own tests seal value-level nonsense through
this function to prove the mirror catches it.
"""

from __future__ import annotations

import pytest

from verifygate.evidence import build_record, verify_chain

CHECK = {
    "name": "unit_tests", "command": "python3 -m pytest -q",
    "exit_code": 0, "duration_ms": 12, "stdout_tail": "",
    "stderr_tail": "", "timed_out": False, "verdict": "PASS",
}

DIAGNOSIS = {
    "explanation": "it broke", "references": [],
    "llm_adapter": "mock-deterministic", "reference_source": "mock",
    "reference_error": None,
}

USAGE_CALL = {
    "adapter": "nebius-token-factory", "model": "m",
    "purpose": "explain_failure", "finish_reason": "stop",
    "usage": {"prompt_tokens": 1, "completion_tokens": 1,
              "total_tokens": 2},
}


def _build(**overrides):
    args = dict(
        spec_name="demo", spec_hash="a" * 64,
        candidate_tree_hash="b" * 64, mode="judge",
        check_results=[dict(CHECK)], verdict="PASS", diagnosis=None,
        replay_command="python3 -m verifygate run --spec s "
                       "--candidate c --mode judge",
        llm_usage=None)
    args.update(overrides)
    return build_record(**args)


# -- genuine shapes are unchanged -----------------------------------


def test_genuine_record_seals_and_verifies():
    record = _build(diagnosis=dict(DIAGNOSIS),
                    llm_usage=[dict(USAGE_CALL)])
    assert verify_chain(record)
    usage = [e for e in record["entries"] if e["kind"] == "llm_usage"]
    assert usage[0]["total_tokens"] == 2


def test_tuple_inputs_are_accepted():
    record = _build(check_results=(dict(CHECK),),
                    llm_usage=(dict(USAGE_CALL),))
    assert verify_chain(record)


def test_usage_with_missing_or_none_counts_seals():
    # A call the API answered without a usage object, and one with
    # partial / None counts, are genuine shapes: the total treats
    # them as 0, exactly as before.
    record = _build(llm_usage=[
        {"adapter": "x", "model": "m", "purpose": "p"},
        {"adapter": "x", "model": "m", "purpose": "p", "usage": None},
        {"adapter": "x", "model": "m", "purpose": "p",
         "usage": {"total_tokens": None}},
        {"adapter": "x", "model": "m", "purpose": "p",
         "usage": {"total_tokens": 0}},
    ])
    assert verify_chain(record)
    usage = [e for e in record["entries"] if e["kind"] == "llm_usage"]
    assert usage[0]["total_tokens"] == 0


# -- check_results / diagnosis shapes --------------------------------


@pytest.mark.parametrize("bad", [
    None, "checks", 123, {"name": "x"}, ["x"], [None], [[]],
    [dict(CHECK), "x"],
])
def test_malformed_check_results_are_value_errors(bad):
    with pytest.raises(ValueError):
        _build(check_results=bad)


@pytest.mark.parametrize("bad", ["oops", 123, [], ["x"], True])
def test_malformed_diagnosis_is_a_value_error(bad):
    with pytest.raises(ValueError):
        _build(diagnosis=bad)


# -- llm_usage shapes --------------------------------------------------


@pytest.mark.parametrize("bad", [
    "calls", 123, {"a": 1}, ["x"], [None], [[]], [dict(USAGE_CALL), 7],
])
def test_malformed_llm_usage_is_a_value_error(bad):
    with pytest.raises(ValueError):
        _build(llm_usage=bad)


@pytest.mark.parametrize("bad_usage", ["lots", 5, [], True])
def test_non_object_usage_is_a_value_error(bad_usage):
    call = dict(USAGE_CALL, usage=bad_usage)
    with pytest.raises(ValueError):
        _build(llm_usage=[call])


@pytest.mark.parametrize("field", [
    "prompt_tokens", "completion_tokens", "total_tokens",
])
@pytest.mark.parametrize("bad_count", ["5", 2.5, True, False, -1, -300])
def test_malformed_token_counts_are_value_errors(field, bad_count):
    usage = {"prompt_tokens": 1, "completion_tokens": 1,
             "total_tokens": 2}
    usage[field] = bad_count
    with pytest.raises(ValueError):
        _build(llm_usage=[dict(USAGE_CALL, usage=usage)])


def test_negative_total_no_longer_seals_a_self_failing_record():
    # The pre-fix regression in one line: this sealed a record
    # whose sealed total (-3) the consistency mirror rejects.
    with pytest.raises(ValueError):
        _build(llm_usage=[dict(USAGE_CALL, usage={"total_tokens": -3})])


# -- the deliberate boundary: value consistency stays the mirror's job


def test_value_level_nonsense_still_seals_and_the_mirror_judges_it():
    # A verdict that disagrees with its checks is not the
    # assembler's business (the runner derives it from validated
    # inputs); it seals, and verify_chain — the mirror — fails it.
    record = _build(verdict="FAIL")
    assert not verify_chain(record)
