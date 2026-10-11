"""Real-adapter wiring: prompts, usage accounting, evidence sealing.

All tests inject a fake transport — zero network, zero credentials —
while exercising the real TokenFactoryAdapter code path end to end:
the runner must seal recorded token usage into the evidence hash
chain, and the repair loop must hand the candidate's actual file
contents to the model so its diff can apply.
"""

import copy
from pathlib import Path

import pytest

from verifygate.evidence import verify_chain
from verifygate.repair import run_repair
from verifygate.runner import run_verification
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup
from verifygate.tokenfactory import DEFAULT_MODEL, TokenFactoryAdapter

APP = Path(__file__).resolve().parent.parent
SPEC = load_spec(APP / "samples" / "spec.json")

GOOD_DIFF = """\
--- a/stringtools.py
+++ b/stringtools.py
@@ -4,9 +4,14 @@
 removes punctuation, so the spec's checks must catch it.
 \"\"\"

+import re
+

 def slugify(text: str) -> str:
-    return text.strip().replace(" ", "-")
+    \"\"\"Lowercase, drop punctuation, join words with single hyphens.\"\"\"
+    lowered = text.strip().lower()
+    hyphenated = re.sub(r"[^a-z0-9]+", "-", lowered)
+    return hyphenated.strip("-")

"""


def _response(content: str, total: int = 100) -> dict:
    return {
        "model": DEFAULT_MODEL,
        "choices": [{
            "message": {"content": content},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": total - 40,
            "completion_tokens": 40,
            "total_tokens": total,
        },
    }


class FakeTransport:
    """Records request bodies; answers patch/explain prompts in kind."""

    def __init__(self, patch_text: str = GOOD_DIFF):
        self.bodies: list[dict] = []
        self.patch_text = patch_text

    def __call__(self, body: dict) -> dict:
        self.bodies.append(body)
        user = body["messages"][-1]["content"]
        if "unified diff" in user:
            return _response(self.patch_text, total=321)
        return _response("Diagnosis: slugify does not lowercase.", total=123)


def _adapter(transport=None) -> TokenFactoryAdapter:
    return TokenFactoryAdapter(transport=transport or FakeTransport())


def test_default_model_is_the_pinned_live_nemotron():
    assert DEFAULT_MODEL == "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
    assert _adapter().model == DEFAULT_MODEL


def test_generate_patch_sends_files_and_records_usage():
    transport = FakeTransport()
    adapter = _adapter(transport)
    out = adapter.generate_patch(
        SPEC.brief,
        {
            "verdict": "FAIL",
            "checks": [{"name": "unit_tests", "verdict": "FAIL"}],
            "candidate_files": {"stringtools.py": "def slugify(t): ...\n"},
        },
    )
    assert out == GOOD_DIFF
    body = transport.bodies[0]
    assert body["model"] == DEFAULT_MODEL
    assert body["max_tokens"] >= 2048  # reasoning model needs headroom
    user = body["messages"][-1]["content"]
    assert "def slugify(t): ..." in user          # exact source was sent
    assert "unified diff" in user                  # diff contract stated
    assert "candidate_files" not in user           # wrapper key not leaked
    calls = adapter.pending_calls()
    assert len(calls) == 1
    assert calls[0]["purpose"] == "generate_patch"
    assert calls[0]["usage"]["total_tokens"] == 321
    assert adapter.drain_calls() == calls
    assert adapter.pending_calls() == []


def test_explain_failure_records_usage_and_uses_reasoning_fallback():
    def transport(body):
        return {
            "model": DEFAULT_MODEL,
            "choices": [{
                "message": {"content": "", "reasoning_content": "why it broke"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                      "total_tokens": 15},
        }

    adapter = _adapter(transport)
    text = adapter.explain_failure(
        [{"name": "c", "verdict": "FAIL", "command": "pytest"}]
    )
    assert text == "why it broke"
    assert adapter.pending_calls()[0]["usage"]["total_tokens"] == 15


def test_empty_completion_is_an_error_not_a_silent_blank():
    adapter = _adapter(lambda body: _response("   "))
    with pytest.raises(RuntimeError, match="empty completion"):
        adapter.explain_failure([{"name": "c", "verdict": "FAIL"}])


def test_usage_is_sealed_into_the_evidence_chain():
    adapter = _adapter()
    record = run_verification(
        spec=SPEC, candidate_dir=APP / "samples" / "failing", mode="real",
        llm=adapter, references=MockReferenceLookup(),
        spec_label="samples/spec.json", candidate_label="samples/failing",
    )
    assert record["verdict"] == "FAIL"  # verdict still from real checks
    usage_entries = [e for e in record["entries"] if e["kind"] == "llm_usage"]
    assert len(usage_entries) == 1
    assert usage_entries[0]["total_tokens"] == 123
    assert usage_entries[0]["calls"][0]["purpose"] == "explain_failure"
    assert verify_chain(record) is True
    tampered = copy.deepcopy(record)
    tampered["entries"][-1]["total_tokens"] = 1  # forge a cheaper bill
    assert verify_chain(tampered) is False


def test_real_style_repair_loop_fail_to_pass_with_usage_chain():
    transport = FakeTransport()
    adapter = _adapter(transport)
    report = run_repair(
        spec=SPEC, candidate_dir=APP / "samples" / "failing", mode="real",
        llm=adapter, references=MockReferenceLookup(),
        spec_label="samples/spec.json", candidate_label="samples/failing",
        max_rounds=3,
    )
    assert [a["verdict"] for a in report["attempts"]] == ["FAIL", "PASS"]
    assert report["final_verdict"] == "PASS"
    # The model actually saw the broken source it had to patch.
    patch_body = transport.bodies[-1]
    assert 'return text.strip().replace(" ", "-")' in (
        patch_body["messages"][-1]["content"])
    # Usage: explain (123) sealed in attempt 0, patch (321) in attempt 1.
    per_attempt = [
        [e for e in a["evidence"]["entries"] if e["kind"] == "llm_usage"]
        for a in report["attempts"]
    ]
    assert per_attempt[0][0]["total_tokens"] == 123
    assert per_attempt[1][0]["total_tokens"] == 321
    assert per_attempt[1][0]["calls"][0]["purpose"] == "generate_patch"
    for a in report["attempts"]:
        assert verify_chain(a["evidence"]) is True
    assert report["llm_usage_total"] == {"llm_calls": 2, "total_tokens": 444}
    assert report["unsealed_llm_usage"] == []
    patch = report["attempts"][1]["patch_from_previous"]
    assert patch["llm_usage"][0]["usage"]["total_tokens"] == 321


def test_rejected_diff_is_fed_back_and_retried():
    """A diff that does not apply is not the end: the applier's exact
    rejection goes back to the model and the loop tries again."""
    bad = GOOD_DIFF.replace('return text.strip().replace(" ", "-")',
                            'return text.strip().replace(" ", "_")')
    transport = FakeTransport()
    state = {"n": 0}

    def flaky(body):
        state["n"] += 1
        user = body["messages"][-1]["content"]
        if "unified diff" in user:
            return _response(bad if state["n"] <= 2 else GOOD_DIFF, total=50)
        # total must stay >= 40: _response derives prompt_tokens as
        # total - 40, and a negative count is a malformed shape the
        # adapter now (v1.18.0) rejects.
        return _response("diagnosis", total=50)

    adapter = TokenFactoryAdapter(transport=flaky)
    report = run_repair(
        spec=SPEC, candidate_dir=APP / "samples" / "failing", mode="real",
        llm=adapter, references=MockReferenceLookup(),
        spec_label="samples/spec.json", candidate_label="samples/failing",
        max_rounds=3,
    )
    assert report["final_verdict"] == "PASS"
    assert report["rounds_used"] == 2
    assert report["failed_patch"]["error"] is not None
    assert "does not match" in report["failed_patch"]["error"]
