"""True replay: `verifygate replay` re-runs checks and compares verdicts.

`verify` proves a record is intact; replay proves it is *reproducible*.
Repair attempts are reconstructed from the original candidate plus the
patches recorded in the report — never from the ephemeral temp path
the old `replay` strings pointed at — and a candidate edited after the
fact, or a tampered record, must not reproduce.
"""

import json
import shutil
from pathlib import Path

import pytest

from verifygate.cli import main
from verifygate.mock_llm import MockLLMAdapter
from verifygate.repair import run_repair
from verifygate.replay import replay_document, replay_ok
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "samples" / "spec.json"
PASSING_REC = ROOT / "samples" / "evidence_passing.json"
FAILING_REC = ROOT / "samples" / "evidence_failing.json"
REAL_REPAIR = ROOT / "samples" / "evidence_real_repair.json"
JS_SPEC = ROOT / "samples-js" / "spec.json"
JS_REAL_REPAIR = ROOT / "samples-js" / "evidence_real_repair.json"
CAND_PASSING = ROOT / "samples" / "passing"
CAND_FAILING = ROOT / "samples" / "failing"
JS_CAND_FAILING = ROOT / "samples-js" / "failing"

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available")


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(tmp_path, data, name="record.json"):
    out = tmp_path / name
    out.write_text(json.dumps(data), encoding="utf-8")
    return str(out)


def _run_replay(record, spec, candidate, *extra):
    return main(["replay", "--record", str(record), "--spec", str(spec),
                 "--candidate", str(candidate), *extra])


def test_single_passing_record_replays(capsys):
    assert _run_replay(PASSING_REC, SPEC, CAND_PASSING) == 0
    out = capsys.readouterr().out
    assert "expected PASS, replayed PASS" in out
    assert "candidate tree: match" in out
    assert "REPLAY MATCHED: 1 record(s) reproduced" in out


def test_single_failing_record_replays():
    # A FAIL record is a deliverable: replaying it reproduces the FAIL.
    assert _run_replay(FAILING_REC, SPEC, CAND_FAILING) == 0


def test_real_repair_report_replays_in_judge_mode(capsys):
    # The committed Nemotron Super report: attempt 1 is reconstructed
    # from the recorded patch, and its tree hash matches the sealed
    # header — zero keys, zero model calls.
    rc = _run_replay(REAL_REPAIR, SPEC, CAND_FAILING, "--mode", "judge")
    assert rc == 0
    out = capsys.readouterr().out
    assert "attempt 0: expected FAIL, replayed FAIL" in out
    assert "attempt 1: expected PASS, replayed PASS" in out
    assert out.count("candidate tree: match") == 2


@needs_node
def test_js_real_repair_report_replays_in_judge_mode():
    rc = _run_replay(JS_REAL_REPAIR, JS_SPEC, JS_CAND_FAILING,
                     "--mode", "judge")
    assert rc == 0


def test_wrong_candidate_does_not_reproduce(capsys):
    assert _run_replay(PASSING_REC, SPEC, CAND_FAILING) == 1
    assert "candidate tree: MISMATCH" in capsys.readouterr().out


def test_edited_candidate_does_not_reproduce(tmp_path):
    cand = tmp_path / "cand"
    shutil.copytree(CAND_PASSING, cand)
    (cand / "stringtools.py").write_text(
        (CAND_PASSING / "stringtools.py").read_text() + "\n# edited\n")
    assert _run_replay(PASSING_REC, SPEC, cand) == 1


def test_tampered_record_does_not_reproduce(tmp_path):
    data = _load(PASSING_REC)
    data["verdict"] = "FAIL"
    assert _run_replay(_write(tmp_path, data), SPEC, CAND_PASSING) == 1


def test_usage_errors_exit_2(tmp_path):
    assert _run_replay(tmp_path / "missing.json", SPEC, CAND_FAILING) == 2
    assert _run_replay(PASSING_REC, SPEC, tmp_path / "no-dir") == 2
    bad = _write(tmp_path, {"tool": "something-else"}, name="bad.json")
    assert _run_replay(bad, SPEC, CAND_FAILING) == 2


def test_fresh_repair_report_replay_strings_are_truthful():
    # Regression for the dead-temp-path bug: a freshly produced report
    # must not name a /tmp workspace in any attempt's replay field, and
    # the patched attempt must name the replay command.
    report = run_repair(
        spec=load_spec(SPEC), candidate_dir=CAND_FAILING, mode="judge",
        llm=MockLLMAdapter(), references=MockReferenceLookup(),
        spec_label="samples/spec.json",
        candidate_label="samples/failing", max_rounds=3)
    assert report["final_verdict"] == "PASS"
    replays = [a["evidence"]["replay"] for a in report["attempts"]]
    assert "verifygate run" in replays[0]
    assert "verifygate replay" in replays[1]
    assert all("/tmp/" not in r and "verifygate-" not in r for r in replays)


def test_replay_document_api_and_original_untouched():
    before = (CAND_FAILING / "stringtools.py").read_bytes()
    results = replay_document(
        _load(REAL_REPAIR), spec=load_spec(SPEC),
        candidate_dir=CAND_FAILING, mode="judge",
        llm=MockLLMAdapter(), references=MockReferenceLookup(),
        spec_label="samples/spec.json")
    assert replay_ok(results)
    assert [r["label"] for r in results] == ["attempt 0", "attempt 1"]
    assert (CAND_FAILING / "stringtools.py").read_bytes() == before


def test_missing_patch_cannot_reconstruct(tmp_path):
    data = _load(REAL_REPAIR)
    data["attempts"][1]["patch_from_previous"] = None
    rec = _write(tmp_path, data)
    assert _run_replay(rec, SPEC, CAND_FAILING, "--mode", "judge") == 1
