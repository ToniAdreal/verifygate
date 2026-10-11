"""Tamper robustness (v1.11.0): header sealing, malformed usage, binary patches.

Three holes reproduced against v1.10.0 before the fix:

  1. The header entry was the only entry not hashed in full —
     ``verify_chain`` recomputed it from a fixed four-field subset,
     so injecting an extra field into the header authenticated
     against nothing and the record still verified INTACT (both
     chain versions; extra fields in any other entry broke the
     chain). Verification now hashes the full header content and
     structure validation requires exactly the emitted field set.
  2. A sealed ``llm_usage`` call whose ``usage`` was rewritten as a
     bare string crashed ``verifygate verify`` and the web view with
     an unhandled ``AttributeError`` — a tampered record must fail,
     never crash its verifier. Malformed usage now totals as
     "malformed" and the document fails cleanly.
  3. A patch targeting a binary (non-UTF-8) file raised a raw
     ``UnicodeDecodeError`` instead of ``PatchError``, escaping the
     repair loop's rejection handling and crashing the run. It is
     now a clean ``PatchError`` rejection, tree unchanged.
"""

import copy
import json
from pathlib import Path

import pytest

from verifygate.cli import main
from verifygate.evidence import build_record, verify_chain
from verifygate.llm import LLMAdapter
from verifygate.patch import PatchError, apply_patch
from verifygate.repair import run_repair
from verifygate.spec import parse_spec
from verifygate.tavily import MockReferenceLookup
from verifygate.verify import (
    derived_usage_total,
    document_ok,
    verify_document,
)
from verifygate.webview import render_document

ROOT = Path(__file__).resolve().parent.parent
PASSING = ROOT / "samples" / "evidence_passing.json"
FAILING = ROOT / "samples" / "evidence_failing.json"
REAL_REPAIR = ROOT / "samples" / "evidence_real_repair.json"
REAL_REPAIR_V2 = ROOT / "samples" / "evidence_real_repair_v2.json"
JS_REAL_REPAIR = ROOT / "samples-js" / "evidence_real_repair.json"

CHECK = {
    "name": "unit_tests",
    "command": "python3 -m pytest -q",
    "exit_code": 0,
    "duration_ms": 12,
    "stdout_tail": "ok",
    "stderr_tail": "",
    "timed_out": False,
    "verdict": "PASS",
}


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(tmp_path, data, name="record.json"):
    out = tmp_path / name
    out.write_text(json.dumps(data), encoding="utf-8")
    return str(out)


def _records(data):
    if "attempts" in data:
        return [a["evidence"] for a in data["attempts"]]
    return [data]


def _all_fixture_records():
    for path in (PASSING, FAILING, REAL_REPAIR, REAL_REPAIR_V2,
                 JS_REAL_REPAIR,
                 ROOT / "samples" / "evidence_real_fail.json",
                 ROOT / "samples-js" / "evidence_passing.json",
                 ROOT / "samples-js" / "evidence_failing.json",
                 ROOT / "evidence_self_check.json"):
        yield from _records(_load(path))


def _fresh_record():
    return build_record(
        spec_name="demo",
        spec_hash="a" * 64,
        candidate_tree_hash="b" * 64,
        mode="judge",
        check_results=[CHECK],
        verdict="PASS",
        diagnosis=None,
        replay_command="python3 -m verifygate run --spec s --candidate c",
    )


# -- header sealing ----------------------------------------------------

def test_fresh_record_header_injection_breaks_chain():
    record = _fresh_record()
    assert verify_chain(record) is True
    record["entries"][0]["injected"] = "attacker controlled"
    assert verify_chain(record) is False


def test_legacy_header_injection_breaks_chain():
    for path in (PASSING, FAILING):
        record = _load(path)
        assert record.get("chain_version") is None
        assert verify_chain(record) is True
        record["entries"][0]["injected"] = "attacker controlled"
        assert verify_chain(record) is False, path


def test_real_report_header_injection_breaks_chain():
    for path in (REAL_REPAIR, REAL_REPAIR_V2, JS_REAL_REPAIR):
        for record in _records(_load(path)):
            assert verify_chain(record) is True, path
            record["entries"][0]["injected"] = "attacker controlled"
            assert verify_chain(record) is False, path


def test_all_committed_fixtures_still_verify():
    # The fix must not invalidate any untampered committed record:
    # every fixture header carries exactly the emitted field set, so
    # the full-content header hash recomputes identically.
    records = list(_all_fixture_records())
    assert len(records) >= 10
    for record in records:
        assert verify_chain(record) is True


def test_header_injection_fails_verify_document_and_cli(tmp_path, capsys):
    report = _load(REAL_REPAIR)
    report["attempts"][0]["evidence"]["entries"][0]["injected"] = "x"
    assert document_ok(verify_document(report)) is False
    assert main(["verify", "--record", _write(tmp_path, report)]) == 1
    assert "CHAIN INVALID" in capsys.readouterr().out


# -- malformed sealed usage: fail, never crash --------------------------

def _tamper_usage(report, usage):
    for entry in report["attempts"][0]["evidence"]["entries"]:
        if entry["kind"] == "llm_usage":
            entry["calls"][0]["usage"] = usage
            return
    raise AssertionError("fixture has no llm_usage entry")


def test_string_usage_fails_verification_without_crashing():
    report = _load(REAL_REPAIR)
    _tamper_usage(report, "lots")
    assert derived_usage_total(report) is None
    results = verify_document(report)  # must not raise
    assert document_ok(results) is False


def test_non_numeric_token_total_is_malformed():
    report = _load(REAL_REPAIR)
    _tamper_usage(report, {"total_tokens": "lots"})
    assert derived_usage_total(report) is None
    assert document_ok(verify_document(report)) is False


def test_malformed_usage_cli_verify_exits_1(tmp_path, capsys):
    report = _load(REAL_REPAIR)
    _tamper_usage(report, "lots")
    assert main(["verify", "--record", _write(tmp_path, report)]) == 1
    out = capsys.readouterr().out
    assert "CHAIN INVALID" in out and "MISMATCH" in out


def test_malformed_usage_webview_renders_chain_invalid():
    report = _load(REAL_REPAIR)
    _tamper_usage(report, "lots")
    html = render_document(report["attempts"][0]["evidence"])
    assert "CHAIN INVALID" in html


def test_non_object_call_in_usage_webview_renders():
    record = _fresh_record()
    record["entries"].append({
        "kind": "llm_usage", "hash": "0" * 64,
        "calls": ["not-a-call", {"purpose": "p", "usage": "lots"}],
        "total_tokens": 0,
    })
    html = render_document(record)  # must not raise
    assert "CHAIN INVALID" in html  # hand-built entry is not sealed


# -- binary patch targets: PatchError, never a crash --------------------

_BINARY_DIFF = (
    "--- a/blob.bin\n"
    "+++ b/blob.bin\n"
    "@@ -1 +1 @@\n"
    "-x\n"
    "+y\n"
)


def test_binary_target_patch_raises_patch_error(tmp_path):
    target = tmp_path / "blob.bin"
    original = b"\xff\xfe\x00\x01binary"
    target.write_bytes(original)
    with pytest.raises(PatchError, match="not a UTF-8 text file"):
        apply_patch(tmp_path, _BINARY_DIFF)
    assert target.read_bytes() == original  # atomicity still holds


class _BinaryStub(LLMAdapter):
    """Always proposes a patch against the candidate's binary file."""

    name = "binary-stub"

    def generate_patch(self, brief, failing_evidence=None):
        return _BINARY_DIFF

    def explain_failure(self, check_results):
        return "stub diagnosis"


def test_repair_loop_survives_binary_patch(tmp_path):
    candidate = tmp_path / "cand"
    candidate.mkdir()
    (candidate / "blob.bin").write_bytes(b"\xff\xfe\x00\x01binary")
    (candidate / "check.py").write_text("raise SystemExit(1)\n")
    spec = parse_spec({
        "name": "binary-target",
        "brief": "Fix it somehow.",
        "checks": [{"name": "c", "command": "python3 check.py",
                    "timeout_s": 10}],
    })
    report = run_repair(
        spec=spec, candidate_dir=candidate, mode="judge",
        llm=_BinaryStub(), references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label=str(candidate),
        max_rounds=2,
    )
    # The binary diff is a clean rejection fed back each round, not
    # a UnicodeDecodeError escaping the loop.
    assert report["final_verdict"] == "FAIL"
    assert report["stopped_reason"] == "patch_not_applicable"
    assert "not a UTF-8 text file" in report["last_patch_error"]
    assert (candidate / "blob.bin").read_bytes() == b"\xff\xfe\x00\x01binary"
