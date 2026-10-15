"""Prompt-snapshot .conf coverage (v1.113.0).

The repair loop's patch prompt carries a snapshot of the candidate's
small text files (``repair._candidate_files``). Generic ``.conf``
config files were invisible to it: ``rates.conf`` matched no
whitelisted suffix, so the file to be patched never reached the
prompt. On the first live candidate whose fix landed in a .conf rate
card the real model named the right file from the brief, invented its
contents, and had every diff correctly rejected
(``patch_not_applicable``, 4 real calls / 18,508 tokens). These tests
pin the inclusion, the unchanged whitelist posture for unlisted/binary
files, and an end-to-end repair whose stub adapter can only build its
diff from the snapshot it is handed.
"""

from verifygate.evidence import verify_chain
from verifygate.llm import LLMAdapter
from verifygate.repair import (
    _TEXT_SUFFIXES,
    _candidate_files,
    run_repair,
)
from verifygate.spec import parse_spec
from verifygate.tavily import MockReferenceLookup

RATES_CONF = (
    "# Delivery rate card -- conf copy (fictional)\n"
    "# coastal depot surcharge waived in 2026 -- do not re-add\n"
    "BASE_FEE 3.00\n"
    "PER_KG 8.0\n"
    "PER_ITEM 0.35\n"
    "# legacy bulk tier kept for reference: PER_KG_BULK 0.6\n"
    "FREE_THRESHOLD 1200.0\n"
)

DELIVERY_PY = (
    "from pathlib import Path\n"
    "\n"
    "\n"
    "def _load_rates():\n"
    "    rates = {}\n"
    "    text = (Path(__file__).parent / \"rates.conf\").read_text(encoding=\"utf-8\")\n"
    "    for line in text.splitlines():\n"
    "        line = line.strip()\n"
    "        if not line or line.startswith(\"#\"):\n"
    "            continue\n"
    "        parts = line.split()\n"
    "        if len(parts) != 2:\n"
    "            continue\n"
    "        rates[parts[0]] = float(parts[1])\n"
    "    return rates\n"
    "\n"
    "\n"
    "_RATES = _load_rates()\n"
    "\n"
    "\n"
    "def quote(weight_kg, item_count, subtotal):\n"
    "    if subtotal >= _RATES[\"FREE_THRESHOLD\"]:\n"
    "        return 0.0\n"
    "    cost = (\n"
    "        _RATES[\"BASE_FEE\"]\n"
    "        + _RATES[\"PER_KG\"] * weight_kg\n"
    "        + _RATES[\"PER_ITEM\"] * item_count\n"
    "    )\n"
    "    return round(cost, 2)\n"
)


def test_conf_is_a_snapshot_suffix():
    assert ".conf" in _TEXT_SUFFIXES


def test_snapshot_includes_conf_files_verbatim(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "rates.conf").write_text(RATES_CONF)
    (cand / "delivery.py").write_text(DELIVERY_PY)
    files = _candidate_files(cand)
    assert files["rates.conf"] == RATES_CONF
    assert files["delivery.py"] == DELIVERY_PY


def test_snapshot_still_skips_unlisted_and_binary_files(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "blob.bin").write_text("plain text, unlisted suffix\n")
    (cand / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff\xfe")
    (cand / "notes").write_text("extensionless, unlisted basename\n")
    (cand / "ok.py").write_text("x = 1\n")
    files = _candidate_files(cand)
    assert files == {"ok.py": "x = 1\n"}


class _SnapshotConfStub(LLMAdapter):
    """Builds its diff from the snapshot, exactly like a real model must.

    If ``rates.conf`` is absent from the candidate_files snapshot the
    stub has nothing truthful to diff against and returns no diff --
    the pre-v1.113.0 live failure shape.
    """

    name = "snapshot-conf-stub"

    def __init__(self):
        self.seen_snapshots = []

    def generate_patch(self, brief, failing_evidence=None):
        files = (failing_evidence or {}).get("candidate_files") or {}
        self.seen_snapshots.append(sorted(files))
        text = files.get("rates.conf")
        if text is None:
            return "Plan only: the conf file was not shown to me."
        lines = text.splitlines()
        out = ["--- a/rates.conf", "+++ b/rates.conf"]
        for old, new in (("PER_KG 8.0", "PER_KG 0.80"),
                         ("FREE_THRESHOLD 1200.0", "FREE_THRESHOLD 120.0")):
            idx = next(i for i, ln in enumerate(lines) if ln == old)
            out.append(f"@@ -{idx + 1},1 +{idx + 1},1 @@")
            out.append(f"-{old}")
            out.append(f"+{new}")
        return "\n".join(out) + "\n"

    def explain_failure(self, check_results):
        return "stub diagnosis: the conf rate card has wrong values"


def _conf_candidate(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "delivery.py").write_text(DELIVERY_PY)
    (cand / "rates.conf").write_text(RATES_CONF)
    (cand / "test_delivery.py").write_text(
        "from delivery import quote\n"
        "\n"
        "\n"
        "def test_standard_quote():\n"
        "    assert quote(5, 4, 20.0) == 8.40\n"
        "\n"
        "\n"
        "def test_free_delivery_at_threshold():\n"
        "    assert quote(2, 1, 120.0) == 0.0\n"
    )
    return cand


def test_conf_repair_end_to_end_from_snapshot(tmp_path):
    cand = _conf_candidate(tmp_path)
    spec = parse_spec({
        "name": "delivery-quote-conf-fix",
        "brief": "Fix rates.conf so every value matches the published rate card.",
        "checks": [{"name": "unit_tests", "command": "python3 -m pytest -q",
                    "timeout_s": 60}],
    })
    stub = _SnapshotConfStub()
    report = run_repair(
        spec=spec, candidate_dir=cand, mode="judge",
        llm=stub, references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label=str(cand), max_rounds=3,
    )
    assert report["final_verdict"] == "PASS"
    assert report["stopped_reason"] == "passed"
    assert report["rounds_used"] == 1
    # The stub was actually handed the conf file in its snapshot.
    assert "rates.conf" in stub.seen_snapshots[0]
    for a in report["attempts"]:
        assert verify_chain(a["evidence"]) is True
    # The original candidate on disk is never modified.
    assert (cand / "rates.conf").read_text() == RATES_CONF
