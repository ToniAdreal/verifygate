"""In-repo dogfooding assets.

The dogfood spec and its self-check evidence record live at the
repository root, so a fresh clone can re-run VerifyGate on its own
codebase with one command. The committed record must be a PASS whose
hash chain verifies and whose header binds it to the committed spec.

The evidence assertions skip (rather than fail) when the record is
absent: the dogfood run itself executes this suite inside a sandbox
copy of the tree, and on the very first generation the record does
not exist yet inside that copy. Everywhere else — a normal clone,
CI, the rehearsal — the record is committed and fully checked.
"""

import json
from pathlib import Path

import pytest

from verifygate.evidence import verify_chain
from verifygate.spec import load_spec

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "dogfood_spec.json"
RECORD = ROOT / "evidence_self_check.json"


def test_dogfood_spec_is_the_self_check():
    spec = load_spec(SPEC)
    assert spec.name == "verifygate-self-check"
    assert len(spec.checks) == 1
    assert spec.checks[0].name == "unit_tests"
    assert "pytest" in spec.checks[0].command


def test_committed_self_check_record_passes_and_chains():
    if not RECORD.exists():
        pytest.skip("self-check record not generated yet (first dogfood run)")
    record = json.loads(RECORD.read_text(encoding="utf-8"))
    assert record["tool"] == "verifygate"
    assert record["verdict"] == "PASS"
    assert verify_chain(record) is True
    header = record["entries"][0]
    assert header["kind"] == "header"
    assert header["spec_name"] == "verifygate-self-check"
    assert header["spec_hash"] == load_spec(SPEC).hash
    checks = [e for e in record["entries"] if e["kind"] == "check"]
    assert len(checks) == 1
    assert checks[0]["verdict"] == "PASS"
    assert checks[0]["exit_code"] == 0
