"""Explanation bounds: the diagnosis text is a bounded dimension.

Before v1.27.0 the diagnosis *references* were capped (v1.26.0)
but the diagnosis *text itself* was not — the runner sealed
whatever ``explain_failure`` returned, verbatim and at any length:

* a stub adapter returning a 500,000-character explanation sealed
  a ~500 KB record that verified INTACT;
* even first-party text could run away: the bundled mock renders
  per failing check, so a maximum-size all-failing spec produced a
  ~67,000-character explanation, and the runner's own deterministic
  fallback rendered ~33,000 characters on the same shape.

Contract pinned here:
* ``runner.normalise_explanation`` bounds the text on the one
  sealing path — model, mock, and fallback alike — to
  ``MAX_EXPLANATION_CHARS``, marking the truncation in the sealed
  text itself; a text at or under the cap is sealed byte-identical;
* ``evidence``'s structure check mirrors the cap, so a record
  carrying a longer explanation fails verification however it was
  sealed (re-chained and re-signed included), while every committed
  fixture — whose longest diagnosis is 2,022 characters — still
  verifies.
"""

import hashlib
import hmac
import json
from pathlib import Path

from verifygate import cli
from verifygate.evidence import (
    GENESIS,
    MAX_EXPLANATION_CHARS,
    _canonical,
    _sha,
    signing_key,
    verify_chain,
)
from verifygate.llm import LLMAdapter
from verifygate.mock_llm import MockLLMAdapter
from verifygate.runner import normalise_explanation, run_verification
from verifygate.spec import load_spec, parse_spec
from verifygate.tavily import MockReferenceLookup
from verifygate.verify import document_ok, verify_document
from verifygate.webview import render_document

APP = Path(__file__).resolve().parent.parent
MARKER = "[Diagnosis truncated by VerifyGate"


class _StubLLM(LLMAdapter):
    name = "stub"

    def __init__(self, text):
        super().__init__()
        self._text = text

    def generate_patch(self, brief, failing_evidence=None):
        return ""

    def explain_failure(self, check_results):
        return self._text


class _ExplodingLLM(LLMAdapter):
    name = "exploding"

    def generate_patch(self, brief, failing_evidence=None):
        return ""

    def explain_failure(self, check_results):
        raise RuntimeError("adapter is down")


def _run(llm, spec=None, candidate=None):
    return run_verification(
        spec=spec or load_spec(APP / "samples" / "spec.json"),
        candidate_dir=candidate or (APP / "samples" / "failing"),
        mode="judge", llm=llm, references=MockReferenceLookup(),
        spec_label="samples/spec.json", candidate_label="samples/failing")


def _diagnosis(record):
    return next(e for e in record["entries"] if e["kind"] == "diagnosis")


def _rechain(record):
    """Recompute a (chain v3) record's chain + signature in place.

    Same route as tests/test_record_consistency.py: the dev signing
    key is public by design, so these tests prove the *structure*
    rules — not the chain — are what catches an over-limit text.
    """
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


def _big_spec(n_checks=80):
    return parse_spec({
        "name": "big-spec",
        "brief": "Every check fails.",
        "checks": [
            {"name": f"check_{i}", "command": "false " + "x" * 300,
             "timeout_s": 10}
            for i in range(n_checks)
        ],
    })


def test_normalise_short_text_unchanged():
    assert normalise_explanation("a short diagnosis") == "a short diagnosis"


def test_normalise_at_cap_unchanged():
    text = "a" * MAX_EXPLANATION_CHARS
    assert normalise_explanation(text) == text


def test_normalise_over_cap_truncates_to_cap_with_marker():
    text = "a" * (MAX_EXPLANATION_CHARS + 1)
    out = normalise_explanation(text)
    assert len(out) == MAX_EXPLANATION_CHARS
    assert out.startswith("a" * 100)
    assert MARKER in out


def test_huge_adapter_explanation_is_bounded_and_verifies():
    record = _run(_StubLLM("X" * 500_000))
    diag = _diagnosis(record)
    assert record["verdict"] == "FAIL"
    assert len(diag["explanation"]) == MAX_EXPLANATION_CHARS
    assert MARKER in diag["explanation"]
    assert len(json.dumps(record)) < 30_000
    assert verify_chain(record) is True
    assert document_ok(verify_document(record)) is True


def test_at_cap_adapter_explanation_sealed_verbatim():
    text = "b" * MAX_EXPLANATION_CHARS
    record = _run(_StubLLM(text))
    assert _diagnosis(record)["explanation"] == text
    assert verify_chain(record) is True


def test_mock_explanation_on_samples_unchanged():
    record = _run(MockLLMAdapter())
    explanation = _diagnosis(record)["explanation"]
    assert MARKER not in explanation
    assert len(explanation) < 1_000
    assert verify_chain(record) is True


def test_mock_explanation_bounded_on_maximum_size_spec(tmp_path):
    candidate = tmp_path / "cand"
    candidate.mkdir()
    (candidate / "note.txt").write_text("fictional\n", encoding="utf-8")
    record = _run(MockLLMAdapter(), spec=_big_spec(), candidate=candidate)
    explanation = _diagnosis(record)["explanation"]
    assert len(explanation) == MAX_EXPLANATION_CHARS
    assert MARKER in explanation
    assert verify_chain(record) is True


def test_fallback_explanation_bounded_on_maximum_size_spec(tmp_path):
    candidate = tmp_path / "cand"
    candidate.mkdir()
    (candidate / "note.txt").write_text("fictional\n", encoding="utf-8")
    record = _run(_ExplodingLLM(), spec=_big_spec(), candidate=candidate)
    diag = _diagnosis(record)
    assert diag["diagnosis_error"] is not None
    assert len(diag["explanation"]) == MAX_EXPLANATION_CHARS
    assert MARKER in diag["explanation"]
    assert verify_chain(record) is True


def test_rechained_over_limit_explanation_fails_verification():
    record = _run(MockLLMAdapter())
    _diagnosis(record)["explanation"] = "y" * (MAX_EXPLANATION_CHARS + 1)
    _rechain(record)
    assert verify_chain(record) is False
    assert document_ok(verify_document(record)) is False


def test_rechained_at_cap_explanation_still_verifies():
    record = _run(MockLLMAdapter())
    _diagnosis(record)["explanation"] = "y" * MAX_EXPLANATION_CHARS
    _rechain(record)
    assert verify_chain(record) is True


def test_over_limit_record_cli_and_webview(tmp_path):
    record = _run(MockLLMAdapter())
    _diagnosis(record)["explanation"] = "y" * (MAX_EXPLANATION_CHARS + 1)
    _rechain(record)
    path = tmp_path / "record.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    assert cli.main(["verify", "--record", str(path)]) == 1
    assert "CHAIN INVALID" in render_document(record)


def _all_diagnoses(obj):
    if isinstance(obj, dict):
        if obj.get("kind") == "diagnosis":
            yield obj
        for value in obj.values():
            yield from _all_diagnoses(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _all_diagnoses(item)


def test_all_committed_fixtures_still_verify():
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
        # The cap carries real headroom over every committed
        # diagnosis (the longest is 2,022 characters).
        for diag in _all_diagnoses(data):
            assert len(diag["explanation"]) * 4 < MAX_EXPLANATION_CHARS, path
