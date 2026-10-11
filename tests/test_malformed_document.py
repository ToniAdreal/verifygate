"""Malformed-document robustness (v1.47.0).

The judge-facing consumers of a record — ``verify``, ``replay``,
and the web view — assumed the document's ``entries`` were a list
of objects, an assumption ``verify_chain`` itself never made (its
structure check guards every entry's type). All shapes below were
reproduced against v1.46.0 before the fix:

  * a record whose ``entries`` was rewritten as a list of strings,
    a bare string, an object, or a number crashed ``verify`` and
    ``replay`` with an unhandled ``AttributeError`` / ``KeyError`` /
    ``TypeError`` traceback out of ``header_of`` — the shared helper
    every consumer reads the sealed header through — instead of
    reporting the tampered record as failed (chain invalid);
  * an attempt record carrying a non-list ``entries`` crashed
    ``derived_usage_total`` (called by both the verify cross-check
    and the web view) and the narrative check's sealed-adapter scan;
  * the web view crashed the same way on a malformed ``entries``,
    a non-object repair attempt, a non-list ``attempts``, a
    non-object stated usage total, a non-list ``changed_files``, and
    a non-list diagnosis ``references`` — only the CLI's backstop
    catch turned those into an exit 2, and the page was lost.

A malformed record must *fail* its consumers cleanly, never crash
them: verify reports CHAIN INVALID (exit 1), replay reports not
reproduced (exit 1), and the web view renders the record next to
its CHAIN INVALID banner. A document that is not an object at all
is a clean ``ValueError`` (the CLI's record error, exit 2).
"""

import copy
import json
from pathlib import Path

import pytest

from verifygate.cli import main
from verifygate.mock_llm import MockLLMAdapter
from verifygate.replay import _check_summary, replay_document, replay_ok
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup
from verifygate.verify import (
    check_record,
    derived_usage_total,
    document_ok,
    header_of,
    verify_document,
)
from verifygate.webview import render_document, render_record

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "samples" / "spec.json"
PASSING = ROOT / "samples" / "evidence_passing.json"
FAILING = ROOT / "samples" / "evidence_failing.json"
REAL_REPAIR = ROOT / "samples" / "evidence_real_repair.json"
CAND_PASSING = ROOT / "samples" / "passing"

MALFORMED_ENTRIES = [
    ["x", {"kind": "check"}],  # non-object entries
    [123],
    "abc",                     # a bare string
    {"a": 1},                  # an object, not a list
    123,                       # a number
    None,
]


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _with_entries(entries, source=PASSING):
    data = _load(source)
    data["entries"] = entries
    return data


def _write(tmp_path, data, name="record.json"):
    out = tmp_path / name
    out.write_text(json.dumps(data), encoding="utf-8")
    return str(out)


# -- header_of: the shared header read --------------------------------

@pytest.mark.parametrize("entries", MALFORMED_ENTRIES)
def test_header_of_malformed_entries_is_empty(entries):
    assert header_of(_with_entries(entries)) == {}


@pytest.mark.parametrize("record", [[], "x", 123, None, {}])
def test_header_of_non_record_is_empty(record):
    assert header_of(record) == {}


def test_header_of_genuine_record_unchanged():
    header = header_of(_load(PASSING))
    assert header.get("kind") == "header"
    assert header.get("spec_name") == "stringtools-slugify-fix"


# -- verify ------------------------------------------------------------

@pytest.mark.parametrize("entries", MALFORMED_ENTRIES)
def test_verify_document_malformed_entries_fails_cleanly(entries):
    results = verify_document(_with_entries(entries))
    assert results[0][1]["chain_ok"] is False
    assert document_ok(results) is False


def test_check_record_non_object_is_value_error():
    with pytest.raises(ValueError):
        check_record(["not", "a", "record"])


def test_verify_document_attempt_entries_number_fails_report():
    report = _load(REAL_REPAIR)
    report["attempts"][0]["evidence"]["entries"] = 123
    results = verify_document(report)
    assert document_ok(results) is False
    assert results[0][1]["report_match"] is False


def test_derived_usage_total_non_list_entries_is_none():
    report = _load(REAL_REPAIR)
    report["attempts"][0]["evidence"]["entries"] = 123
    assert derived_usage_total(report) is None
    # A string entries field is malformed too (its characters are
    # not entries) — it must not silently total as zero usage.
    report["attempts"][0]["evidence"]["entries"] = "abc"
    assert derived_usage_total(report) is None


@pytest.mark.parametrize("entries", MALFORMED_ENTRIES[:4])
def test_cli_verify_malformed_entries_exit_1_no_traceback(
        tmp_path, capsys, entries):
    path = _write(tmp_path, _with_entries(entries))
    assert main(["verify", "--record", path]) == 1
    captured = capsys.readouterr()
    assert "CHAIN INVALID" in captured.out
    assert "Traceback" not in captured.err


# -- web view ------------------------------------------------------------

@pytest.mark.parametrize("entries", MALFORMED_ENTRIES)
def test_render_malformed_entries_shows_chain_invalid(entries):
    html = render_document(_with_entries(entries))
    assert "CHAIN INVALID" in html


def test_render_record_non_object_is_value_error():
    with pytest.raises(ValueError):
        render_record(["not", "a", "record"])
    with pytest.raises(ValueError):
        render_document("not a record")


def test_render_repair_report_malformed_shapes():
    report = _load(REAL_REPAIR)
    report["attempts"] = ["x", 123]
    assert "Repair report" in render_document(
        {**report, "kind": "repair-report"})
    report = _load(REAL_REPAIR)
    report["attempts"] = "abc"
    assert "Repair report" in render_document(
        {**report, "kind": "repair-report"})
    report = _load(REAL_REPAIR)
    report["llm_usage_total"] = "huge"
    html = render_document({**report, "kind": "repair-report"})
    assert "SUMMARY MISMATCH" in html
    report = _load(REAL_REPAIR)
    report["attempts"][1]["patch_from_previous"]["changed_files"] = 123
    assert "Repair report" in render_document(
        {**report, "kind": "repair-report"})
    report = _load(REAL_REPAIR)
    report["attempts"][0]["evidence"]["entries"] = 123
    assert "CHAIN INVALID" in render_document(
        {**report, "kind": "repair-report"})


def test_render_malformed_references_do_not_crash():
    data = _load(FAILING)
    for entry in data["entries"]:
        if isinstance(entry, dict) and entry.get("kind") == "diagnosis":
            entry["references"] = 123
    assert "Diagnosis" in render_document(data)
    for entry in data["entries"]:
        if isinstance(entry, dict) and entry.get("kind") == "diagnosis":
            entry["references"] = ["x", {"title": "t", "url": "https://x",
                                         "snippet": "s", "source": "mock"}]
    assert "Diagnosis" in render_document(data)


def test_cli_view_malformed_entries_renders_chain_invalid(tmp_path):
    path = _write(tmp_path, _with_entries(["x"]))
    out = tmp_path / "view.html"
    assert main(["view", "--record", path, "--out", str(out)]) == 0
    assert "CHAIN INVALID" in out.read_text(encoding="utf-8")


# -- replay --------------------------------------------------------------

@pytest.mark.parametrize("entries", MALFORMED_ENTRIES)
def test_check_summary_malformed_entries(entries):
    summary = _check_summary(_with_entries(entries))
    if entries == MALFORMED_ENTRIES[0]:
        # The one object entry is a (field-less) check: it is
        # summarised, the string beside it is skipped, nothing crashes.
        assert summary == [(None, None, None)]
    else:
        assert summary == []


def test_replay_document_malformed_record_not_reproduced():
    spec = load_spec(SPEC)
    results = replay_document(
        _with_entries(["x"]), spec=spec, candidate_dir=CAND_PASSING,
        mode="judge", llm=MockLLMAdapter(),
        references=MockReferenceLookup(), spec_label=str(SPEC))
    assert len(results) == 1
    assert results[0]["chain_ok"] is False
    assert replay_ok(results) is False


def test_cli_replay_malformed_entries_exit_1_no_traceback(
        tmp_path, capsys):
    path = _write(tmp_path, _with_entries({"a": 1}))
    rc = main(["replay", "--record", path, "--spec", str(SPEC),
               "--candidate", str(CAND_PASSING)])
    assert rc == 1
    captured = capsys.readouterr()
    assert "CHAIN INVALID" in captured.out
    assert "Traceback" not in captured.err


# -- genuine documents are unchanged -------------------------------------

def test_genuine_fixtures_still_verify_and_render():
    for path in (PASSING, FAILING, REAL_REPAIR):
        data = _load(path)
        assert document_ok(verify_document(data)) is True
        assert "CHAIN INVALID" not in render_document(copy.deepcopy(data))
