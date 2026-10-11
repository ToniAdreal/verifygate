"""Usage sealing normalisation (v1.35.0).

The v1.29.0 ceilings bounded what a sealed record may *carry*, and
the first-party construction paths were capped at the source — but
the runner, the one sealing path, still sealed adapter-supplied
metadata verbatim (all shapes below reproduced against v1.34.0):

* an adapter (or lookup) whose ``name`` was empty or 500,000
  characters long was sealed into the diagnosis entry, which then
  failed the structure check's name rule;
* a recorded usage call carrying a 500,000-character model string
  or an int ``finish_reason`` sealed a record that failed its own
  verification;
* a recorded call whose ``usage`` was not an object at all crashed
  ``build_record`` with an ``AttributeError`` — the FAIL record was
  lost entirely, the v1.19.0 loss class one field over;
* a *configured* Token Factory model id of any length was recorded
  verbatim whenever the API returned no usable model name, reaching
  the same self-failing record through the fallback path.

Contract pinned here: the sealing paths normalise through
``evidence.normalise_adapter_name`` / ``normalise_usage_calls``
(the runner for records; the repair loop for its report-level
snapshots, unsealed usage, and credited adapter name), honest
first-party values pass through unchanged, and the Token Factory
adapter rejects an empty/non-string/over-long configured model id
at construction with ``ValueError`` (the CLI's exit-2 adapter
error).
"""

import copy
from pathlib import Path

import pytest

from verifygate import cli
from verifygate.evidence import (
    MAX_ADAPTER_NAME_CHARS,
    MAX_CALLS_PER_USAGE_ENTRY,
    MAX_CALL_FIELD_CHARS,
    MAX_CALL_KEYS,
    MAX_FINISH_REASON_CHARS,
    MAX_MODEL_CHARS,
    MAX_PURPOSE_CHARS,
    normalise_adapter_name,
    normalise_usage_calls,
    verify_chain,
)
from verifygate.llm import LLMAdapter
from verifygate.mock_llm import MockLLMAdapter, _SAMPLE_SLUGIFY_DIFF
from verifygate.repair import run_repair
from verifygate.runner import run_verification
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup, ReferenceLookup
from verifygate.tokenfactory import TokenFactoryAdapter
from verifygate.verify import document_ok, verify_document

APP = Path(__file__).resolve().parent.parent
SPEC = load_spec(APP / "samples" / "spec.json")
FAILING = APP / "samples" / "failing"

HONEST_CALL = {
    "adapter": "nebius-token-factory",
    "model": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
    "purpose": "explain_failure",
    "finish_reason": "stop",
    "usage": {"prompt_tokens": 10, "completion_tokens": 5,
              "total_tokens": 15},
}


def _run(llm, references=None):
    return run_verification(
        spec=SPEC, candidate_dir=FAILING, mode="judge", llm=llm,
        references=references or MockReferenceLookup(),
        spec_label="samples/spec.json", candidate_label="samples/failing")


def _usage_entry(record):
    return next(e for e in record["entries"] if e["kind"] == "llm_usage")


def _diagnosis_entry(record):
    return next(e for e in record["entries"] if e["kind"] == "diagnosis")


class _Stub(LLMAdapter):
    """Adapter with a configurable name and pre-recorded calls."""

    def __init__(self, name="stub", calls=()):
        super().__init__()
        self.name = name
        self._calls = list(calls)

    def generate_patch(self, brief, failing_evidence=None):
        return _SAMPLE_SLUGIFY_DIFF

    def explain_failure(self, check_results):
        for call in self._calls:
            self.record_call(copy.deepcopy(call))
        return "a diagnosis"


# -- normalise_adapter_name ---------------------------------------------------


def test_adapter_name_within_cap_is_unchanged():
    assert normalise_adapter_name("nebius-token-factory", "fb") \
        == "nebius-token-factory"
    assert normalise_adapter_name("x" * MAX_ADAPTER_NAME_CHARS, "fb") \
        == "x" * MAX_ADAPTER_NAME_CHARS


def test_adapter_name_over_cap_is_truncated():
    assert normalise_adapter_name("x" * 500_000, "fb") \
        == "x" * MAX_ADAPTER_NAME_CHARS


@pytest.mark.parametrize("bad", ["", None, 123, ["name"], {"n": 1}])
def test_adapter_name_unusable_falls_back(bad):
    assert normalise_adapter_name(bad, "unknown-llm-adapter") \
        == "unknown-llm-adapter"


# -- normalise_usage_calls ------------------------------------------------------


def test_honest_call_passes_through_unchanged():
    assert normalise_usage_calls([copy.deepcopy(HONEST_CALL)]) == [HONEST_CALL]


@pytest.mark.parametrize("raw", [None, "calls", 123, {"adapter": "x"}])
def test_non_list_usage_normalises_to_no_calls(raw):
    assert normalise_usage_calls(raw) == []


def test_non_object_calls_are_dropped():
    assert normalise_usage_calls(["nope", 42, None, HONEST_CALL]) \
        == [HONEST_CALL]


def test_call_count_is_capped():
    calls = [copy.deepcopy(HONEST_CALL)
             for _ in range(MAX_CALLS_PER_USAGE_ENTRY + 400)]
    assert len(normalise_usage_calls(calls)) == MAX_CALLS_PER_USAGE_ENTRY


def test_call_key_count_is_capped():
    call = dict(HONEST_CALL)
    call.update({f"extra_{i}": i for i in range(MAX_CALL_KEYS + 30)})
    (out,) = normalise_usage_calls([call])
    assert len(out) <= MAX_CALL_KEYS


def test_known_text_fields_are_truncated_to_their_caps():
    call = {"adapter": "a" * 500_000, "model": "m" * 500_000,
            "purpose": "p" * 500_000, "usage": {"total_tokens": 1}}
    (out,) = normalise_usage_calls([call])
    assert len(out["adapter"]) == MAX_ADAPTER_NAME_CHARS
    assert len(out["model"]) == MAX_MODEL_CHARS
    assert len(out["purpose"]) == MAX_PURPOSE_CHARS


@pytest.mark.parametrize("bad", [123, True, {"r": 1}, ["stop"], "x" * 500_000])
def test_bad_finish_reason_becomes_none(bad):
    (out,) = normalise_usage_calls([{"finish_reason": bad}])
    assert out["finish_reason"] is None


def test_good_finish_reason_is_kept():
    (out,) = normalise_usage_calls([{"finish_reason": "stop"}])
    assert out["finish_reason"] == "stop"


def test_non_string_known_fields_are_dropped():
    (out,) = normalise_usage_calls(
        [{"adapter": 123, "model": None, "purpose": ["x"]}])
    assert "adapter" not in out and "model" not in out \
        and "purpose" not in out


def test_other_string_fields_are_truncated_and_containers_dropped():
    call = {"note": "n" * 500_000, "blob": {"nested": "x" * 500_000},
            "retries": 2}
    (out,) = normalise_usage_calls([call])
    assert len(out["note"]) == MAX_CALL_FIELD_CHARS
    assert "blob" not in out
    assert out["retries"] == 2


@pytest.mark.parametrize("usage,expected", [
    ("not-a-dict", {}),
    (123, {}),
    ({"total_tokens": "lots", "prompt_tokens": -1,
      "completion_tokens": True, "extra_key": 9},
     {"prompt_tokens": None, "completion_tokens": None,
      "total_tokens": None}),
    ({"prompt_tokens": 3}, {"prompt_tokens": 3,
                            "completion_tokens": None,
                            "total_tokens": None}),
    (None, None),
])
def test_usage_is_rebuilt_as_a_valid_object(usage, expected):
    (out,) = normalise_usage_calls([{"adapter": "a", "usage": usage}])
    assert out["usage"] == expected


# -- the runner's sealing path ---------------------------------------------------


def test_huge_adapter_and_lookup_names_seal_a_verifying_record():
    class HugeLookup(MockReferenceLookup):
        name = "y" * 500_000

    record = _run(_Stub(name="x" * 500_000), references=HugeLookup())
    assert record["verdict"] == "FAIL"
    diag = _diagnosis_entry(record)
    assert diag["llm_adapter"] == "x" * MAX_ADAPTER_NAME_CHARS
    assert diag["reference_source"] == "y" * MAX_ADAPTER_NAME_CHARS
    assert verify_chain(record) is True
    assert document_ok(verify_document(record)) is True


def test_empty_adapter_name_falls_back_in_the_record():
    record = _run(_Stub(name=""))
    assert _diagnosis_entry(record)["llm_adapter"] == "unknown-llm-adapter"
    assert verify_chain(record) is True


def test_malformed_usage_call_seals_a_verifying_record():
    bad = {"adapter": "a" * 500_000, "model": "m" * 500_000,
           "purpose": "explain_failure", "finish_reason": 123,
           "usage": {"prompt_tokens": 1, "completion_tokens": 2,
                     "total_tokens": 3}}
    record = _run(_Stub(calls=[bad]))
    usage = _usage_entry(record)
    assert usage["total_tokens"] == 3
    assert usage["calls"][0]["finish_reason"] is None
    assert len(usage["calls"][0]["model"]) == MAX_MODEL_CHARS
    assert verify_chain(record) is True
    assert document_ok(verify_document(record)) is True


def test_non_object_usage_no_longer_crashes_the_run():
    # Pre-fix this raised AttributeError inside build_record and the
    # FAIL record was lost.
    record = _run(_Stub(calls=[{"adapter": "stub",
                                 "usage": "not-a-dict"}]))
    assert record["verdict"] == "FAIL"
    assert _usage_entry(record)["calls"][0]["usage"] == {}
    assert verify_chain(record) is True


def test_non_object_call_in_the_log_is_dropped_not_sealed():
    record = _run(_Stub(calls=["garbage", copy.deepcopy(HONEST_CALL)]))
    assert _usage_entry(record)["calls"] == [HONEST_CALL]
    assert verify_chain(record) is True


def test_first_party_mock_records_are_unchanged():
    # Judge Mode regression at the unit level: the mock records no
    # calls, so no usage entry appears and the diagnosis names are
    # the mock's own.
    record = _run(MockLLMAdapter())
    assert all(e["kind"] != "llm_usage" for e in record["entries"])
    assert _diagnosis_entry(record)["llm_adapter"] == "mock-deterministic"
    assert verify_chain(record) is True


# -- the repair loop's report-level copies ---------------------------------------


def test_repair_report_normalises_names_snapshots_and_unsealed():
    stub = _Stub(name="x" * 500_000, calls=[{
        "adapter": "x" * 500_000, "model": "m" * 500_000,
        "purpose": "generate_patch", "finish_reason": ["stop"],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                  "total_tokens": 2}}])
    report = run_repair(
        spec=SPEC, candidate_dir=FAILING, mode="judge", llm=stub,
        references=MockReferenceLookup(),
        spec_label="samples/spec.json", candidate_label="samples/failing",
        max_rounds=3)
    assert report["final_verdict"] == "PASS"
    assert report["llm_adapter"] == "x" * MAX_ADAPTER_NAME_CHARS
    assert document_ok(verify_document(report)) is True


def test_repair_generation_failure_snapshot_is_normalised():
    class Exploding(LLMAdapter):
        name = "z" * 500_000

        def generate_patch(self, brief, failing_evidence=None):
            self.record_call({"adapter": "z" * 500_000,
                              "usage": "not-a-dict"})
            raise RuntimeError("boom")

        def explain_failure(self, check_results):
            return "a diagnosis"

    report = run_repair(
        spec=SPEC, candidate_dir=FAILING, mode="judge", llm=Exploding(),
        references=MockReferenceLookup(),
        spec_label="samples/spec.json", candidate_label="samples/failing",
        max_rounds=3)
    assert report["stopped_reason"] == "generation_failed"
    failed = report["failed_patch"]
    assert failed["llm_adapter"] == "z" * MAX_ADAPTER_NAME_CHARS
    assert failed["llm_usage"][0]["usage"] == {}
    assert document_ok(verify_document(report)) is True


# -- the Token Factory configured model -------------------------------------------


def _transport(body):
    return {"choices": [{"message": {"content": "diag"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                      "total_tokens": 2}}


def test_configured_model_at_the_cap_is_accepted():
    adapter = TokenFactoryAdapter(model="m" * MAX_MODEL_CHARS,
                                  transport=_transport)
    assert adapter.model == "m" * MAX_MODEL_CHARS


@pytest.mark.parametrize("bad", ["m" * (MAX_MODEL_CHARS + 1),
                                 "m" * 500_000, 123, ["model"]])
def test_configured_model_over_the_cap_or_non_string_is_rejected(bad):
    with pytest.raises(ValueError, match="model id"):
        TokenFactoryAdapter(model=bad, transport=_transport)


def test_cli_rejects_an_over_long_model_with_exit_2(capsys):
    rc = cli.main(["run", "--spec", str(APP / "samples" / "spec.json"),
                   "--candidate", str(FAILING), "--mode", "real",
                   "--model", "m" * 500_000])
    assert rc == 2
    assert "adapter error" in capsys.readouterr().err
