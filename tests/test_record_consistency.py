"""Record consistency (v1.12.0): a record must agree with itself.

Structure validation used to check that fields exist (and, since
v1.7.0, that a check's name/command are strings), but never that a
record is internally consistent. Every case below was reproduced
against v1.11.0, where each one verified INTACT — the hash chain
faithfully authenticates whatever it is fed, and the signing key is
a labelled *public* dev key, so re-chaining a forgery is not a
barrier either:

* a PASS verdict sealed over FAIL checks (and the mirror image, and
  a verdict that is not PASS/FAIL at all);
* a check claiming PASS with exit code 1, FAIL with exit code 0, or
  PASS on a timeout — verdicts ``validators`` can never emit, since
  a check's verdict is a pure function of its exit code / timeout;
* field values no real run produces: a negative or non-numeric
  ``duration_ms``, ``exit_code: true`` (bool is an int subclass),
  a non-boolean ``timed_out``, a non-string output tail, a header
  naming a mode that does not exist or hashes that are not SHA-256
  hex, a diagnosis whose explanation is not a string or whose
  references are not the adapters' normalised shape;
* an ``llm_usage`` entry whose stated ``total_tokens`` disagrees
  with the sum of its own calls — a forged token bill, re-chained
  and re-signed so only the consistency check can catch it.

Verification now recomputes each of these from the record's own
parts, mirroring ``validators`` / ``overall_verdict`` /
``build_record``. Every runner-emitted record — including all the
committed fixtures, chain v1 and v2 — satisfies the rules.
"""

import copy
import hashlib
import hmac
import json
from pathlib import Path

import pytest

from verifygate.cli import main
from verifygate.evidence import (
    GENESIS,
    _canonical,
    _sha,
    build_record,
    signing_key,
    verify_chain,
)
from verifygate.verify import verify_document
from verifygate.webview import render_document

ROOT = Path(__file__).resolve().parent.parent

PASS_CHECK = {
    "name": "c1", "command": "true", "exit_code": 0, "duration_ms": 1,
    "stdout_tail": "", "stderr_tail": "", "timed_out": False,
    "verdict": "PASS",
}
FAIL_CHECK = dict(PASS_CHECK, exit_code=1, verdict="FAIL")
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


def _record(checks, verdict, diagnosis=None, usage=None, **header):
    return build_record(
        spec_name="demo",
        spec_hash=header.get("spec_hash", "a" * 64),
        candidate_tree_hash="b" * 64,
        mode=header.get("mode", "judge"),
        check_results=checks, verdict=verdict, diagnosis=diagnosis,
        replay_command="python3 -m verifygate run",
        llm_usage=usage)


def _rechain(record):
    """Recompute a (chain v2) record's chain + signature in place.

    The dev signing key is public by design, so a forged record can
    always be re-sealed; these tests use that route to prove the
    *consistency* rules — not the chain — are what catches it.
    """
    prev = _sha(GENESIS)
    for entry in record["entries"]:
        payload = {k: v for k, v in entry.items() if k != "hash"}
        prev = _sha(prev + _canonical(payload))
        entry["hash"] = prev
    record["record_hash"] = _sha(
        prev + _canonical({"verdict": record["verdict"]}))
    record["signature"] = hmac.new(
        signing_key().encode("utf-8"),
        record["record_hash"].encode("utf-8"),
        hashlib.sha256).hexdigest()
    return record


def _usage_record():
    return _record([FAIL_CHECK], "FAIL", diagnosis=DIAGNOSIS,
                   usage=[dict(USAGE_CALL)])


# -- the verdict must be the one the checks imply ----------------------

def test_pass_verdict_over_fail_check_no_longer_verifies():
    assert verify_chain(_record([FAIL_CHECK], "PASS")) is False


def test_fail_verdict_over_pass_checks_no_longer_verifies():
    assert verify_chain(_record([PASS_CHECK], "FAIL")) is False


def test_mixed_checks_require_fail_verdict():
    checks = [PASS_CHECK, dict(FAIL_CHECK, name="c2")]
    assert verify_chain(_record(checks, "PASS")) is False
    assert verify_chain(_record(checks, "FAIL")) is True


@pytest.mark.parametrize("verdict", ["MAYBE", "pass", "", None, 0])
def test_nonsense_verdict_no_longer_verifies(verdict):
    assert verify_chain(_record([PASS_CHECK], verdict)) is False


def test_rechained_verdict_flip_fails_verification_and_cli(tmp_path, capsys):
    record = _rechain(_record([FAIL_CHECK], "PASS"))
    assert verify_chain(record) is False
    results = verify_document(record)
    assert not all(r["chain_ok"] for _, r in results)
    out = tmp_path / "forged.json"
    out.write_text(json.dumps(record), encoding="utf-8")
    assert main(["verify", "--record", str(out)]) == 1
    assert "CHAIN INVALID" in render_document(record)


# -- a check's verdict must be the one its exit code implies -----------

@pytest.mark.parametrize("check,verdict", [
    (dict(PASS_CHECK, exit_code=1), "PASS"),       # PASS claimed, exit 1
    (dict(PASS_CHECK, verdict="FAIL"), "FAIL"),    # FAIL claimed, exit 0
    (dict(PASS_CHECK, timed_out=True), "PASS"),    # PASS claimed, timed out
    (dict(PASS_CHECK, exit_code=None), "FAIL"),    # no exit, no timeout
    (dict(FAIL_CHECK, exit_code=0), "PASS"),       # exit rewritten to 0
])
def test_check_verdict_must_match_exit_code(check, verdict):
    diagnosis = DIAGNOSIS if verdict == "FAIL" else None
    assert verify_chain(_record([check], verdict,
                                  diagnosis=diagnosis)) is False


def test_timeout_check_is_a_consistent_fail():
    check = dict(PASS_CHECK, exit_code=None, timed_out=True,
                 verdict="FAIL")
    assert verify_chain(_record([check], "FAIL",
                                diagnosis=DIAGNOSIS)) is True


def test_signal_exit_code_is_a_consistent_fail():
    # A check killed by a signal reports a negative exit code; that
    # is a real shape run_command can seal, not an inconsistency.
    check = dict(FAIL_CHECK, exit_code=-9)
    assert verify_chain(_record([check], "FAIL",
                                diagnosis=DIAGNOSIS)) is True


# -- field values a real run never produces ------------------------------

@pytest.mark.parametrize("check", [
    dict(PASS_CHECK, duration_ms=-1),
    dict(PASS_CHECK, duration_ms="fast"),
    dict(PASS_CHECK, duration_ms=True),
    dict(PASS_CHECK, exit_code=True),
    dict(PASS_CHECK, exit_code="0"),
    dict(PASS_CHECK, timed_out="no"),
    dict(PASS_CHECK, stdout_tail=123),
    dict(PASS_CHECK, stderr_tail=None),
    dict(PASS_CHECK, extra_field="injected"),
])
def test_unemittable_check_values_no_longer_verify(check):
    verdict = check["verdict"]
    diagnosis = DIAGNOSIS if verdict == "FAIL" else None
    assert verify_chain(_record([check], verdict,
                                  diagnosis=diagnosis)) is False


@pytest.mark.parametrize("header", [
    {"mode": "banana"},
    {"mode": ""},
    {"spec_hash": "not-a-hash"},
    {"spec_hash": "a" * 63},
    {"spec_hash": "A" * 64},  # hexdigest output is lowercase
])
def test_unemittable_header_values_no_longer_verify(header):
    assert verify_chain(_record([PASS_CHECK], "PASS", **header)) is False


# -- diagnosis shape ------------------------------------------------------

@pytest.mark.parametrize("diagnosis", [
    dict(DIAGNOSIS, explanation=42),
    dict(DIAGNOSIS, references="none"),
    dict(DIAGNOSIS, references=[{"title": "t"}]),  # ref fields missing
    dict(DIAGNOSIS, references=[{
        "title": "t", "url": "https://x", "snippet": "s",
        "source": "mock", "extra": "y"}]),
    dict(DIAGNOSIS, reference_error=42),
    dict(DIAGNOSIS, injected="field"),
    {k: v for k, v in DIAGNOSIS.items() if k != "llm_adapter"},
])
def test_unemittable_diagnosis_no_longer_verifies(diagnosis):
    record = _record([FAIL_CHECK], "FAIL", diagnosis=diagnosis)
    assert verify_chain(record) is False


def test_legacy_diagnosis_without_reference_error_still_verifies():
    legacy = {k: v for k, v in DIAGNOSIS.items() if k != "reference_error"}
    record = _record([FAIL_CHECK], "FAIL", diagnosis=legacy)
    assert verify_chain(record) is True


def test_diagnosis_with_references_still_verifies():
    diagnosis = dict(DIAGNOSIS, references=[{
        "title": "Docs", "url": "https://docs.python.org/3/",
        "snippet": "s", "source": "mock"}])
    record = _record([FAIL_CHECK], "FAIL", diagnosis=diagnosis)
    assert verify_chain(record) is True


# -- llm_usage: the stated total must be the sum of its calls -------------

def test_usage_total_mismatch_rechained_no_longer_verifies():
    record = _usage_record()
    usage = next(e for e in record["entries"] if e["kind"] == "llm_usage")
    usage["total_tokens"] = 999_999  # forge a grander bill, re-sealed
    assert verify_chain(_rechain(record)) is False


def test_usage_total_as_string_no_longer_verifies():
    record = _usage_record()
    usage = next(e for e in record["entries"] if e["kind"] == "llm_usage")
    usage["total_tokens"] = "lots"
    assert verify_chain(_rechain(record)) is False


@pytest.mark.parametrize("call", [
    {"usage": {"total_tokens": -1}},
    {"usage": {"total_tokens": True}},
    {"usage": {"total_tokens": 2, "mystery_tokens": 5}},
])
def test_unemittable_usage_calls_no_longer_verify(call):
    record = _record([FAIL_CHECK], "FAIL", diagnosis=DIAGNOSIS,
                     usage=[call])
    assert verify_chain(record) is False


@pytest.mark.parametrize("call", [{"usage": "lots"}, "not-a-call"])
def test_usage_shapes_build_record_cannot_seal_no_longer_verify(call):
    # build_record's own sum crashes on these shapes, so they can
    # only arrive as a mutation + re-chain; structure must catch them.
    record = _usage_record()
    usage = next(e for e in record["entries"] if e["kind"] == "llm_usage")
    usage["calls"] = [call]
    assert verify_chain(_rechain(record)) is False


def test_usage_call_without_usage_object_counts_zero():
    # build_record's own sum treats a call with no usage object as 0;
    # the consistency rule mirrors that instead of inventing a shape.
    record = _record([FAIL_CHECK], "FAIL", diagnosis=DIAGNOSIS,
                     usage=[{"adapter": "x", "purpose": "explain_failure"},
                            dict(USAGE_CALL)])
    assert verify_chain(record) is True


def test_usage_call_with_partial_token_counts_still_verifies():
    # The Token Factory adapter may record None for a count the API
    # did not return, and test stubs record total_tokens only.
    call = {"adapter": "x", "usage": {"total_tokens": 7}}
    record = _record([FAIL_CHECK], "FAIL", diagnosis=DIAGNOSIS,
                     usage=[call])
    assert verify_chain(record) is True
    call2 = dict(USAGE_CALL, usage={
        "prompt_tokens": None, "completion_tokens": None,
        "total_tokens": None})
    record = _record([FAIL_CHECK], "FAIL", diagnosis=DIAGNOSIS,
                     usage=[call2])
    assert verify_chain(record) is True


# -- nothing legitimate broke ----------------------------------------------

def test_well_formed_records_still_verify():
    assert verify_chain(_record([PASS_CHECK], "PASS")) is True
    assert verify_chain(_usage_record()) is True


def test_all_committed_fixtures_still_verify():
    fixtures = [
        ROOT / "samples" / "evidence_passing.json",
        ROOT / "samples" / "evidence_failing.json",
        ROOT / "samples" / "evidence_real_fail.json",
        ROOT / "samples" / "evidence_real_repair.json",
        ROOT / "samples" / "evidence_real_repair_v2.json",
        ROOT / "samples-js" / "evidence_passing.json",
        ROOT / "samples-js" / "evidence_failing.json",
        ROOT / "samples-js" / "evidence_real_repair.json",
        ROOT / "evidence_self_check.json",
    ]
    for path in fixtures:
        data = json.loads(path.read_text(encoding="utf-8"))
        results = verify_document(data)
        assert all(r["chain_ok"] for _, r in results), path


def test_copy_of_fixture_is_not_mutated_by_verification():
    data = json.loads(
        (ROOT / "samples" / "evidence_real_repair_v2.json").read_text(
            encoding="utf-8"))
    snapshot = copy.deepcopy(data)
    verify_document(data)
    assert data == snapshot
