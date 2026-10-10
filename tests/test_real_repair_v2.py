"""Chain-v2 real-mode evidence: the flagship loop under the new chain.

Every real-mode record committed before v1.5.0 is a legacy chain-v1
record — the hardened chain (v2, entry kinds sealed) had only ever
been exercised by Judge Mode and fake transports. This file pins the
first real-mode repair report sealed under chain v2:
``samples/evidence_real_repair_v2.json``, produced by Nemotron 3
Super 120B through the live Token Factory adapter.

The run it pins also produced this release's adapter fix: Super's
first attempt in an earlier run that day spent the entire 8,192-token
budget reasoning *in the reply content* (finish_reason=length) and
never emitted a usable diff. The patch prompt now orders silent
reasoning; the second test pins that instruction.
"""

import json
from pathlib import Path

from verifygate.evidence import verify_chain
from verifygate.spec import load_spec
from verifygate.tokenfactory import TokenFactoryAdapter

APP = Path(__file__).resolve().parent.parent
SPEC = load_spec(APP / "samples" / "spec.json")
REAL_V2_REPAIR = APP / "samples" / "evidence_real_repair_v2.json"
SUPER = "nvidia/nemotron-3-super-120b-a12b"


def test_v2_real_repair_record_is_committed_and_sealed():
    report = json.loads(REAL_V2_REPAIR.read_text(encoding="utf-8"))
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
        assert record["chain_version"] == 2
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


def test_patch_prompt_orders_silent_reasoning():
    """Reasoning models can burn the whole token budget deliberating
    in the reply content; the patch prompt must forbid it explicitly."""
    bodies = []

    def transport(body):
        bodies.append(body)
        return {
            "model": SUPER,
            "choices": [{
                "message": {"content": "--- a/x\n+++ b/x\n"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                      "total_tokens": 2},
        }

    adapter = TokenFactoryAdapter(model=SUPER, transport=transport)
    adapter.generate_patch("brief", {"candidate_files": {"x": "y"}})
    user = bodies[0]["messages"][-1]["content"]
    assert "Reason silently" in user
    assert "finish_reason=length" in user
