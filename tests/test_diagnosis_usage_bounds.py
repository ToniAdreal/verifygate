"""Diagnosis/usage metadata bounds: the last unbounded sealed text.

Before v1.29.0 two entry kinds still carried unbounded text:

* a diagnosis entry's ``llm_adapter`` / ``reference_source`` names
  and ``reference_error`` / ``diagnosis_error`` notes were checked
  only for being strings;
* an ``llm_usage`` entry's calls were checked only for their
  ``usage`` totals — their count, and their ``adapter`` / ``model``
  / ``purpose`` / ``finish_reason`` strings, were unbounded. A
  record honestly sealed by ``build_record`` over 500,000-character
  adapter/error strings plus 50 calls with 500,000-character model
  names verified INTACT at ~100 MB.

Contract pinned here:
* ``evidence``'s structure check mirrors generous ceilings over
  all of it (adapter/lookup names, error notes, calls per entry,
  per-call keys, and every string a call carries), so an
  over-limit record fails verification however it was sealed —
  re-chained and re-signed included — while a record at every
  ceiling still verifies;
* the two construction paths that could exceed a ceiling are
  capped at the source: the runner truncates a lookup's
  ``last_error`` on the one sealing path, and the Token Factory
  adapter falls back to its configured model for an over-long
  API-returned model string, exactly as for a non-string one;
* every committed fixture sits far below every cap and still
  verifies.
"""

import hashlib
import hmac
import json
from pathlib import Path

from verifygate import cli
from verifygate.evidence import (
    GENESIS,
    MAX_ADAPTER_NAME_CHARS,
    MAX_CALLS_PER_USAGE_ENTRY,
    MAX_CALL_FIELD_CHARS,
    MAX_CALL_KEYS,
    MAX_ERROR_CHARS,
    MAX_FINISH_REASON_CHARS,
    MAX_MODEL_CHARS,
    MAX_PURPOSE_CHARS,
    _canonical,
    _sha,
    build_record,
    signing_key,
    verify_chain,
)
from verifygate.mock_llm import MockLLMAdapter
from verifygate.runner import _cap_error, run_verification
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup, ReferenceLookup
from verifygate.tokenfactory import TokenFactoryAdapter
from verifygate.verify import document_ok, verify_document
from verifygate.webview import render_document

APP = Path(__file__).resolve().parent.parent

FAIL_CHECK = {
    "name": "unit_tests", "command": "python3 -m pytest -q",
    "exit_code": 1, "duration_ms": 12, "stdout_tail": "",
    "stderr_tail": "boom", "timed_out": False, "verdict": "FAIL",
}

DIAGNOSIS = {
    "explanation": "it broke", "references": [],
    "llm_adapter": "mock-deterministic", "reference_source": "mock",
    "reference_error": None, "diagnosis_error": None,
}

USAGE_CALL = {
    "adapter": "nebius-token-factory", "model": "m",
    "purpose": "explain_failure", "finish_reason": "stop",
    "usage": {"prompt_tokens": 1, "completion_tokens": 1,
              "total_tokens": 2},
}


def _record(diagnosis=None, usage=None):
    return build_record(
        spec_name="demo", spec_hash="a" * 64,
        candidate_tree_hash="b" * 64, mode="judge",
        check_results=[dict(FAIL_CHECK)], verdict="FAIL",
        diagnosis=diagnosis,
        replay_command="python3 -m verifygate run --spec s "
                       "--candidate c --mode judge",
        llm_usage=usage)


def _rechain(record):
    """Recompute a (chain v3) record's chain + signature in place."""
    prev = _sha(GENESIS)
    for entry in record["entries"]:
        payload = {k: v for k, v in entry.items() if k != "hash"}
        prev = _sha(prev + _canonical(payload))
        entry["hash"] = prev
    record["record_hash"] = _sha(prev + _canonical({
        "verdict": record["verdict"],
        "version": record["version"],
        "created_at": record["created_at"],
        "replay": record["replay"],
        "signing_key_is_default_dev_key":
            record["signing_key_is_default_dev_key"],
    }))
    record["signature"] = hmac.new(
        signing_key().encode("utf-8"),
        record["record_hash"].encode("utf-8"),
        hashlib.sha256).hexdigest()
    return record


def _diagnosis(record):
    return next(e for e in record["entries"] if e["kind"] == "diagnosis")


# -- the ceilings themselves -------------------------------------------


def test_ceilings_are_the_documented_values():
    assert MAX_ADAPTER_NAME_CHARS == 100
    assert MAX_MODEL_CHARS == 200
    assert MAX_PURPOSE_CHARS == 100
    assert MAX_FINISH_REASON_CHARS == 100
    assert MAX_ERROR_CHARS == 500
    assert MAX_CALLS_PER_USAGE_ENTRY == 50
    assert MAX_CALL_KEYS == 20
    assert MAX_CALL_FIELD_CHARS == 500


def test_pre_fix_probe_now_fails():
    # The v1.28.0 probe: huge diagnosis metadata + huge usage calls
    # sealed honestly by build_record verified INTACT.
    record = _record(
        diagnosis=dict(
            DIAGNOSIS,
            llm_adapter="A" * 500_000, reference_source="B" * 500_000,
            reference_error="E" * 500_000, diagnosis_error="D" * 500_000),
        usage=[dict(USAGE_CALL, model="M" * 500_000)] * 50)
    assert verify_chain(record) is False


# -- diagnosis metadata ---------------------------------------------------


def test_diagnosis_at_every_ceiling_verifies():
    record = _record(diagnosis=dict(
        DIAGNOSIS,
        llm_adapter="a" * MAX_ADAPTER_NAME_CHARS,
        reference_source="b" * MAX_ADAPTER_NAME_CHARS,
        reference_error="e" * MAX_ERROR_CHARS,
        diagnosis_error="d" * MAX_ERROR_CHARS))
    assert verify_chain(record) is True


def test_diagnosis_name_over_ceiling_fails():
    for field in ("llm_adapter", "reference_source"):
        record = _record(diagnosis=dict(
            DIAGNOSIS, **{field: "x" * (MAX_ADAPTER_NAME_CHARS + 1)}))
        assert verify_chain(record) is False, field


def test_diagnosis_empty_name_fails():
    for field in ("llm_adapter", "reference_source"):
        record = _record(diagnosis=dict(DIAGNOSIS, **{field: ""}))
        assert verify_chain(record) is False, field


def test_diagnosis_error_note_over_ceiling_fails():
    for field in ("reference_error", "diagnosis_error"):
        record = _record(diagnosis=dict(
            DIAGNOSIS, **{field: "x" * (MAX_ERROR_CHARS + 1)}))
        assert verify_chain(record) is False, field


def test_rechained_over_limit_diagnosis_metadata_fails():
    record = _record(diagnosis=dict(DIAGNOSIS))
    _diagnosis(record)["llm_adapter"] = "x" * (MAX_ADAPTER_NAME_CHARS + 1)
    _rechain(record)
    assert verify_chain(record) is False
    assert document_ok(verify_document(record)) is False


# -- llm_usage calls --------------------------------------------------------


def test_usage_at_call_ceiling_verifies():
    record = _record(
        diagnosis=dict(DIAGNOSIS),
        usage=[dict(USAGE_CALL) for _ in range(MAX_CALLS_PER_USAGE_ENTRY)])
    assert verify_chain(record) is True


def test_usage_over_call_ceiling_fails():
    record = _record(
        diagnosis=dict(DIAGNOSIS),
        usage=[dict(USAGE_CALL)
               for _ in range(MAX_CALLS_PER_USAGE_ENTRY + 1)])
    assert verify_chain(record) is False


def test_usage_call_text_over_ceiling_fails():
    caps = {"adapter": MAX_ADAPTER_NAME_CHARS, "model": MAX_MODEL_CHARS,
            "purpose": MAX_PURPOSE_CHARS,
            "finish_reason": MAX_FINISH_REASON_CHARS}
    for field, cap in caps.items():
        record = _record(
            diagnosis=dict(DIAGNOSIS),
            usage=[dict(USAGE_CALL, **{field: "x" * (cap + 1)})])
        assert verify_chain(record) is False, field


def test_usage_call_text_at_ceiling_verifies():
    record = _record(
        diagnosis=dict(DIAGNOSIS),
        usage=[dict(USAGE_CALL, model="m" * MAX_MODEL_CHARS,
                    adapter="a" * MAX_ADAPTER_NAME_CHARS)])
    assert verify_chain(record) is True


def test_usage_call_non_string_known_field_fails():
    record = _record(
        diagnosis=dict(DIAGNOSIS),
        usage=[dict(USAGE_CALL, model=42)])
    assert verify_chain(record) is False


def test_usage_call_finish_reason_none_still_verifies():
    record = _record(
        diagnosis=dict(DIAGNOSIS),
        usage=[dict(USAGE_CALL, finish_reason=None)])
    assert verify_chain(record) is True


def test_usage_call_extra_string_field_bounded_generically():
    ok = _record(diagnosis=dict(DIAGNOSIS), usage=[
        dict(USAGE_CALL, note="n" * MAX_CALL_FIELD_CHARS)])
    assert verify_chain(ok) is True
    over = _record(diagnosis=dict(DIAGNOSIS), usage=[
        dict(USAGE_CALL, note="n" * (MAX_CALL_FIELD_CHARS + 1))])
    assert verify_chain(over) is False


def test_usage_call_with_too_many_keys_fails():
    call = dict(USAGE_CALL)
    call.update({f"k{i}": i for i in range(MAX_CALL_KEYS)})
    assert len(call) > MAX_CALL_KEYS
    record = _record(diagnosis=dict(DIAGNOSIS), usage=[call])
    assert verify_chain(record) is False


def test_over_limit_record_cli_and_webview(tmp_path):
    record = _record(diagnosis=dict(
        DIAGNOSIS, llm_adapter="x" * (MAX_ADAPTER_NAME_CHARS + 1)))
    _rechain(record)
    path = tmp_path / "record.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    assert cli.main(["verify", "--record", str(path)]) == 1
    assert "CHAIN INVALID" in render_document(record)


# -- sealing-path caps ------------------------------------------------------


def test_cap_error_helper():
    assert _cap_error(None) is None
    assert _cap_error("short") == "short"
    assert _cap_error("x" * MAX_ERROR_CHARS) == "x" * MAX_ERROR_CHARS
    assert _cap_error("x" * (MAX_ERROR_CHARS + 1)) == "x" * MAX_ERROR_CHARS


class _HugeErrorLookup(ReferenceLookup):
    name = "huge-error-stub"

    def lookup(self, query, max_results=3):
        self.last_error = "E" * 500_000
        return []


def test_runner_caps_a_lookups_last_error_and_record_verifies():
    record = run_verification(
        spec=load_spec(APP / "samples" / "spec.json"),
        candidate_dir=APP / "samples" / "failing",
        mode="judge", llm=MockLLMAdapter(), references=_HugeErrorLookup(),
        spec_label="samples/spec.json", candidate_label="samples/failing")
    diag = _diagnosis(record)
    assert diag["reference_error"] == "E" * MAX_ERROR_CHARS
    assert verify_chain(record) is True
    assert document_ok(verify_document(record)) is True


def _fake_transport(model_value):
    def transport(body):
        return {
            "model": model_value,
            "choices": [{
                "message": {"content": "a diagnosis"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4,
                      "total_tokens": 7},
        }
    return transport


def test_tokenfactory_overlong_model_falls_back_to_configured():
    adapter = TokenFactoryAdapter(
        model="nvidia/configured-model",
        transport=_fake_transport("M" * 500_000))
    adapter.explain_failure([dict(FAIL_CHECK)])
    (call,) = adapter.drain_calls()
    assert call["model"] == "nvidia/configured-model"


def test_tokenfactory_normal_model_string_is_recorded():
    adapter = TokenFactoryAdapter(
        model="nvidia/configured-model",
        transport=_fake_transport("nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"))
    adapter.explain_failure([dict(FAIL_CHECK)])
    (call,) = adapter.drain_calls()
    assert call["model"] == "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"


# -- committed fixtures -------------------------------------------------------


def _all_entries(obj):
    if isinstance(obj, dict):
        if "kind" in obj and "hash" in obj:
            yield obj
        for value in obj.values():
            yield from _all_entries(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _all_entries(item)


def test_all_committed_fixtures_still_verify_with_headroom():
    fixtures = [
        APP / "evidence_self_check.json",
        APP / "samples" / "evidence_passing.json",
        APP / "samples" / "evidence_failing.json",
        APP / "samples" / "evidence_real_fail.json",
        APP / "samples" / "evidence_real_fail_v2.json",
        APP / "samples" / "evidence_real_repair.json",
        APP / "samples" / "evidence_real_repair_v2.json",
        APP / "samples" / "evidence_real_repair_v3.json",
        APP / "samples-js" / "evidence_passing.json",
        APP / "samples-js" / "evidence_failing.json",
        APP / "samples-js" / "evidence_real_repair.json",
        APP / "samples-js" / "evidence_real_repair_v3.json",
    ]
    for path in fixtures:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert document_ok(verify_document(data)), path
        for entry in _all_entries(data):
            if entry.get("kind") == "diagnosis":
                # Committed names peak at 20 chars, error notes
                # at 79 — at most a fifth of either ceiling.
                assert len(entry["llm_adapter"]) * 5 \
                    <= MAX_ADAPTER_NAME_CHARS, path
                assert len(entry["reference_source"]) * 5 \
                    <= MAX_ADAPTER_NAME_CHARS, path
                for field in ("reference_error", "diagnosis_error"):
                    note = entry.get(field)
                    if isinstance(note, str):
                        assert len(note) * 5 <= MAX_ERROR_CHARS, path
            if entry.get("kind") == "llm_usage":
                # Committed maximum is 2 calls per entry.
                assert len(entry["calls"]) * 10 \
                    <= MAX_CALLS_PER_USAGE_ENTRY, path
                for call in entry["calls"]:
                    assert len(call["model"]) * 5 <= MAX_MODEL_CHARS, path
                    assert len(call["adapter"]) * 5 \
                        <= MAX_ADAPTER_NAME_CHARS, path
