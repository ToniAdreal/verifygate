"""Prompt-snapshot .jsonl coverage (v1.114.0).

The repair loop's patch prompt carries a snapshot of the candidate's
small text files (``repair._candidate_files``). JSON Lines data files
were invisible to it: ``.json`` is whitelisted but the snapshot
matches the full suffix, so ``tiers.jsonl`` (and its ``.ndjson``
alias) matched nothing and the file to be patched never reached the
prompt. On the first live candidate whose fix landed in a .jsonl
tier table the real model named the right file from the brief,
invented its contents, and had every diff correctly rejected
(``patch_not_applicable``, 4 real calls / 13,774 tokens). These tests
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

TIERS_JSONL = (
    '{"tier": "standard", "base": 4.00, "per_kg": 9.0}\n'
    '{"tier": "express", "base": 12.00, "per_kg": 2.40}\n'
    '{"tier": "freight", "base": 40.00, "per_kg": 1.10}\n'
)

NOTES_NDJSON = (
    '{"event": "card-published", "year": 2026}\n'
    '{"event": "card-audited", "year": 2026}\n'
)

PARCEL_PY = (
    "import json\n"
    "from pathlib import Path\n"
    "\n"
    "\n"
    "def _load_tiers():\n"
    "    tiers = {}\n"
    "    text = (Path(__file__).parent / \"tiers.jsonl\").read_text(encoding=\"utf-8\")\n"
    "    for line in text.splitlines():\n"
    "        line = line.strip()\n"
    "        if not line:\n"
    "            continue\n"
    "        row = json.loads(line)\n"
    "        tiers[row[\"tier\"]] = (float(row[\"base\"]), float(row[\"per_kg\"]))\n"
    "    return tiers\n"
    "\n"
    "\n"
    "_TIERS = _load_tiers()\n"
    "\n"
    "\n"
    "def quote(tier, weight_kg):\n"
    "    base, per_kg = _TIERS[tier]\n"
    "    return round(base + per_kg * weight_kg, 2)\n"
)


def test_jsonl_and_ndjson_are_snapshot_suffixes():
    assert ".jsonl" in _TEXT_SUFFIXES
    assert ".ndjson" in _TEXT_SUFFIXES


def test_snapshot_includes_jsonl_files_verbatim(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "tiers.jsonl").write_text(TIERS_JSONL)
    (cand / "notes.ndjson").write_text(NOTES_NDJSON)
    (cand / "parcel.py").write_text(PARCEL_PY)
    files = _candidate_files(cand)
    assert files["tiers.jsonl"] == TIERS_JSONL
    assert files["notes.ndjson"] == NOTES_NDJSON
    assert files["parcel.py"] == PARCEL_PY


def test_snapshot_still_skips_unlisted_and_binary_files(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "blob.bin").write_text("plain text, unlisted suffix\n")
    (cand / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff\xfe")
    (cand / "notes").write_text("extensionless, unlisted basename\n")
    (cand / "ok.py").write_text("x = 1\n")
    files = _candidate_files(cand)
    assert files == {"ok.py": "x = 1\n"}


class _SnapshotJsonlStub(LLMAdapter):
    """Builds its diff from the snapshot, exactly like a real model must.

    If ``tiers.jsonl`` is absent from the candidate_files snapshot the
    stub has nothing truthful to diff against and returns no diff --
    the pre-v1.114.0 live failure shape.
    """

    name = "snapshot-jsonl-stub"

    def __init__(self):
        self.seen_snapshots = []

    def generate_patch(self, brief, failing_evidence=None):
        files = (failing_evidence or {}).get("candidate_files") or {}
        self.seen_snapshots.append(sorted(files))
        text = files.get("tiers.jsonl")
        if text is None:
            return "Plan only: the jsonl file was not shown to me."
        lines = text.splitlines()
        out = ["--- a/tiers.jsonl", "+++ b/tiers.jsonl"]
        for old, new in (
            ('{"tier": "standard", "base": 4.00, "per_kg": 9.0}',
             '{"tier": "standard", "base": 4.00, "per_kg": 0.90}'),
            ('{"tier": "express", "base": 12.00, "per_kg": 2.40}',
             '{"tier": "express", "base": 21.00, "per_kg": 2.40}'),
        ):
            idx = next(i for i, ln in enumerate(lines) if ln == old)
            out.append(f"@@ -{idx + 1},1 +{idx + 1},1 @@")
            out.append(f"-{old}")
            out.append(f"+{new}")
        return "\n".join(out) + "\n"

    def explain_failure(self, check_results):
        return "stub diagnosis: the jsonl tier table has wrong values"


def _jsonl_candidate(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "parcel.py").write_text(PARCEL_PY)
    (cand / "tiers.jsonl").write_text(TIERS_JSONL)
    (cand / "test_parcel.py").write_text(
        "from parcel import quote\n"
        "\n"
        "\n"
        "def test_standard_quote():\n"
        "    assert quote(\"standard\", 5) == 8.50\n"
        "\n"
        "\n"
        "def test_express_quote():\n"
        "    assert quote(\"express\", 3) == 28.20\n"
        "\n"
        "\n"
        "def test_freight_quote():\n"
        "    assert quote(\"freight\", 10) == 51.00\n"
    )
    return cand


def test_jsonl_repair_end_to_end_from_snapshot(tmp_path):
    cand = _jsonl_candidate(tmp_path)
    spec = parse_spec({
        "name": "parcel-quote-jsonlcard-fix",
        "brief": "Fix tiers.jsonl so every value matches the published tier table.",
        "checks": [{"name": "unit_tests", "command": "python3 -m pytest -q",
                    "timeout_s": 60}],
    })
    stub = _SnapshotJsonlStub()
    report = run_repair(
        spec=spec, candidate_dir=cand, mode="judge",
        llm=stub, references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label=str(cand), max_rounds=3,
    )
    assert report["final_verdict"] == "PASS"
    assert report["stopped_reason"] == "passed"
    assert report["rounds_used"] == 1
    # The stub was actually handed the jsonl file in its snapshot.
    assert "tiers.jsonl" in stub.seen_snapshots[0]
    for a in report["attempts"]:
        assert verify_chain(a["evidence"]) is True
    # The original candidate on disk is never modified.
    assert (cand / "tiers.jsonl").read_text() == TIERS_JSONL
