"""Prompt-snapshot suffix coverage (v1.109.0).

The repair loop's patch prompt carries a snapshot of the candidate's
small text files (``repair._candidate_files``): a model asked for a
unified diff must see the exact contents the diff will be applied to.
The suffix whitelist omitted ``.sql`` (and the other data/query text
formats added in v1.109.0), so on the first live SQL candidate the file
to be fixed never reached the prompt at all -- both real models
diagnosed the bug correctly, invented plausible file contents, and
had every diff correctly rejected (``patch_not_applicable``). These
tests pin the snapshot inclusion, the unchanged whitelist posture for
unlisted/binary files, and an end-to-end repair whose stub adapter
can only build its diff from the snapshot it is handed.
"""

import pytest

from verifygate.evidence import verify_chain
from verifygate.llm import LLMAdapter
from verifygate.repair import _TEXT_SUFFIXES, _candidate_files, run_repair
from verifygate.spec import parse_spec
from verifygate.tavily import MockReferenceLookup

TOTALS_SQL = (
    "SELECT c.name || '|' || printf('%.2f', SUM(o.amount)) AS line\n"
    "FROM customers c\n"
    "JOIN orders o ON o.customer_id = c.id\n"
    "GROUP BY c.id, c.name\n"
    "ORDER BY SUM(o.amount) DESC;\n"
)

SCHEMA_SQL = (
    "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL);\n"
    "CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL,"
    " amount REAL NOT NULL, status TEXT NOT NULL);\n"
    "INSERT INTO customers VALUES (1, 'Ada'), (2, 'Ben'), (3, 'Cy');\n"
    "INSERT INTO orders VALUES\n"
    "  (1, 1, 100.00, 'completed'),\n"
    "  (2, 1, 50.00, 'cancelled'),\n"
    "  (3, 2, 200.00, 'completed'),\n"
    "  (4, 2, 25.50, 'completed'),\n"
    "  (5, 3, 75.00, 'cancelled'),\n"
    "  (6, 1, 10.00, 'completed');\n"
)


def test_sql_is_a_snapshot_suffix():
    assert ".sql" in _TEXT_SUFFIXES


@pytest.mark.parametrize(
    "suffix", [".csv", ".tsv", ".xml", ".graphql", ".proto",
               ".lua", ".pl", ".swift", ".kt", ".kts", ".scala", ".groovy"]
)
def test_added_text_suffixes_are_snapshot_suffixes(suffix):
    assert suffix in _TEXT_SUFFIXES


def test_snapshot_includes_sql_files_verbatim(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "totals.sql").write_text(TOTALS_SQL)
    (cand / "schema.sql").write_text(SCHEMA_SQL)
    files = _candidate_files(cand)
    assert files["totals.sql"] == TOTALS_SQL
    assert files["schema.sql"] == SCHEMA_SQL


def test_snapshot_includes_csv_and_xml(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "rates.csv").write_text("code,rate\nUSD,1.0\n")
    (cand / "config.xml").write_text("<config><mode>on</mode></config>\n")
    files = _candidate_files(cand)
    assert files["rates.csv"] == "code,rate\nUSD,1.0\n"
    assert files["config.xml"] == "<config><mode>on</mode></config>\n"


def test_snapshot_still_skips_unlisted_and_binary_files(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "blob.bin").write_text("plain text, unlisted suffix\n")
    (cand / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff\xfe")
    (cand / "ok.py").write_text("x = 1\n")
    files = _candidate_files(cand)
    assert files == {"ok.py": "x = 1\n"}


class _SnapshotSqlStub(LLMAdapter):
    """Builds its diff from the snapshot, exactly like a real model must.

    If ``totals.sql`` is absent from the candidate_files snapshot the
    stub has nothing truthful to diff against and returns no diff --
    the pre-v1.109.0 live failure shape.
    """

    name = "snapshot-sql-stub"

    def __init__(self):
        self.seen_snapshots = []

    def generate_patch(self, brief, failing_evidence=None):
        files = (failing_evidence or {}).get("candidate_files") or {}
        self.seen_snapshots.append(sorted(files))
        text = files.get("totals.sql")
        if text is None:
            return "Plan only: the query file was not shown to me."
        lines = text.splitlines()
        join_idx = next(i for i, ln in enumerate(lines) if ln.startswith("JOIN"))
        context = lines[join_idx - 1:join_idx + 2]
        assert len(context) == 3
        return (
            "--- a/totals.sql\n"
            "+++ b/totals.sql\n"
            f"@@ -{join_idx},3 +{join_idx},4 @@\n"
            f" {context[0]}\n"
            f" {context[1]}\n"
            "+WHERE o.status <> 'cancelled'\n"
            f" {context[2]}\n"
        )

    def explain_failure(self, check_results):
        return "stub diagnosis: cancelled orders are included"


def _sql_candidate(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "schema.sql").write_text(SCHEMA_SQL)
    (cand / "totals.sql").write_text(TOTALS_SQL)
    (cand / "test_totals.sh").write_text(
        "#!/bin/bash\n"
        "set -u\n"
        "got=\"$(sqlite3 :memory: \".read schema.sql\" \".read totals.sql\")\"\n"
        "expected=\"Ben|225.50\nAda|110.00\"\n"
        "[ \"$got\" = \"$expected\" ] || { echo \"FAIL: $got\"; exit 1; }\n"
        "echo ok\n"
    )
    return cand


def test_sql_repair_end_to_end_from_snapshot(tmp_path):
    cand = _sql_candidate(tmp_path)
    spec = parse_spec({
        "name": "customer-totals-sql-fix",
        "brief": "Exclude cancelled orders from totals.sql.",
        "checks": [{"name": "sql_tests", "command": "bash test_totals.sh",
                    "timeout_s": 60}],
    })
    stub = _SnapshotSqlStub()
    report = run_repair(
        spec=spec, candidate_dir=cand, mode="judge",
        llm=stub, references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label=str(cand), max_rounds=3,
    )
    assert report["final_verdict"] == "PASS"
    assert report["stopped_reason"] == "passed"
    assert report["rounds_used"] == 1
    # The stub was actually handed the SQL file in its snapshot.
    assert "totals.sql" in stub.seen_snapshots[0]
    assert "schema.sql" in stub.seen_snapshots[0]
    for a in report["attempts"]:
        assert verify_chain(a["evidence"]) is True
    # The original candidate on disk is never modified.
    assert (cand / "totals.sql").read_text() == TOTALS_SQL
