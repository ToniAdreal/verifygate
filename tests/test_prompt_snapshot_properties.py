"""Prompt-snapshot .properties coverage (v1.111.0).

The repair loop's patch prompt carries a snapshot of the candidate's
small text files (``repair._candidate_files``). The suffix whitelist
omitted ``.properties``, so on the first live candidate whose fix
landed in a Java-style properties rate card the file to be fixed never
reached the prompt at all -- the real model named the right file from
the brief, invented its contents, and had every diff correctly
rejected (``patch_not_applicable``, 4 real calls / 22,238 tokens).
These tests pin the snapshot inclusion, the unchanged whitelist
posture for unlisted/binary files, and an end-to-end repair whose stub
adapter can only build its diff from the snapshot it is handed.
"""

from verifygate.evidence import verify_chain
from verifygate.llm import LLMAdapter
from verifygate.repair import _TEXT_SUFFIXES, _candidate_files, run_repair
from verifygate.spec import parse_spec
from verifygate.tavily import MockReferenceLookup

RATES_PROPERTIES = (
    "# Parcel rate card -- storefront copy (fictional)\n"
    "base.fee = 5.00\n"
    "per.kg = 12.0\n"
    "per.km = 0.05\n"
    "# legacy bulk tier kept for reference: per.kg.bulk = 0.9\n"
    "free.threshold = 750.0\n"
)

PRICING_PY = (
    "from pathlib import Path\n"
    "\n"
    "\n"
    "def _load_rates():\n"
    "    rates = {}\n"
    "    text = (Path(__file__).parent / \"rates.properties\").read_text(encoding=\"utf-8\")\n"
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
    "    if subtotal >= _RATES[\"free.threshold\"]:\n"
    "        return 0.0\n"
    "    cost = (\n"
    "        _RATES[\"base.fee\"]\n"
    "        + _RATES[\"per.kg\"] * weight_kg\n"
    "        + _RATES[\"per.km\"] * distance_km\n"
    "    )\n"
    "    return round(cost, 2)\n"
)


def test_properties_is_a_snapshot_suffix():
    assert ".properties" in _TEXT_SUFFIXES


def test_snapshot_includes_properties_verbatim(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "rates.properties").write_text(RATES_PROPERTIES)
    (cand / "pricing.py").write_text(PRICING_PY)
    files = _candidate_files(cand)
    assert files["rates.properties"] == RATES_PROPERTIES
    assert files["pricing.py"] == PRICING_PY


def test_snapshot_still_skips_unlisted_and_binary_files(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "blob.bin").write_text("plain text, unlisted suffix\n")
    (cand / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff\xfe")
    (cand / "ok.py").write_text("x = 1\n")
    files = _candidate_files(cand)
    assert files == {"ok.py": "x = 1\n"}


class _SnapshotPropertiesStub(LLMAdapter):
    """Builds its diff from the snapshot, exactly like a real model must.

    If ``rates.properties`` is absent from the candidate_files snapshot
    the stub has nothing truthful to diff against and returns no diff --
    the pre-v1.111.0 live failure shape.
    """

    name = "snapshot-properties-stub"

    def __init__(self):
        self.seen_snapshots = []

    def generate_patch(self, brief, failing_evidence=None):
        files = (failing_evidence or {}).get("candidate_files") or {}
        self.seen_snapshots.append(sorted(files))
        text = files.get("rates.properties")
        if text is None:
            return "Plan only: the properties file was not shown to me."
        lines = text.splitlines()
        out = ["--- a/rates.properties", "+++ b/rates.properties"]
        for old, new in (("per.kg = 12.0", "per.kg = 1.2"),
                         ("free.threshold = 750.0", "free.threshold = 75.0")):
            idx = next(i for i, ln in enumerate(lines) if ln == old)
            out.append(f"@@ -{idx + 1},1 +{idx + 1},1 @@")
            out.append(f"-{old}")
            out.append(f"+{new}")
        return "\n".join(out) + "\n"

    def explain_failure(self, check_results):
        return "stub diagnosis: the properties rate card has wrong values"


def _properties_candidate(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "pricing.py").write_text(PRICING_PY)
    (cand / "rates.properties").write_text(RATES_PROPERTIES)
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


def test_properties_repair_end_to_end_from_snapshot(tmp_path):
    cand = _properties_candidate(tmp_path)
    spec = parse_spec({
        "name": "parcel-quote-properties-fix",
        "brief": "Fix rates.properties so every value matches the published rate card.",
        "checks": [{"name": "unit_tests", "command": "python3 -m pytest -q",
                    "timeout_s": 60}],
    })
    stub = _SnapshotPropertiesStub()
    report = run_repair(
        spec=spec, candidate_dir=cand, mode="judge",
        llm=stub, references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label=str(cand), max_rounds=3,
    )
    assert report["final_verdict"] == "PASS"
    assert report["stopped_reason"] == "passed"
    assert report["rounds_used"] == 1
    # The stub was actually handed the properties file in its snapshot.
    assert "rates.properties" in stub.seen_snapshots[0]
    for a in report["attempts"]:
        assert verify_chain(a["evidence"]) is True
    # The original candidate on disk is never modified.
    assert (cand / "rates.properties").read_text() == RATES_PROPERTIES
