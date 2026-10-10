"""Unsealed-summary + replay-spec binding hardening (v1.8.0).

Two trust holes, both reproduced against v1.7.0 before the fix:

* A repair report's ``llm_usage_total`` and ``rounds_used`` sit
  outside every hash chain, and ``verifygate verify`` never recomputed
  them — rewriting a 5,367-token bill as 1 token, or 1 round as 99,
  still verified RECORD INTACT, and the web view printed the
  rewritten figures to the reader.
* ``verifygate replay`` never compared the supplied spec against the
  record's sealed ``spec_hash`` — a different task (different name and
  brief) whose checks happen to be the same commands reported
  REPLAY MATCHED against the original task's record.

The fixes: ``verify`` derives both figures from the sealed attempts
(+ the ``unsealed_llm_usage`` list, whose edits change the derived
total and are therefore caught too) and folds them into
``report_match``; replay binds the spec exactly like it binds the
candidate tree; the web view shows the derived figures and flags a
disagreeing stated summary.
"""

import copy
import json
from pathlib import Path

from verifygate.cli import main
from verifygate.mock_llm import MockLLMAdapter
from verifygate.replay import replay_document, replay_ok
from verifygate.spec import load_spec, parse_spec
from verifygate.tavily import MockReferenceLookup
from verifygate.verify import (
    derived_rounds_used,
    derived_usage_total,
    document_ok,
    verify_document,
)
from verifygate.webview import render_document

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "samples" / "spec.json"
PASSING_REC = ROOT / "samples" / "evidence_passing.json"
REAL_REPAIR = ROOT / "samples" / "evidence_real_repair.json"
REAL_REPAIR_V2 = ROOT / "samples" / "evidence_real_repair_v2.json"
JS_REAL_REPAIR = ROOT / "samples-js" / "evidence_real_repair.json"
CAND_PASSING = ROOT / "samples" / "passing"
CAND_FAILING = ROOT / "samples" / "failing"


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(tmp_path, data, name="report.json"):
    out = tmp_path / name
    out.write_text(json.dumps(data), encoding="utf-8")
    return str(out)


# -- derived figures ----------------------------------------------------

def test_derived_figures_match_all_committed_reports():
    for path in (REAL_REPAIR, REAL_REPAIR_V2, JS_REAL_REPAIR):
        report = _load(path)
        assert derived_usage_total(report) == report["llm_usage_total"], path
        assert derived_rounds_used(report) == report["rounds_used"], path
        assert document_ok(verify_document(report)) is True, path


def test_derived_rounds_count_the_rejected_patch_round():
    # The v2 report: attempts are rounds 0 and 2 because round 1's
    # patch was rejected and preserved as failed_patch.
    report = _load(REAL_REPAIR_V2)
    assert report["failed_patch"]["round"] == 1
    assert derived_rounds_used(report) == 2


# -- verify catches rewritten figures -----------------------------------

def test_usage_total_tamper_fails_verify():
    report = _load(REAL_REPAIR)
    report["llm_usage_total"] = {"llm_calls": 2, "total_tokens": 1}
    results = verify_document(report)
    assert all(r["report_match"] is False for _, r in results)
    assert document_ok(results) is False


def test_rounds_used_tamper_fails_verify():
    report = _load(REAL_REPAIR)
    report["rounds_used"] = 99
    assert document_ok(verify_document(report)) is False


def test_unsealed_usage_tamper_fails_verify():
    # The unsealed list is itself unchained, but inflating it changes
    # the derived total, which no longer matches the stated total.
    report = _load(REAL_REPAIR)
    report["unsealed_llm_usage"] = [{
        "model": "fake", "purpose": "generate_patch",
        "usage": {"total_tokens": 1000},
    }]
    assert document_ok(verify_document(report)) is False


def test_cli_verify_exits_1_on_usage_tamper(tmp_path, capsys):
    report = _load(REAL_REPAIR)
    report["llm_usage_total"]["total_tokens"] = 1
    rc = main(["verify", "--record", _write(tmp_path, report)])
    assert rc == 1
    assert "report summary: MISMATCH" in capsys.readouterr().out


def test_missing_figures_are_legacy_not_tamper():
    # A pre-v1.8 report that omits a figure displays nothing false;
    # it is not failed for the omission alone.
    report = _load(REAL_REPAIR)
    del report["llm_usage_total"]
    del report["rounds_used"]
    assert document_ok(verify_document(report)) is True


# -- web view shows derived figures --------------------------------------

def test_webview_flags_and_replaces_tampered_usage_total():
    report = _load(REAL_REPAIR)
    report["llm_usage_total"] = {"llm_calls": 2, "total_tokens": 1}
    html = render_document(report)
    assert "REPORT SUMMARY MISMATCH" in html
    assert "LLM usage total: 2 call(s), 5367 total token(s)" in html


def test_webview_flags_and_replaces_tampered_rounds():
    report = _load(REAL_REPAIR)
    report["rounds_used"] = 99
    html = render_document(report)
    assert "REPORT SUMMARY MISMATCH" in html
    assert "repair rounds used: 1 /" in html


# -- replay binds the spec ------------------------------------------------

def _other_spec(tmp_path):
    raw = copy.deepcopy(_load(SPEC))
    raw["name"] = "a-totally-different-task"
    raw["brief"] = "An unrelated brief that happens to run the same checks."
    out = tmp_path / "spec_b.json"
    out.write_text(json.dumps(raw), encoding="utf-8")
    return out


def test_replay_with_wrong_spec_does_not_match(tmp_path, capsys):
    rc = main(["replay", "--record", str(PASSING_REC),
               "--spec", str(_other_spec(tmp_path)),
               "--candidate", str(CAND_PASSING), "--mode", "judge"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "spec: MISMATCH" in out
    assert "REPLAY MATCHED" not in out


def test_replay_document_api_reports_spec_match():
    results = replay_document(
        _load(PASSING_REC), spec=load_spec(SPEC),
        candidate_dir=CAND_PASSING, mode="judge",
        llm=MockLLMAdapter(), references=MockReferenceLookup(),
        spec_label="samples/spec.json")
    assert [r["spec_match"] for r in results] == [True]
    assert replay_ok(results)


def test_replay_document_api_reports_spec_mismatch(tmp_path):
    raw = copy.deepcopy(_load(SPEC))
    raw["brief"] = "Changed brief only — the hash is the binding."
    results = replay_document(
        _load(PASSING_REC), spec=parse_spec(raw),
        candidate_dir=CAND_PASSING, mode="judge",
        llm=MockLLMAdapter(), references=MockReferenceLookup(),
        spec_label="other.json")
    assert [r["spec_match"] for r in results] == [False]
    assert replay_ok(results) is False


def test_replay_real_report_still_matches_with_its_spec(capsys):
    rc = main(["replay", "--record", str(REAL_REPAIR),
               "--spec", str(SPEC), "--candidate", str(CAND_FAILING),
               "--mode", "judge"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.count("spec: match") == 2
    assert "REPLAY MATCHED" in out
