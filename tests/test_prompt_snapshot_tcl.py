"""Prompt-snapshot Tcl coverage (v1.123.0).

The repair loop's patch prompt carries a snapshot of the candidate's
small text files (``repair._candidate_files``). Tcl programs were
invisible to it: no ``.tcl`` suffix was whitelisted (the ``.exp``
entry covers Expect scripts, not plain Tcl), so a candidate whose
fix landed in a ``rates.tcl`` program matched nothing and the file
to be patched never reached the prompt. On the first live candidate
whose fix landed in a .tcl program the real model named the right
file from the brief, invented its contents, and had every diff
correctly rejected (``patch_not_applicable``). These tests pin the
inclusion, the unchanged whitelist posture for unlisted/binary
files, and an end-to-end repair whose stub adapter can only build
its diff from the snapshot it is handed.
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

RATES_TCL = (
    "# Warehouse B rate card -- Tcl table, rev 7 (do not hand-edit).\n"
    "# Published discount rules: GOLD -> 20% off, SILVER -> 10% off,\n"
    "# BRONZE -> 5% off, NONE -> no discount.\n"
    "array set rate {\n"
    "    GOLD   0.20\n"
    "    SILVER 0.03\n"
    "    BRONZE 0.12\n"
    "    NONE   0\n"
    "}\n"
    "set tier [lindex $argv 0]\n"
    "if {![info exists rate($tier)]} { set tier NONE }\n"
    "puts $rate($tier)\n"
)

EXTRA_TCL = "set tier EXTRA\n"

CHECKOUT_SH = (
    "#!/usr/bin/env bash\n"
    "# Correct as delivered: computes the checkout total via rates.tcl.\n"
    "here=\"$(dirname \"${BASH_SOURCE[0]}\")\"\n"
    "\n"
    "tier() {\n"
    "  if (( $1 >= 200 )); then echo GOLD;\n"
    "  elif (( $1 >= 100 )); then echo SILVER;\n"
    "  elif (( $1 >= 50 )); then echo BRONZE;\n"
    "  else echo NONE; fi\n"
    "}\n"
    "\n"
    "total() {\n"
    "  local t rate\n"
    "  t=\"$(tier \"$1\")\"\n"
    "  rate=\"$(expect \"$here/rates.tcl\" \"$t\")\"\n"
    "  awk -v s=\"$1\" -v r=\"$rate\""
    " 'BEGIN { printf \"%.2f\\n\", s * (1 - r) }'\n"
    "}\n"
    "\n"
    "if [[ \"${BASH_SOURCE[0]}\" == \"${0}\" ]]; then\n"
    "  total \"$1\"\n"
    "fi\n"
)


def test_tcl_is_a_snapshot_suffix():
    assert ".tcl" in _TEXT_SUFFIXES


def test_snapshot_includes_tcl_files_verbatim(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "rates.tcl").write_text(RATES_TCL)
    (cand / "extra.tcl").write_text(EXTRA_TCL)
    (cand / "checkout.sh").write_text(CHECKOUT_SH)
    files = _candidate_files(cand)
    assert files["rates.tcl"] == RATES_TCL
    assert files["extra.tcl"] == EXTRA_TCL
    assert files["checkout.sh"] == CHECKOUT_SH


def test_snapshot_still_skips_unlisted_and_binary_files(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "blob.bin").write_text("plain text, unlisted suffix\n")
    (cand / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff\xfe")
    (cand / "notes").write_text("extensionless, unlisted basename\n")
    (cand / "ok.py").write_text("x = 1\n")
    files = _candidate_files(cand)
    assert files == {"ok.py": "x = 1\n"}


class _SnapshotTclStub(LLMAdapter):
    """Builds its diff from the snapshot, exactly like a real model must.

    If ``rates.tcl`` is absent from the candidate_files snapshot the
    stub has nothing truthful to diff against and returns no diff --
    the pre-v1.123.0 live failure shape.
    """

    name = "snapshot-tcl-stub"

    def __init__(self):
        self.seen_snapshots = []

    def generate_patch(self, brief, failing_evidence=None):
        files = (failing_evidence or {}).get("candidate_files") or {}
        self.seen_snapshots.append(sorted(files))
        text = files.get("rates.tcl")
        if text is None:
            return "Plan only: the Tcl file was not shown to me."
        lines = text.splitlines()
        out = ["--- a/rates.tcl", "+++ b/rates.tcl"]
        for old, new in (
            ("    SILVER 0.03", "    SILVER 0.10"),
            ("    BRONZE 0.12", "    BRONZE 0.05"),
        ):
            idx = next(i for i, ln in enumerate(lines) if ln == old)
            out.append(f"@@ -{idx + 1},1 +{idx + 1},1 @@")
            out.append(f"-{old}")
            out.append(f"+{new}")
        return "\n".join(out) + "\n"

    def explain_failure(self, check_results):
        return "stub diagnosis: the Tcl rate program has wrong rates"


def _tcl_candidate(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "checkout.sh").write_text(CHECKOUT_SH)
    (cand / "rates.tcl").write_text(RATES_TCL)
    (cand / "checkout_test.sh").write_text(
        "#!/usr/bin/env bash\n"
        "source \"$(dirname \"${BASH_SOURCE[0]}\")/checkout.sh\"\n"
        "fail=0\n"
        "check() {\n"
        "  if [[ \"$2\" != \"$1\" ]]; then\n"
        "    echo \"FAIL $3: expected $1, got $2\"\n"
        "    fail=1\n"
        "  else\n"
        "    echo \"ok $3\"\n"
        "  fi\n"
        "}\n"
        "check \"200.00\" \"$(total 250)\" bulk\n"
        "check \"108.00\" \"$(total 120)\" over100\n"
        "check \"76.00\" \"$(total 80)\" over50\n"
        "check \"30.00\" \"$(total 30)\" small\n"
        "exit \"$fail\"\n"
    )
    (cand / "repro.sh").write_text(
        "#!/usr/bin/env bash\n"
        "source \"$(dirname \"${BASH_SOURCE[0]}\")/checkout.sh\"\n"
        "if [[ \"$(total 120)\" != \"108.00\""
        " || \"$(total 80)\" != \"76.00\" ]]; then\n"
        "  echo \"discount rates are wrong\"\n"
        "  exit 1\n"
        "fi\n"
        "echo \"repro ok\"\n"
    )
    return cand


@pytest.mark.skipif(shutil.which("expect") is None
                    or shutil.which("bash") is None,
                    reason="expect or bash is not available")
def test_tcl_repair_end_to_end_from_snapshot(tmp_path):
    cand = _tcl_candidate(tmp_path)
    spec = parse_spec({
        "name": "shipping-discount-tcl-fix",
        "brief": "Fix rates.tcl so every rate matches the published rules.",
        "checks": [
            {"name": "unit_tests", "command": "bash checkout_test.sh",
             "timeout_s": 120},
            {"name": "repro", "command": "bash repro.sh",
             "timeout_s": 120},
        ],
    })
    stub = _SnapshotTclStub()
    report = run_repair(
        spec=spec, candidate_dir=cand, mode="judge",
        llm=stub, references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label=str(cand), max_rounds=3,
    )
    assert report["final_verdict"] == "PASS"
    assert report["stopped_reason"] == "passed"
    assert report["rounds_used"] == 1
    # The stub was actually handed the Tcl file in its snapshot.
    assert "rates.tcl" in stub.seen_snapshots[0]
    for a in report["attempts"]:
        assert verify_chain(a["evidence"]) is True
    # The original candidate on disk is never modified.
    assert (cand / "rates.tcl").read_text() == RATES_TCL
