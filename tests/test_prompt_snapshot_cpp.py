"""Prompt-snapshot C++ variant coverage (v1.117.0).

The repair loop's patch prompt carries a snapshot of the candidate's
small text files (``repair._candidate_files``). C++ variants were
invisible to it: ``.cpp`` / ``.h`` are whitelisted but the snapshot
matches the full suffix, so ``rates.hpp`` (and its ``.cc`` / ``.cxx``
/ ``.hxx`` siblings) matched nothing and the file to be patched never
reached the prompt. On the first live candidate whose fix landed in
a .hpp header the real model named the right file from the brief,
invented its contents, and had every diff correctly rejected
(``patch_not_applicable``, 5 real calls / 26,282 tokens). These
tests pin the inclusion, the unchanged whitelist posture for
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

RATES_HPP = (
    "#pragma once\n"
    "// Published discount rules: >= 200 -> 20% off, >= 100 -> 10% off,\n"
    "// >= 50 -> 5% off, otherwise no discount.\n"
    "inline double discount_rate(double subtotal) {\n"
    "    if (subtotal >= 200) {\n"
    "        return 0.20;\n"
    "    } else if (subtotal >= 100) {\n"
    "        return 0.01;\n"
    "    } else if (subtotal >= 50) {\n"
    "        return 0.15;\n"
    "    } else {\n"
    "        return 0.0;\n"
    "    }\n"
    "}\n"
)

EXTRA_CC = "// cc helper: prints the shop's greeting.\n"
EXTRA_CXX = "// cxx helper: prints the shop's farewell.\n"
EXTRA_HXX = "#pragma once\n// hxx helper header.\n"

CHECKOUT_CPP = (
    "#include <cstdio>\n"
    "#include <cstdlib>\n"
    "#include \"rates.hpp\"\n"
    "\n"
    "int main(int argc, char** argv) {\n"
    "    if (argc < 2) {\n"
    "        return 2;\n"
    "    }\n"
    "    double subtotal = std::atof(argv[1]);\n"
    "    double rate = discount_rate(subtotal);\n"
    "    std::printf(\"%.2f\\n\", subtotal * (1.0 - rate));\n"
    "    return 0;\n"
    "}\n"
)


def test_cpp_variants_are_snapshot_suffixes():
    for suffix in (".hpp", ".cc", ".cxx", ".hxx"):
        assert suffix in _TEXT_SUFFIXES


def test_snapshot_includes_cpp_variant_files_verbatim(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "rates.hpp").write_text(RATES_HPP)
    (cand / "extra.cc").write_text(EXTRA_CC)
    (cand / "extra.cxx").write_text(EXTRA_CXX)
    (cand / "extra.hxx").write_text(EXTRA_HXX)
    (cand / "checkout.cpp").write_text(CHECKOUT_CPP)
    files = _candidate_files(cand)
    assert files["rates.hpp"] == RATES_HPP
    assert files["extra.cc"] == EXTRA_CC
    assert files["extra.cxx"] == EXTRA_CXX
    assert files["extra.hxx"] == EXTRA_HXX
    assert files["checkout.cpp"] == CHECKOUT_CPP


def test_snapshot_still_skips_unlisted_and_binary_files(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "blob.bin").write_text("plain text, unlisted suffix\n")
    (cand / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff\xfe")
    (cand / "notes").write_text("extensionless, unlisted basename\n")
    (cand / "ok.py").write_text("x = 1\n")
    files = _candidate_files(cand)
    assert files == {"ok.py": "x = 1\n"}


class _SnapshotCppStub(LLMAdapter):
    """Builds its diff from the snapshot, exactly like a real model must.

    If ``rates.hpp`` is absent from the candidate_files snapshot the
    stub has nothing truthful to diff against and returns no diff --
    the pre-v1.117.0 live failure shape.
    """

    name = "snapshot-cpp-stub"

    def __init__(self):
        self.seen_snapshots = []

    def generate_patch(self, brief, failing_evidence=None):
        files = (failing_evidence or {}).get("candidate_files") or {}
        self.seen_snapshots.append(sorted(files))
        text = files.get("rates.hpp")
        if text is None:
            return "Plan only: the hpp file was not shown to me."
        lines = text.splitlines()
        out = ["--- a/rates.hpp", "+++ b/rates.hpp"]
        for old, new in (
            ("        return 0.01;", "        return 0.10;"),
            ("        return 0.15;", "        return 0.05;"),
        ):
            idx = next(i for i, ln in enumerate(lines) if ln == old)
            out.append(f"@@ -{idx + 1},1 +{idx + 1},1 @@")
            out.append(f"-{old}")
            out.append(f"+{new}")
        return "\n".join(out) + "\n"

    def explain_failure(self, check_results):
        return "stub diagnosis: the hpp rate card has wrong rates"


def _cpp_candidate(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "checkout.cpp").write_text(CHECKOUT_CPP)
    (cand / "rates.hpp").write_text(RATES_HPP)
    (cand / "checkout_test.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -u\n"
        "cd \"$(dirname \"${BASH_SOURCE[0]}\")\"\n"
        "g++ -std=c++17 -O0 -o checkout_bin checkout.cpp || exit 1\n"
        "fail=0\n"
        "check() {\n"
        "  got=\"$(./checkout_bin \"$3\")\"\n"
        "  if [[ \"$got\" != \"$1\" ]]; then\n"
        "    echo \"FAIL $2: expected $1, got $got\"\n"
        "    fail=1\n"
        "  else\n"
        "    echo \"ok $2\"\n"
        "  fi\n"
        "}\n"
        "check \"200.00\" bulk 250\n"
        "check \"108.00\" over100 120\n"
        "check \"76.00\" over50 80\n"
        "check \"30.00\" small 30\n"
        "exit \"$fail\"\n"
    )
    (cand / "repro.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -u\n"
        "cd \"$(dirname \"${BASH_SOURCE[0]}\")\"\n"
        "g++ -std=c++17 -O0 -o repro_bin checkout.cpp || exit 1\n"
        "if [[ \"$(./repro_bin 120)\" != \"108.00\" "
        "|| \"$(./repro_bin 80)\" != \"76.00\" ]]; then\n"
        "  echo \"discount rates are wrong\"\n"
        "  exit 1\n"
        "fi\n"
        "echo \"repro ok\"\n"
    )
    return cand


@pytest.mark.skipif(shutil.which("g++") is None,
                    reason="g++ is not available")
def test_cpp_repair_end_to_end_from_snapshot(tmp_path):
    cand = _cpp_candidate(tmp_path)
    spec = parse_spec({
        "name": "shipping-discount-cpp-fix",
        "brief": "Fix rates.hpp so every rate matches the published rules.",
        "checks": [
            {"name": "unit_tests", "command": "bash checkout_test.sh",
             "timeout_s": 120},
            {"name": "repro", "command": "bash repro.sh",
             "timeout_s": 120},
        ],
    })
    stub = _SnapshotCppStub()
    report = run_repair(
        spec=spec, candidate_dir=cand, mode="judge",
        llm=stub, references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label=str(cand), max_rounds=3,
    )
    assert report["final_verdict"] == "PASS"
    assert report["stopped_reason"] == "passed"
    assert report["rounds_used"] == 1
    # The stub was actually handed the hpp file in its snapshot.
    assert "rates.hpp" in stub.seen_snapshots[0]
    for a in report["attempts"]:
        assert verify_chain(a["evidence"]) is True
    # The original candidate on disk is never modified.
    assert (cand / "rates.hpp").read_text() == RATES_HPP
