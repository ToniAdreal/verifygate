"""Prompt-snapshot CMake coverage (v1.195.0).

The repair loop's patch prompt carries a snapshot of the candidate's
small text files (``repair._candidate_files``). CMake module files
were invisible to it: no '.cmake' suffix was whitelisted at all
('CMakeLists.txt' is covered as a basename, but module files are
matched by suffix), so a candidate whose fix landed in a
``rates.cmake`` rate card never reached the prompt. On the first live
candidate whose fix landed in a .cmake card both models named the
right file from the brief, invented its contents, and had every diff
correctly rejected. These tests pin the inclusion, the unchanged
whitelist posture for unlisted/binary files, and an end-to-end repair
whose stub adapter can only build its diff from the snapshot it is
handed.
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

RATES_CMAKE = '# Rate card: tier discount percents (CMake)\nset(GOLD 20) # flagship tier, unchanged since launch\nset(STUDENT 15) # campus programme, not a checkout tier\nset(SILVER 3) # spring promo rate, archived in May\nset(STAFF 30) # internal only, not a checkout tier\nset(BRONZE 12) # legacy web rate from the old storefront\nset(NONE 0)\n'

PRICING_PY = '"""Checkout pricing: tier discounts from rates.cmake."""\nimport re\nfrom pathlib import Path\n\n_HERE = Path(__file__).parent\n_PATTERN = re.compile(r"set\\((GOLD|SILVER|BRONZE|NONE) ([0-9]+)\\)")\n\n\ndef _rates():\n    text = (_HERE / "rates.cmake").read_text(encoding="utf-8")\n    return {name: int(value) / 100 for name, value in _PATTERN.findall(text)}\n\n\ndef _tier(subtotal):\n    if subtotal >= 200:\n        return "GOLD"\n    if subtotal >= 100:\n        return "SILVER"\n    if subtotal >= 50:\n        return "BRONZE"\n    return "NONE"\n\n\ndef total(subtotal):\n    return round(subtotal * (1 - _rates()[_tier(subtotal)]), 2)\n'

CHECKOUT_TEST_PY = 'from pricing import total\n\nfailures = []\n\n\ndef check(expected, subtotal, label):\n    got = total(subtotal)\n    if got != expected:\n        failures.append(\n            f"FAIL {label}: expected {expected}, got {got}")\n    else:\n        print(f"ok {label}")\n\n\ncheck(200.00, 250, "bulk")\ncheck(108.00, 120, "over100")\ncheck(76.00, 80, "over50")\ncheck(30.00, 30, "small")\n\nfor line in failures:\n    print(line)\nraise SystemExit(1 if failures else 0)\n'

REPRO_PY = 'from pricing import total\n\nif total(120) != 108.00 or total(80) != 76.00:\n    print("discount rates are wrong")\n    raise SystemExit(1)\nprint("repro ok")\n'


def test_cmake_is_a_snapshot_suffix():
    assert ".cmake" in _TEXT_SUFFIXES


def test_snapshot_includes_cmake_files_verbatim(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "rates.cmake").write_text(RATES_CMAKE)
    (cand / "pricing.py").write_text(PRICING_PY)
    files = _candidate_files(cand)
    assert files["rates.cmake"] == RATES_CMAKE
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


class _SnapshotCmakeStub(LLMAdapter):
    """Builds its diff from the snapshot, exactly like a real model must.

    If ``rates.cmake`` is absent from the candidate_files snapshot
    the stub has nothing truthful to diff against and returns no
    diff -- the pre-v1.195.0 live failure shape.
    """

    name = "snapshot-cmake-stub"

    def __init__(self):
        self.seen_snapshots = []

    def generate_patch(self, brief, failing_evidence=None):
        files = (failing_evidence or {}).get("candidate_files") or {}
        self.seen_snapshots.append(sorted(files))
        text = files.get("rates.cmake")
        if text is None:
            return "Plan only: the cmake file was not shown to me."
        lines = text.splitlines()
        old = [ln for ln in lines if "set(SILVER" in ln][0]
        new = old.replace("set(SILVER 3)", "set(SILVER 10)")
        old2 = [ln for ln in lines if "set(BRONZE" in ln][0]
        new2 = old2.replace("set(BRONZE 12)", "set(BRONZE 5)")
        idx = lines.index(old)
        idx2 = lines.index(old2)
        out = ["--- a/rates.cmake", "+++ b/rates.cmake",
               f"@@ -{idx + 1},1 +{idx + 1},1 @@", f"-{old}", f"+{new}",
               f"@@ -{idx2 + 1},1 +{idx2 + 1},1 @@", f"-{old2}", f"+{new2}"]
        return "\n".join(out) + "\n"

    def explain_failure(self, check_results):
        return "stub diagnosis: the cmake rate card has wrong rates"


def _cmake_candidate(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "pricing.py").write_text(PRICING_PY)
    (cand / "rates.cmake").write_text(RATES_CMAKE)
    (cand / "checkout_test.py").write_text(CHECKOUT_TEST_PY)
    (cand / "repro.py").write_text(REPRO_PY)
    return cand


@pytest.mark.skipif(shutil.which("python3") is None,
                    reason="python3 is not available")
def test_cmake_repair_end_to_end_from_snapshot(tmp_path):
    cand = _cmake_candidate(tmp_path)
    spec = parse_spec({
        "name": "shipping-discount-cmake-fix",
        "brief": "Fix rates.cmake so every rate matches the published rules.",
        "checks": [
            {"name": "unit_tests", "command": "python3 checkout_test.py",
             "timeout_s": 120},
            {"name": "repro", "command": "python3 repro.py",
             "timeout_s": 120},
        ],
    })
    stub = _SnapshotCmakeStub()
    report = run_repair(
        spec=spec, candidate_dir=cand, mode="judge",
        llm=stub, references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label=str(cand), max_rounds=3,
    )
    assert report["final_verdict"] == "PASS"
    assert report["stopped_reason"] == "passed"
    assert report["rounds_used"] == 1
    assert "rates.cmake" in stub.seen_snapshots[0]
    for a in report["attempts"]:
        assert verify_chain(a["evidence"]) is True
    assert (cand / "rates.cmake").read_text() == RATES_CMAKE
