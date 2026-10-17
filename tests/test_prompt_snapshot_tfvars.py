"""Prompt-snapshot tfvars coverage (v1.158.0).

The repair loop's patch prompt carries a snapshot of the candidate's
small text files (``repair._candidate_files``). Terraform variable
files were invisible to it: no '.tfvars' suffix was whitelisted at
all, so a candidate whose fix landed in a ``rates.tfvars`` rate card
matched nothing and the file to be patched never reached the prompt.
On the first live candidate whose fix landed in a .tfvars card the
real model named the right file from the brief, invented its
contents, and had every diff correctly rejected
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

RATES_TFVARS = '# Warehouse Terraform-style rate card -- rates.tfvars, rev 2 (do not hand-edit).\n# Shared by the storefront checkout service.\n# Published discount rules: GOLD: 20% off, SILVER: 10% off,\n# BRONZE: 5% off, NONE: no discount.\n# The table below is the rate table.\n\nGOLD    = 0.20\nSTUDENT = 0.15\nSILVER  = 0.03\nSTAFF   = 0.30\nBRONZE  = 0.12\nNONE    = 0.0\n'

EXTRA_TFVARS = '# Extra Terraform variables fragment.\n\nEXTRA = 1\n'

PRICING_PY = '"""Checkout pricing: tier discounts from rates.tfvars."""\nimport re\nfrom pathlib import Path\n\n_HERE = Path(__file__).parent\n_PATTERN = re.compile(r\'(GOLD|SILVER|BRONZE|NONE)\\s*=\\s*([0-9]+(?:\\.[0-9]+)?)\')\n\n\ndef _rates():\n    text = (_HERE / "rates.tfvars").read_text(encoding="utf-8")\n    return {name: float(value) for name, value in _PATTERN.findall(text)}\n\n\ndef _tier(subtotal):\n    if subtotal >= 200:\n        return "GOLD"\n    if subtotal >= 100:\n        return "SILVER"\n    if subtotal >= 50:\n        return "BRONZE"\n    return "NONE"\n\n\ndef total(subtotal):\n    return round(subtotal * (1 - _rates()[_tier(subtotal)]), 2)\n'

CHECKOUT_TEST_PY = 'from pricing import total\n\nfailures = []\n\n\ndef check(expected, subtotal, label):\n    got = total(subtotal)\n    if got != expected:\n        failures.append(\n            f"FAIL {label}: expected {expected}, got {got}")\n    else:\n        print(f"ok {label}")\n\n\ncheck(200.00, 250, "bulk")\ncheck(108.00, 120, "over100")\ncheck(76.00, 80, "over50")\ncheck(30.00, 30, "small")\n\nfor line in failures:\n    print(line)\nraise SystemExit(1 if failures else 0)\n'

REPRO_PY = 'from pricing import total\n\nif total(120) != 108.00 or total(80) != 76.00:\n    print("discount rates are wrong")\n    raise SystemExit(1)\nprint("repro ok")\n'


def test_tfvars_is_a_snapshot_suffix():
    assert ".tfvars" in _TEXT_SUFFIXES


def test_snapshot_includes_tfvars_files_verbatim(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "rates.tfvars").write_text(RATES_TFVARS)
    (cand / "extra.tfvars").write_text(EXTRA_TFVARS)
    (cand / "pricing.py").write_text(PRICING_PY)
    files = _candidate_files(cand)
    assert files["rates.tfvars"] == RATES_TFVARS
    assert files["extra.tfvars"] == EXTRA_TFVARS
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


class _SnapshotTfvarsStub(LLMAdapter):
    """Builds its diff from the snapshot, exactly like a real model must.

    If ``rates.tfvars`` is absent from the candidate_files snapshot
    the stub has nothing truthful to diff against and returns no
    diff -- the pre-v1.158.0 live failure shape.
    """

    name = "snapshot-tfvars-stub"

    def __init__(self):
        self.seen_snapshots = []

    def generate_patch(self, brief, failing_evidence=None):
        files = (failing_evidence or {}).get("candidate_files") or {}
        self.seen_snapshots.append(sorted(files))
        text = files.get("rates.tfvars")
        if text is None:
            return "Plan only: the tfvars file was not shown to me."
        lines = text.splitlines()
        old = 'SILVER  = 0.03'
        new = 'SILVER  = 0.10'
        old2 = 'BRONZE  = 0.12'
        new2 = 'BRONZE  = 0.05'
        idx = next(i for i, ln in enumerate(lines) if ln == old)
        idx2 = next(i for i, ln in enumerate(lines) if ln == old2)
        out = ["--- a/rates.tfvars", "+++ b/rates.tfvars",
               f"@@ -{idx + 1},1 +{idx + 1},1 @@", f"-{old}", f"+{new}",
               f"@@ -{idx2 + 1},1 +{idx2 + 1},1 @@", f"-{old2}", f"+{new2}"]
        return "\n".join(out) + "\n"

    def explain_failure(self, check_results):
        return "stub diagnosis: the tfvars rate card has wrong rates"


def _tfvars_candidate(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "pricing.py").write_text(PRICING_PY)
    (cand / "rates.tfvars").write_text(RATES_TFVARS)
    (cand / "checkout_test.py").write_text(CHECKOUT_TEST_PY)
    (cand / "repro.py").write_text(REPRO_PY)
    return cand


@pytest.mark.skipif(shutil.which("python3") is None,
                    reason="python3 is not available")
def test_tfvars_repair_end_to_end_from_snapshot(tmp_path):
    cand = _tfvars_candidate(tmp_path)
    spec = parse_spec({
        "name": "shipping-discount-tfvars-fix",
        "brief": "Fix rates.tfvars so every rate matches the published rules.",
        "checks": [
            {"name": "unit_tests", "command": "python3 checkout_test.py",
             "timeout_s": 120},
            {"name": "repro", "command": "python3 repro.py",
             "timeout_s": 120},
        ],
    })
    stub = _SnapshotTfvarsStub()
    report = run_repair(
        spec=spec, candidate_dir=cand, mode="judge",
        llm=stub, references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label=str(cand), max_rounds=3,
    )
    assert report["final_verdict"] == "PASS"
    assert report["stopped_reason"] == "passed"
    assert report["rounds_used"] == 1
    assert "rates.tfvars" in stub.seen_snapshots[0]
    for a in report["attempts"]:
        assert verify_chain(a["evidence"]) is True
    assert (cand / "rates.tfvars").read_text() == RATES_TFVARS
