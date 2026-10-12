"""Replay of a report whose patch linkage is not an object (v1.62.0).

The reconstruction path in ``replay_document`` read each later
attempt's ``patch_from_previous`` with ``.get`` on the assumption
it is an object — true of every report the repair loop emits, but
a report is an untrusted artifact. A linkage rewritten as a bare
string, a number, a list, or ``true`` crashed ``replay_document``
with an unhandled ``AttributeError`` — past the per-record path,
whose doctrine since v1.47.0 is that a malformed record replays
as *not reproduced*, never as a crash (and at the CLI the
backstop caught it as a usage error, exit 2, instead of the
not-reproduced answer, exit 1). A non-object linkage is now
simply no applied patch recorded: the attempt cannot be
reconstructed, and the result says exactly that.
"""

import copy
import json
from pathlib import Path

import pytest

from verifygate.cli import main
from verifygate.mock_llm import MockLLMAdapter
from verifygate.replay import replay_document, replay_ok
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "samples" / "spec.json"
REAL_REPAIR = ROOT / "samples" / "evidence_real_repair.json"
CAND_FAILING = ROOT / "samples" / "failing"


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _replay(data):
    return replay_document(
        data,
        spec=load_spec(SPEC),
        candidate_dir=CAND_FAILING,
        mode="judge",
        llm=MockLLMAdapter(),
        references=MockReferenceLookup(),
        spec_label=str(SPEC),
    )


def _with_linkage(value):
    doc = _load(REAL_REPAIR)
    doc["attempts"][1]["patch_from_previous"] = value
    return doc


@pytest.mark.parametrize("bad", ["x", 123, 1.5, True, ["diff"], None])
def test_non_object_linkage_is_not_reproduced_not_a_crash(bad):
    results = _replay(_with_linkage(bad))
    # Attempt 0 still replays and reproduces on its own terms.
    assert results[0]["error"] is None
    assert results[0]["verdict_match"] is True
    # Attempt 1 cannot be reconstructed, and says so.
    assert len(results) == 2
    attempt = results[1]
    assert attempt["verdict_match"] is False
    assert attempt["actual_verdict"] is None
    assert "no applied patch recorded" in attempt["error"]
    assert replay_ok(results) is False


def test_empty_object_linkage_is_not_reproduced():
    results = _replay(_with_linkage({}))
    assert len(results) == 2
    assert "no applied patch recorded" in results[1]["error"]
    assert replay_ok(results) is False


def test_non_string_patch_text_is_a_reconstruction_error():
    doc = _with_linkage({"applied": True, "patch_text": 123})
    results = _replay(doc)
    assert len(results) == 2
    assert "recorded patch no longer applies" in results[1]["error"]
    assert replay_ok(results) is False


def test_genuine_report_still_replays_matched():
    results = _replay(_load(REAL_REPAIR))
    assert len(results) == 2
    assert all(r["error"] is None for r in results)
    assert replay_ok(results) is True


def test_cli_replay_non_object_linkage_exits_1_without_traceback(
        tmp_path, capsys):
    out = tmp_path / "report.json"
    out.write_text(json.dumps(_with_linkage("x")), encoding="utf-8")
    code = main(["replay", "--record", str(out), "--spec", str(SPEC),
                 "--candidate", str(CAND_FAILING)])
    assert code == 1
    captured = capsys.readouterr()
    assert "REPLAY ERROR: no applied patch recorded" in captured.out
    assert "Traceback" not in captured.out + captured.err


def test_original_candidate_byte_unchanged_after_bad_linkage_replay():
    before = (CAND_FAILING / "stringtools.py").read_bytes()
    _replay(_with_linkage(["not", "a", "linkage"]))
    assert (CAND_FAILING / "stringtools.py").read_bytes() == before
