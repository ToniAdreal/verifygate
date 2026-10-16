"""Prompt-snapshot F# script coverage (v1.135.0).

The repair loop's patch prompt carries a snapshot of the candidate's
small text files (``repair._candidate_files``). F# script files were
invisible to it: '.fs' is whitelisted but the snapshot matches the
full suffix, so a candidate
whose fix landed in a ``rates.fsx`` rate card matched nothing and the
file to be patched never reached the prompt. On the first live
candidate whose fix landed in a .fsx card the real model named the
right file from the brief, invented its contents as a file creation,
and had every diff correctly rejected (``patch_not_applicable``).
These tests pin the inclusion, the unchanged whitelist posture for
unlisted/binary files, and an end-to-end repair whose stub adapter
can only build its diff from the snapshot it is handed.
"""

import shutil

import pytest

from verifygate.evidence import verify_chain
from verifygate.llm import LLMAdapter
from verifygate.repair import (
    _TEXT_SUFFIXES,
    _candidate_files,
    run_repair,
)
from verifygate.spec import parse_spec
from verifygate.tavily import MockReferenceLookup

RATES_FSX = (
    "// Warehouse F# rate card -- rates.fsx, rev 2 (do not hand-edit).\n"
    "// Published discount rules: GOLD 20% off, SILVER 10% off,\n"
    "// BRONZE 5% off, NONE no discount.\n"
    "// Evaluated by the billing service; the list below is the rate table.\n"
    'let rates = [ ("GOLD", 0.20); ("SILVER", 0.03); ("BRONZE", 0.12); ("NONE", 0.0) ]\n'
)

EXTRA_FSX = "// extra program\nlet extra = 1\n"

PRICING_PY = (
    '"""Checkout pricing: tier discounts from rates.fsx."""\n'
    "import re\n"
    "from pathlib import Path\n"
    "\n"
    "_HERE = Path(__file__).parent\n"
    "_PATTERN = re.compile(r'\\(\"(GOLD|SILVER|BRONZE|NONE)\","
    "\\s*([0-9]+(?:\\.[0-9]+)?)\\)')\n"
    "\n"
    "\n"
    "def _rates():\n"
    '    text = (_HERE / "rates.fsx").read_text(encoding="utf-8")\n'
    "    return {name: float(value)"
    " for name, value in _PATTERN.findall(text)}\n"
    "\n"
    "\n"
    "def _tier(subtotal):\n"
    "    if subtotal >= 200:\n"
    '        return "GOLD"\n'
    "    if subtotal >= 100:\n"
    '        return "SILVER"\n'
    "    if subtotal >= 50:\n"
    '        return "BRONZE"\n'
    '    return "NONE"\n'
    "\n"
    "\n"
    "def total(subtotal):\n"
    "    return round(subtotal * (1 - _rates()[_tier(subtotal)]), 2)\n"
)


def test_fsx_is_a_snapshot_suffix():
    assert ".fsx" in _TEXT_SUFFIXES


def test_snapshot_includes_fsx_files_verbatim(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "rates.fsx").write_text(RATES_FSX)
    (cand / "extra.fsx").write_text(EXTRA_FSX)
    (cand / "pricing.py").write_text(PRICING_PY)
    files = _candidate_files(cand)
    assert files["rates.fsx"] == RATES_FSX
    assert files["extra.fsx"] == EXTRA_FSX
    assert files["pricing.py"] == PRICING_PY


def test_snapshot_still_skips_unlisted_and_binary_files(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "blob.bin").write_text("plain text, unlisted suffix\n")
    (cand / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff\xfe")
    (cand / "notes").write_text("extensionless, unlisted basename\n")
    (cand / "ok.py").write_text("x = 1\n")
    files = _candidate_files(cand)
    assert files == {"ok.py": "x = 1\n"}


class _SnapshotFsxStub(LLMAdapter):
    """Builds its diff from the snapshot, exactly like a real model must.

    If ``rates.fsx`` is absent from the candidate_files snapshot the
    stub has nothing truthful to diff against and returns no diff --
    the pre-v1.135.0 live failure shape.
    """

    name = "snapshot-fsx-stub"

    def __init__(self):
        self.seen_snapshots = []

    def generate_patch(self, brief, failing_evidence=None):
        files = (failing_evidence or {}).get("candidate_files") or {}
        self.seen_snapshots.append(sorted(files))
        text = files.get("rates.fsx")
        if text is None:
            return "Plan only: the F# script file was not shown to me."
        lines = text.splitlines()
        old = 'let rates = [ ("GOLD", 0.20); ("SILVER", 0.03); ("BRONZE", 0.12); ("NONE", 0.0) ]'
        new = 'let rates = [ ("GOLD", 0.20); ("SILVER", 0.10); ("BRONZE", 0.05); ("NONE", 0.0) ]'
        idx = next(i for i, ln in enumerate(lines) if ln == old)
        out = ["--- a/rates.fsx", "+++ b/rates.fsx",
               f"@@ -{idx + 1},1 +{idx + 1},1 @@", f"-{old}", f"+{new}"]
        return "\n".join(out) + "\n"

    def explain_failure(self, check_results):
        return "stub diagnosis: the F# script rate card has wrong rates"


def _fsx_candidate(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "pricing.py").write_text(PRICING_PY)
    (cand / "rates.fsx").write_text(RATES_FSX)
    (cand / "checkout_test.py").write_text(
        "from pricing import total\n"
        "\n"
        "failures = []\n"
        "\n"
        "\n"
        "def check(expected, subtotal, label):\n"
        "    got = total(subtotal)\n"
        "    if got != expected:\n"
        "        failures.append(\n"
        "            f\"FAIL {label}: expected {expected}, got {got}\")\n"
        "    else:\n"
        "        print(f\"ok {label}\")\n"
        "\n"
        "\n"
        "check(200.00, 250, \"bulk\")\n"
        "check(108.00, 120, \"over100\")\n"
        "check(76.00, 80, \"over50\")\n"
        "check(30.00, 30, \"small\")\n"
        "\n"
        "for line in failures:\n"
        "    print(line)\n"
        "raise SystemExit(1 if failures else 0)\n"
    )
    (cand / "repro.py").write_text(
        "from pricing import total\n"
        "\n"
        "if total(120) != 108.00 or total(80) != 76.00:\n"
        "    print(\"discount rates are wrong\")\n"
        "    raise SystemExit(1)\n"
        "print(\"repro ok\")\n"
    )
    return cand


@pytest.mark.skipif(shutil.which("python3") is None,
                    reason="python3 is not available")
def test_fsx_repair_end_to_end_from_snapshot(tmp_path):
    cand = _fsx_candidate(tmp_path)
    spec = parse_spec({
        "name": "shipping-discount-fsx-fix",
        "brief": "Fix rates.fsx so every rate matches the published rules.",
        "checks": [
            {"name": "unit_tests", "command": "python3 checkout_test.py",
             "timeout_s": 120},
            {"name": "repro", "command": "python3 repro.py",
             "timeout_s": 120},
        ],
    })
    stub = _SnapshotFsxStub()
    report = run_repair(
        spec=spec, candidate_dir=cand, mode="judge",
        llm=stub, references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label=str(cand), max_rounds=3,
    )
    assert report["final_verdict"] == "PASS"
    assert report["stopped_reason"] == "passed"
    assert report["rounds_used"] == 1
    assert "rates.fsx" in stub.seen_snapshots[0]
    for a in report["attempts"]:
        assert verify_chain(a["evidence"]) is True
    assert (cand / "rates.fsx").read_text() == RATES_FSX
