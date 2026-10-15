"""Prompt-snapshot .bash coverage (v1.116.0).

The repair loop's patch prompt carries a snapshot of the candidate's
small text files (``repair._candidate_files``). Shell variants were
invisible to it: ``.sh`` is whitelisted but the snapshot matches the
full suffix, so ``rates.bash`` (and its ``.zsh`` sibling) matched
nothing and the file to be patched never reached the prompt. On the
first live candidate whose fix landed in a .bash script the real
model named the right file from the brief, invented its contents,
and had every diff correctly rejected (``patch_not_applicable``,
4 real calls / 19,344 tokens). These tests pin the inclusion, the
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

RATES_BASH = (
    "# Published discount rules: >= 200 -> 20% off, >= 100 -> 10% off,\n"
    "# >= 50 -> 5% off, otherwise no discount.\n"
    "discount_rate() {\n"
    "  local subtotal=\"$1\"\n"
    "  if (( subtotal >= 200 )); then\n"
    "    echo \"0.20\"\n"
    "  elif (( subtotal >= 100 )); then\n"
    "    echo \"0.01\"\n"
    "  elif (( subtotal >= 50 )); then\n"
    "    echo \"0.15\"\n"
    "  else\n"
    "    echo \"0\"\n"
    "  fi\n"
    "}\n"
)

EXTRA_ZSH = (
    "# zsh helper: prints the shop's greeting.\n"
    "greeting() { echo \"welcome\"; }\n"
)

CHECKOUT_SH = (
    "#!/usr/bin/env bash\n"
    "source \"$(dirname \"${BASH_SOURCE[0]}\")/rates.bash\"\n"
    "\n"
    "total() {\n"
    "  local subtotal=\"$1\"\n"
    "  local rate\n"
    "  rate=\"$(discount_rate \"$subtotal\")\"\n"
    "  awk -v s=\"$subtotal\" -v r=\"$rate\" "
    "'BEGIN { printf \"%.2f\", s * (1 - r) }'\n"
    "}\n"
    "\n"
    "if [[ \"${BASH_SOURCE[0]}\" == \"${0}\" ]]; then\n"
    "  total \"$1\"\n"
    "  echo\n"
    "fi\n"
)


def test_shell_variants_are_snapshot_suffixes():
    for suffix in (".bash", ".zsh"):
        assert suffix in _TEXT_SUFFIXES


def test_snapshot_includes_shell_variant_files_verbatim(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "rates.bash").write_text(RATES_BASH)
    (cand / "extra.zsh").write_text(EXTRA_ZSH)
    (cand / "checkout.sh").write_text(CHECKOUT_SH)
    files = _candidate_files(cand)
    assert files["rates.bash"] == RATES_BASH
    assert files["extra.zsh"] == EXTRA_ZSH
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


class _SnapshotBashStub(LLMAdapter):
    """Builds its diff from the snapshot, exactly like a real model must.

    If ``rates.bash`` is absent from the candidate_files snapshot the
    stub has nothing truthful to diff against and returns no diff --
    the pre-v1.116.0 live failure shape.
    """

    name = "snapshot-bash-stub"

    def __init__(self):
        self.seen_snapshots = []

    def generate_patch(self, brief, failing_evidence=None):
        files = (failing_evidence or {}).get("candidate_files") or {}
        self.seen_snapshots.append(sorted(files))
        text = files.get("rates.bash")
        if text is None:
            return "Plan only: the bash file was not shown to me."
        lines = text.splitlines()
        out = ["--- a/rates.bash", "+++ b/rates.bash"]
        for old, new in (
            ("    echo \"0.01\"", "    echo \"0.10\""),
            ("    echo \"0.15\"", "    echo \"0.05\""),
        ):
            idx = next(i for i, ln in enumerate(lines) if ln == old)
            out.append(f"@@ -{idx + 1},1 +{idx + 1},1 @@")
            out.append(f"-{old}")
            out.append(f"+{new}")
        return "\n".join(out) + "\n"

    def explain_failure(self, check_results):
        return "stub diagnosis: the bash rate card has wrong rates"


def _bash_candidate(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "checkout.sh").write_text(CHECKOUT_SH)
    (cand / "rates.bash").write_text(RATES_BASH)
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
        "if [[ \"$(total 120)\" != \"108.00\" "
        "|| \"$(total 80)\" != \"76.00\" ]]; then\n"
        "  echo \"discount rates are wrong\"\n"
        "  exit 1\n"
        "fi\n"
        "echo \"repro ok\"\n"
    )
    return cand


@pytest.mark.skipif(shutil.which("bash") is None,
                    reason="bash is not available")
def test_bash_repair_end_to_end_from_snapshot(tmp_path):
    cand = _bash_candidate(tmp_path)
    spec = parse_spec({
        "name": "shipping-discount-bash-fix",
        "brief": "Fix rates.bash so every rate matches the published rules.",
        "checks": [
            {"name": "unit_tests", "command": "bash checkout_test.sh",
             "timeout_s": 60},
            {"name": "repro", "command": "bash repro.sh",
             "timeout_s": 60},
        ],
    })
    stub = _SnapshotBashStub()
    report = run_repair(
        spec=spec, candidate_dir=cand, mode="judge",
        llm=stub, references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label=str(cand), max_rounds=3,
    )
    assert report["final_verdict"] == "PASS"
    assert report["stopped_reason"] == "passed"
    assert report["rounds_used"] == 1
    # The stub was actually handed the bash file in its snapshot.
    assert "rates.bash" in stub.seen_snapshots[0]
    for a in report["attempts"]:
        assert verify_chain(a["evidence"]) is True
    # The original candidate on disk is never modified.
    assert (cand / "rates.bash").read_text() == RATES_BASH
