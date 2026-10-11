"""Generation resilience: an LLM patch-generation failure can
never destroy a repair report the deterministic checks already
earned.

Before v1.20.0 the repair loop called ``llm.generate_patch``
unguarded — the one unguarded advisory call left after v1.19.0
guarded the diagnosis. A real-adapter failure (network error,
malformed Token Factory response, empty completion after the
retry — all RuntimeError) propagated out of the loop: attempt 0's
already-decided FAIL evidence was never returned, the CLI reported
an adapter error (exit 2) and wrote no report at all. A non-string
return crashed the same way, one line later, at the patch-text
truncation. Generation failure now ends the loop: the failed round
counts as used, the failure is preserved as the report's
``failed_patch`` with ``error_kind: generation_failed``,
``stopped_reason`` is ``generation_failed``, verification derives
exactly that stop from the preserved failure, and the report is
delivered with final verdict FAIL (CLI exit 1). Unlike a rejected
diff, a generation failure is not retried — there is no proposal
to give feedback on.
"""

import copy
import json
from pathlib import Path

from verifygate import cli
from verifygate.evidence import verify_chain
from verifygate.llm import LLMAdapter
from verifygate.mock_llm import MockLLMAdapter
from verifygate.repair import run_repair
from verifygate.replay import replay_document, replay_ok
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup
from verifygate.verify import (derived_stopped_reason, document_ok,
                               verify_document)
from verifygate.webview import render_document

APP = Path(__file__).resolve().parent.parent
SPEC = load_spec(APP / "samples" / "spec.json")
FAILING_DIR = APP / "samples" / "failing"
PASSING_DIR = APP / "samples" / "passing"

GEN_USAGE = {
    "adapter": "exploding-gen",
    "model": "nvidia/test-model",
    "purpose": "generate_patch",
    "finish_reason": "length",
    "usage": {"prompt_tokens": 12, "completion_tokens": 30,
              "total_tokens": 42},
}


class ExplodingGen(LLMAdapter):
    """Generation raises (after recording one call, mirroring the
    real adapter's completed-but-empty attempt); explanations work."""

    name = "exploding-gen"

    def __init__(self):
        self.calls = 0

    def generate_patch(self, brief, failing_evidence=None):
        self.calls += 1
        self.record_call(copy.deepcopy(GEN_USAGE))
        raise RuntimeError(
            "Token Factory returned an empty completion for "
            "generate_patch (finish_reason='length') after one retry")

    def explain_failure(self, check_results):
        return "stub diagnosis"


class NonStringGen(LLMAdapter):
    name = "nonstring-gen"

    def generate_patch(self, brief, failing_evidence=None):
        return None

    def explain_failure(self, check_results):
        return "stub diagnosis"


def _repair(llm, candidate=FAILING_DIR, max_rounds=3):
    return run_repair(
        spec=SPEC, candidate_dir=candidate, mode="judge", llm=llm,
        references=MockReferenceLookup(),
        spec_label="samples/spec.json",
        candidate_label="samples/failing", max_rounds=max_rounds)


# -- the loop survives a generation failure -------------------------------


def test_exploding_generation_preserves_the_report():
    report = _repair(ExplodingGen())
    assert report["final_verdict"] == "FAIL"
    assert report["stopped_reason"] == "generation_failed"
    assert report["rounds_used"] == 1
    assert len(report["attempts"]) == 1
    assert report["attempts"][0]["verdict"] == "FAIL"
    assert verify_chain(report["attempts"][0]["evidence"])
    failed = report["failed_patch"]
    assert failed["applied"] is False
    assert failed["error_kind"] == "generation_failed"
    assert failed["round"] == 1
    assert failed["patch_text"] == ""
    assert "empty completion" in failed["error"]
    assert "RuntimeError" in failed["error"]
    assert report["last_patch_error"] == failed["error"]
    json.dumps(report)  # report stays JSON-serializable end to end


def test_generation_failure_is_not_retried():
    stub = ExplodingGen()
    _repair(stub, max_rounds=3)
    assert stub.calls == 1


def test_nonstring_generation_is_a_generation_failure():
    report = _repair(NonStringGen())
    assert report["final_verdict"] == "FAIL"
    assert report["stopped_reason"] == "generation_failed"
    assert "TypeError" in report["failed_patch"]["error"]
    assert "not str" in report["failed_patch"]["error"]


def test_usage_recorded_before_the_failure_is_surfaced():
    report = _repair(ExplodingGen())
    assert report["unsealed_llm_usage"] == [GEN_USAGE]
    assert report["llm_usage_total"] == {"llm_calls": 1,
                                         "total_tokens": 42}


def test_original_candidate_unchanged_on_generation_failure():
    before = (FAILING_DIR / "stringtools.py").read_bytes()
    _repair(ExplodingGen())
    assert (FAILING_DIR / "stringtools.py").read_bytes() == before


def test_passing_candidate_never_calls_generation():
    stub = ExplodingGen()
    report = _repair(stub, candidate=PASSING_DIR)
    assert report["final_verdict"] == "PASS"
    assert report["stopped_reason"] == "passed"
    assert report["rounds_used"] == 0
    assert stub.calls == 0


def test_zero_max_rounds_never_calls_generation():
    stub = ExplodingGen()
    report = _repair(stub, max_rounds=0)
    assert report["final_verdict"] == "FAIL"
    assert report["stopped_reason"] == "max_rounds"
    assert stub.calls == 0


# -- verification derives the new stop -------------------------------------


def test_generation_failed_report_verifies():
    report = _repair(ExplodingGen())
    assert derived_stopped_reason(report) == "generation_failed"
    assert document_ok(verify_document(report))


def test_flipped_stop_reason_fails_verification():
    report = _repair(ExplodingGen())
    report["stopped_reason"] = "max_rounds"
    assert not document_ok(verify_document(report))


def test_flipped_error_kind_fails_verification():
    report = _repair(ExplodingGen())
    report["failed_patch"]["error_kind"] = "patch_rejected"
    assert derived_stopped_reason(report) == "patch_not_applicable"
    assert not document_ok(verify_document(report))


def test_unknown_error_kind_fails_verification():
    report = _repair(ExplodingGen())
    report["failed_patch"]["error_kind"] = "exploded"
    assert not document_ok(verify_document(report))


def test_mock_rejection_still_derives_patch_not_applicable():
    # The mock's no-diff proposal for an unknown brief is an applier
    # rejection, not a generation failure (regression pin).
    from verifygate.spec import parse_spec
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        cand = Path(td) / "cand"
        cand.mkdir()
        (cand / "check.py").write_text("raise SystemExit(1)\n")
        spec = parse_spec({
            "name": "always-fails",
            "brief": "Make the check pass somehow.",
            "checks": [{"name": "c", "command": "python3 check.py",
                        "timeout_s": 10}],
        })
        report = run_repair(
            spec=spec, candidate_dir=cand, mode="judge",
            llm=MockLLMAdapter(), references=MockReferenceLookup(),
            spec_label="spec.json", candidate_label=str(cand),
            max_rounds=3)
    assert report["stopped_reason"] == "patch_not_applicable"
    assert report["failed_patch"]["error_kind"] == "patch_rejected"
    assert document_ok(verify_document(report))


# -- CLI + replay + rendering ------------------------------------------------


def test_cli_repair_writes_the_report_and_exits_1(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli, "_adapters",
        lambda mode, model=None: (ExplodingGen(), MockReferenceLookup()))
    out = tmp_path / "repair.json"
    rc = cli.main(["repair", "--spec", str(APP / "samples" / "spec.json"),
                   "--candidate", str(FAILING_DIR), "--mode", "real",
                   "--max-rounds", "3", "--out", str(out)])
    assert rc == 1  # FAIL verdict, not the exit-2 adapter error
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["final_verdict"] == "FAIL"
    assert report["stopped_reason"] == "generation_failed"
    assert document_ok(verify_document(report))


def test_replay_of_generation_failed_report_matches():
    report = _repair(ExplodingGen())
    results = replay_document(
        report, spec=SPEC, candidate_dir=FAILING_DIR, mode="judge",
        llm=MockLLMAdapter(), references=MockReferenceLookup(),
        spec_label="samples/spec.json")
    assert replay_ok(results)


def test_webview_shows_generation_failed_without_mismatch():
    html = render_document(_repair(ExplodingGen()))
    assert "generation_failed" in html
    assert "Hash chain verified" in html
    assert "REPORT SUMMARY MISMATCH" not in html
    assert "CHAIN INVALID" not in html
