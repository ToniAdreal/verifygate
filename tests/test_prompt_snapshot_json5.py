"""Prompt-snapshot JSON5 (.json5) coverage (v1.205.0).

The repair loop's patch prompt carries a snapshot of the candidate's
small text files (``repair._candidate_files``). JSON5 files were
invisible to it: no '.json5' suffix was whitelisted at all
('.json' covers JSON, '.jsonl' covers JSON Lines, and '.jsonnet'
covers Jsonnet, but the snapshot matches the full suffix), so a
candidate whose fix landed in a ``rates.json5`` rate card never
reached the prompt. On the first live candidate whose fix landed
in a .json5 card both models named the right file from the brief,
invented its contents, and had every diff correctly rejected.
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

RATES_JSON5 = '{\n  // Tier discount percents (JSON5 rate card).\n  gold: 20,\n  student: 15,\n  silver: 3,\n  staff: 30,\n  bronze: 12,\n  none: 0,\n}\n'

PRICING_PY = '"""Checkout pricing: tier discounts from rates.json5."""\nimport re\nfrom pathlib import Path\n\n_HERE = Path(__file__).parent\n_FIELD = re.compile(r"(gold|silver|bronze|none)\\s*:\\s*(\\d+)")\n\n\ndef _rates():\n    text = (_HERE / "rates.json5").read_text(encoding="utf-8")\n    return {\n        name.upper(): int(value) / 100\n        for name, value in _FIELD.findall(text)\n    }\n\n\ndef _tier(subtotal):\n    if subtotal >= 200:\n        return "GOLD"\n    if subtotal >= 100:\n        return "SILVER"\n    if subtotal >= 50:\n        return "BRONZE"\n    return "NONE"\n\n\ndef total(subtotal):\n    return round(subtotal * (1 - _rates()[_tier(subtotal)]), 2)\n'

CHECKOUT_TEST_PY = 'from pricing import total\n\nfailures = []\n\n\ndef check(expected, subtotal, label):\n    got = total(subtotal)\n    if got != expected:\n        failures.append(\n            f"FAIL {label}: expected {expected}, got {got}")\n    else:\n        print(f"ok {label}")\n\n\ncheck(200.00, 250, "bulk")\ncheck(108.00, 120, "over100")\ncheck(76.00, 80, "over50")\ncheck(30.00, 30, "small")\n\nfor line in failures:\n    print(line)\nraise SystemExit(1 if failures else 0)\n'

REPRO_PY = 'from pricing import total\n\nif total(120) != 108.00 or total(80) != 76.00:\n    print("discount rates are wrong")\n    raise SystemExit(1)\nprint("repro ok")\n'


def test_json5_is_a_snapshot_suffix():
    assert ".json5" in _TEXT_SUFFIXES


def test_snapshot_includes_json5_files_verbatim(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "rates.json5").write_text(RATES_JSON5)
    (cand / "pricing.py").write_text(PRICING_PY)
    files = _candidate_files(cand)
    assert files["rates.json5"] == RATES_JSON5
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


class _SnapshotJson5Stub(LLMAdapter):
    """Builds its diff from the snapshot, exactly like a real model must.

    If ``rates.json5`` is absent from the candidate_files snapshot
    the stub has nothing truthful to diff against and returns no
    diff -- the pre-v1.205.0 live failure shape.
    """

    name = "snapshot-json5-stub"

    def __init__(self):
        self.seen_snapshots = []

    def generate_patch(self, brief, failing_evidence=None):
        files = (failing_evidence or {}).get("candidate_files") or {}
        self.seen_snapshots.append(sorted(files))
        text = files.get("rates.json5")
        if text is None:
            return "Plan only: the json5 file was not shown to me."
        lines = text.splitlines()
        old = [ln for ln in lines if "silver" in ln][0]
        new = old.replace(": 3,", ": 10,")
        old2 = [ln for ln in lines if "bronze" in ln][0]
        new2 = old2.replace(": 12,", ": 5,")
        idx = lines.index(old)
        idx2 = lines.index(old2)
        out = ["--- a/rates.json5", "+++ b/rates.json5",
               f"@@ -{idx + 1},1 +{idx + 1},1 @@", f"-{old}", f"+{new}",
               f"@@ -{idx2 + 1},1 +{idx2 + 1},1 @@", f"-{old2}", f"+{new2}"]
        return "\n".join(out) + "\n"

    def explain_failure(self, check_results):
        return "stub diagnosis: the json5 rate card has wrong rates"


def _json5_candidate(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "pricing.py").write_text(PRICING_PY)
    (cand / "rates.json5").write_text(RATES_JSON5)
    (cand / "checkout_test.py").write_text(CHECKOUT_TEST_PY)
    (cand / "repro.py").write_text(REPRO_PY)
    return cand


@pytest.mark.skipif(shutil.which("python3") is None,
                    reason="python3 is not available")
def test_json5_repair_end_to_end_from_snapshot(tmp_path):
    cand = _json5_candidate(tmp_path)
    spec = parse_spec({
        "name": "shipping-discount-json5-fix",
        "brief": "Fix rates.json5 so every rate matches the published rules.",
        "checks": [
            {"name": "unit_tests", "command": "python3 checkout_test.py",
             "timeout_s": 120},
            {"name": "repro", "command": "python3 repro.py",
             "timeout_s": 120},
        ],
    })
    stub = _SnapshotJson5Stub()
    report = run_repair(
        spec=spec, candidate_dir=cand, mode="judge",
        llm=stub, references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label=str(cand), max_rounds=3,
    )
    assert report["final_verdict"] == "PASS"
    assert report["stopped_reason"] == "passed"
    assert report["rounds_used"] == 1
    assert "rates.json5" in stub.seen_snapshots[0]
    for a in report["attempts"]:
        assert verify_chain(a["evidence"]) is True
    assert (cand / "rates.json5").read_text() == RATES_JSON5
