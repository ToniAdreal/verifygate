"""Prompt-snapshot .env coverage (v1.112.0).

The repair loop's patch prompt carries a snapshot of the candidate's
small text files (``repair._candidate_files``). Env-style config files
were invisible to it: ``rates.env`` matched no whitelisted suffix, and
a file named exactly ``.env`` carries no suffix at all (a leading-dot
name is all stem), so neither form could reach the prompt. On the
first live candidate whose fix landed in an env rate card the real
model named the right file from the brief, invented its contents, and
had every diff correctly rejected (``patch_not_applicable``, 4 real
calls / 16,801 tokens). These tests pin both inclusion forms, the
unchanged whitelist posture for unlisted/binary files, and an
end-to-end repair whose stub adapter can only build its diff from the
snapshot it is handed.
"""

from verifygate.evidence import verify_chain
from verifygate.llm import LLMAdapter
from verifygate.repair import (
    _TEXT_BASENAMES,
    _TEXT_SUFFIXES,
    _candidate_files,
    run_repair,
)
from verifygate.spec import parse_spec
from verifygate.tavily import MockReferenceLookup

RATES_ENV = (
    "# Parcel rate card -- env copy (fictional)\n"
    "BASE_FEE=5.00\n"
    "PER_KG=12.0\n"
    "PER_KM=0.05\n"
    "# legacy bulk tier kept for reference: PER_KG_BULK=0.9\n"
    "FREE_THRESHOLD=750.0\n"
)

PRICING_PY = (
    "from pathlib import Path\n"
    "\n"
    "\n"
    "def _load_rates():\n"
    "    rates = {}\n"
    "    text = (Path(__file__).parent / \"rates.env\").read_text(encoding=\"utf-8\")\n"
    "    for line in text.splitlines():\n"
    "        line = line.strip()\n"
    "        if not line or line.startswith(\"#\") or \"=\" not in line:\n"
    "            continue\n"
    "        key, _, value = line.partition(\"=\")\n"
    "        rates[key.strip()] = float(value.strip())\n"
    "    return rates\n"
    "\n"
    "\n"
    "_RATES = _load_rates()\n"
    "\n"
    "\n"
    "def quote(weight_kg, distance_km, subtotal):\n"
    "    if subtotal >= _RATES[\"FREE_THRESHOLD\"]:\n"
    "        return 0.0\n"
    "    cost = (\n"
    "        _RATES[\"BASE_FEE\"]\n"
    "        + _RATES[\"PER_KG\"] * weight_kg\n"
    "        + _RATES[\"PER_KM\"] * distance_km\n"
    "    )\n"
    "    return round(cost, 2)\n"
)


def test_env_is_a_snapshot_suffix_and_basename():
    assert ".env" in _TEXT_SUFFIXES
    # A file named exactly '.env' has no suffix; only the basename
    # set can ever include it.
    assert ".env" in _TEXT_BASENAMES


def test_snapshot_includes_env_files_verbatim(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "rates.env").write_text(RATES_ENV)
    (cand / ".env").write_text("EXTRA_FLAG=on\n")
    (cand / "pricing.py").write_text(PRICING_PY)
    files = _candidate_files(cand)
    assert files["rates.env"] == RATES_ENV
    assert files[".env"] == "EXTRA_FLAG=on\n"
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


class _SnapshotEnvStub(LLMAdapter):
    """Builds its diff from the snapshot, exactly like a real model must.

    If ``rates.env`` is absent from the candidate_files snapshot the
    stub has nothing truthful to diff against and returns no diff --
    the pre-v1.112.0 live failure shape.
    """

    name = "snapshot-env-stub"

    def __init__(self):
        self.seen_snapshots = []

    def generate_patch(self, brief, failing_evidence=None):
        files = (failing_evidence or {}).get("candidate_files") or {}
        self.seen_snapshots.append(sorted(files))
        text = files.get("rates.env")
        if text is None:
            return "Plan only: the env file was not shown to me."
        lines = text.splitlines()
        out = ["--- a/rates.env", "+++ b/rates.env"]
        for old, new in (("PER_KG=12.0", "PER_KG=1.2"),
                         ("FREE_THRESHOLD=750.0", "FREE_THRESHOLD=75.0")):
            idx = next(i for i, ln in enumerate(lines) if ln == old)
            out.append(f"@@ -{idx + 1},1 +{idx + 1},1 @@")
            out.append(f"-{old}")
            out.append(f"+{new}")
        return "\n".join(out) + "\n"

    def explain_failure(self, check_results):
        return "stub diagnosis: the env rate card has wrong values"


def _env_candidate(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "pricing.py").write_text(PRICING_PY)
    (cand / "rates.env").write_text(RATES_ENV)
    (cand / "test_pricing.py").write_text(
        "from pricing import quote\n"
        "\n"
        "\n"
        "def test_standard_quote():\n"
        "    assert quote(2, 100, 20.0) == 12.40\n"
        "\n"
        "\n"
        "def test_free_shipping_at_threshold():\n"
        "    assert quote(3, 200, 75.0) == 0.0\n"
    )
    return cand


def test_env_repair_end_to_end_from_snapshot(tmp_path):
    cand = _env_candidate(tmp_path)
    spec = parse_spec({
        "name": "parcel-quote-env-fix",
        "brief": "Fix rates.env so every value matches the published rate card.",
        "checks": [{"name": "unit_tests", "command": "python3 -m pytest -q",
                    "timeout_s": 60}],
    })
    stub = _SnapshotEnvStub()
    report = run_repair(
        spec=spec, candidate_dir=cand, mode="judge",
        llm=stub, references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label=str(cand), max_rounds=3,
    )
    assert report["final_verdict"] == "PASS"
    assert report["stopped_reason"] == "passed"
    assert report["rounds_used"] == 1
    # The stub was actually handed the env file in its snapshot.
    assert "rates.env" in stub.seen_snapshots[0]
    for a in report["attempts"]:
        assert verify_chain(a["evidence"]) is True
    # The original candidate on disk is never modified.
    assert (cand / "rates.env").read_text() == RATES_ENV
