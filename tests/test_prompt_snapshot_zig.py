"""Prompt-snapshot Zig coverage (v1.145.0).

The repair loop's patch prompt carries a snapshot of the candidate's
small text files (``repair._candidate_files``). Zig files were
invisible to it: no '.zig' suffix was whitelisted at all, so a
candidate whose fix landed in a ``rates.zig`` rate card matched
nothing and the file to be patched never reached the prompt. On the
first live candidate whose fix landed in a .zig card the real model
named the right file from the brief, invented its contents as a
file creation, and had every diff correctly rejected
(``patch_not_applicable``). These tests pin the inclusion, the
unchanged whitelist posture for unlisted/binary files, and an
end-to-end repair whose stub adapter can only build its diff from
the snapshot it is handed.
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

RATES_ZIG = (
    "// Warehouse Zig rate card -- rates.zig, rev 2 (do not hand-edit).\n"
    "// Shared by the Zig billing service.\n"
    "// Published discount rules: GOLD 20% off, SILVER 10% off,\n"
    "// BRONZE 5% off, NONE no discount.\n"
    "// The table below is the rate table.\n"
    "\n"
    "pub const Rate = struct { code: []const u8, rate: f64 };\n"
    "\n"
    "pub const rate_table = [_]Rate{\n"
    '    .{ .code = "GOLD", .rate = 0.20 }, .{ .code = "STUDENT", .rate = 0.15 },\n'
    '    .{ .code = "SILVER", .rate = 0.03 }, .{ .code = "STAFF", .rate = 0.30 },\n'
    '    .{ .code = "BRONZE", .rate = 0.12 }, .{ .code = "NONE", .rate = 0.0 },\n'
    "};\n"
)

EXTRA_ZIG = "// Extra Zig module.\n\npub const extra: u32 = 1;\n"

PRICING_PY = (
    '"""Checkout pricing: tier discounts from rates.zig."""\n'
    "import re\n"
    "from pathlib import Path\n"
    "\n"
    "_HERE = Path(__file__).parent\n"
    "_PATTERN = re.compile(r'\\.code = \"(GOLD|SILVER|BRONZE|NONE)\", \\.rate = ([0-9]+(?:\\.[0-9]+)?)')\n"
    "\n"
    "\n"
    "def _rates():\n"
    '    text = (_HERE / "rates.zig").read_text(encoding="utf-8")\n'
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


def test_zig_is_a_snapshot_suffix():
    assert ".zig" in _TEXT_SUFFIXES


def test_snapshot_includes_zig_files_verbatim(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "rates.zig").write_text(RATES_ZIG)
    (cand / "extra.zig").write_text(EXTRA_ZIG)
    (cand / "pricing.py").write_text(PRICING_PY)
    files = _candidate_files(cand)
    assert files["rates.zig"] == RATES_ZIG
    assert files["extra.zig"] == EXTRA_ZIG
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


class _SnapshotZigStub(LLMAdapter):
    """Builds its diff from the snapshot, exactly like a real model must.

    If ``rates.zig`` is absent from the candidate_files snapshot
    the stub has nothing truthful to diff against and returns no
    diff -- the pre-v1.145.0 live failure shape.
    """

    name = "snapshot-zig-stub"

    def __init__(self):
        self.seen_snapshots = []

    def generate_patch(self, brief, failing_evidence=None):
        files = (failing_evidence or {}).get("candidate_files") or {}
        self.seen_snapshots.append(sorted(files))
        text = files.get("rates.zig")
        if text is None:
            return "Plan only: the Zig file was not shown to me."
        lines = text.splitlines()
        old = '    .{ .code = "SILVER", .rate = 0.03 }, .{ .code = "STAFF", .rate = 0.30 },'
        new = '    .{ .code = "SILVER", .rate = 0.10 }, .{ .code = "STAFF", .rate = 0.30 },'
        old2 = '    .{ .code = "BRONZE", .rate = 0.12 }, .{ .code = "NONE", .rate = 0.0 },'
        new2 = '    .{ .code = "BRONZE", .rate = 0.05 }, .{ .code = "NONE", .rate = 0.0 },'
        idx = next(i for i, ln in enumerate(lines) if ln == old)
        idx2 = next(i for i, ln in enumerate(lines) if ln == old2)
        out = ["--- a/rates.zig", "+++ b/rates.zig",
               f"@@ -{idx + 1},1 +{idx + 1},1 @@", f"-{old}", f"+{new}",
               f"@@ -{idx2 + 1},1 +{idx2 + 1},1 @@", f"-{old2}", f"+{new2}"]
        return "\n".join(out) + "\n"

    def explain_failure(self, check_results):
        return "stub diagnosis: the Zig rate card has wrong rates"


def _zig_candidate(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "pricing.py").write_text(PRICING_PY)
    (cand / "rates.zig").write_text(RATES_ZIG)
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
def test_zig_repair_end_to_end_from_snapshot(tmp_path):
    cand = _zig_candidate(tmp_path)
    spec = parse_spec({
        "name": "shipping-discount-zig-fix",
        "brief": "Fix rates.zig so every rate matches the published rules.",
        "checks": [
            {"name": "unit_tests", "command": "python3 checkout_test.py",
             "timeout_s": 120},
            {"name": "repro", "command": "python3 repro.py",
             "timeout_s": 120},
        ],
    })
    stub = _SnapshotZigStub()
    report = run_repair(
        spec=spec, candidate_dir=cand, mode="judge",
        llm=stub, references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label=str(cand), max_rounds=3,
    )
    assert report["final_verdict"] == "PASS"
    assert report["stopped_reason"] == "passed"
    assert report["rounds_used"] == 1
    assert "rates.zig" in stub.seen_snapshots[0]
    for a in report["attempts"]:
        assert verify_chain(a["evidence"]) is True
    assert (cand / "rates.zig").read_text() == RATES_ZIG
