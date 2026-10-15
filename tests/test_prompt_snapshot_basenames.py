"""Prompt-snapshot extensionless-basename coverage (v1.110.0).

The repair loop's patch prompt carries a snapshot of the candidate's
small text files (``repair._candidate_files``). Inclusion was decided
by suffix alone, so extensionless text files -- the build/ops files a
fix most often lands in -- were silently omitted: on the first live
Makefile candidate the model named the right file from the brief but
had never seen its contents, and every proposed hunk missed the file
on disk (``patch_not_applicable``, 4 real calls / 12,411 tokens).
These tests pin the basename inclusion, the unchanged posture for
unlisted extensionless and binary files, and an end-to-end repair
whose stub adapter can only build its diff from the snapshot it is
handed.
"""

import pytest

from verifygate.evidence import verify_chain
from verifygate.llm import LLMAdapter
from verifygate.repair import _TEXT_BASENAMES, _candidate_files, run_repair
from verifygate.spec import parse_spec
from verifygate.tavily import MockReferenceLookup

MAKEFILE = (
    "CC = gcc\n"
    "CFLAGS = -std=c11 -Wall -Wextra -DSCALE=10\n"
    "\n"
    "sum: sum.c\n"
    "\t$(CC) $(CFLAGS) -o sum sum.c\n"
)

SUM_C = (
    "#include <stdio.h>\n"
    "#include <stdlib.h>\n"
    "\n"
    "#ifndef SCALE\n"
    "#define SCALE 1\n"
    "#endif\n"
    "\n"
    "int main(int argc, char **argv) {\n"
    "    long total = 0;\n"
    "    for (int i = 1; i < argc; i++) {\n"
    "        total += atol(argv[i]);\n"
    "    }\n"
    "    printf(\"%ld\\n\", total * SCALE);\n"
    "    return 0;\n"
    "}\n"
)


@pytest.mark.parametrize("name", ["Makefile", "Dockerfile", "Rakefile",
                                  "Gemfile", "Jenkinsfile", "Vagrantfile",
                                  "Procfile"])
def test_extensionless_build_files_are_snapshot_basenames(name):
    assert name in _TEXT_BASENAMES


def test_snapshot_includes_makefile_verbatim(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "Makefile").write_text(MAKEFILE)
    (cand / "sum.c").write_text(SUM_C)
    files = _candidate_files(cand)
    assert files["Makefile"] == MAKEFILE
    assert files["sum.c"] == SUM_C


def test_snapshot_includes_dockerfile_verbatim(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "Dockerfile").write_text("FROM python:3.12\nRUN pip install bad==0\n")
    files = _candidate_files(cand)
    assert files["Dockerfile"] == "FROM python:3.12\nRUN pip install bad==0\n"


def test_snapshot_still_skips_unlisted_extensionless_and_binary(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "LICENSE").write_text("some license text\n")
    (cand / "NOTES").write_text("plain text, no listed basename\n")
    (cand / "blob").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff\xfe")
    (cand / "ok.py").write_text("x = 1\n")
    files = _candidate_files(cand)
    assert files == {"ok.py": "x = 1\n"}


class _SnapshotMakefileStub(LLMAdapter):
    """Builds its diff from the snapshot, exactly like a real model must.

    If ``Makefile`` is absent from the candidate_files snapshot the
    stub has nothing truthful to diff against and returns no diff --
    the pre-v1.110.0 live failure shape.
    """

    name = "snapshot-makefile-stub"

    def __init__(self):
        self.seen_snapshots = []

    def generate_patch(self, brief, failing_evidence=None):
        files = (failing_evidence or {}).get("candidate_files") or {}
        self.seen_snapshots.append(sorted(files))
        text = files.get("Makefile")
        if text is None:
            return "Plan only: the Makefile was not shown to me."
        lines = text.splitlines()
        idx = next(i for i, ln in enumerate(lines) if "-DSCALE=10" in ln)
        fixed = lines[idx].replace(" -DSCALE=10", "")
        return (
            "--- a/Makefile\n"
            "+++ b/Makefile\n"
            f"@@ -{idx + 1},1 +{idx + 1},1 @@\n"
            f"-{lines[idx]}\n"
            f"+{fixed}\n"
        )

    def explain_failure(self, check_results):
        return "stub diagnosis: the build injects -DSCALE=10"


def _makefile_candidate(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "Makefile").write_text(MAKEFILE)
    (cand / "sum.c").write_text(SUM_C)
    (cand / "test_sum.sh").write_text(
        "#!/bin/bash\n"
        "set -e\n"
        "make -s sum\n"
        "out=$(./sum 2 3 4)\n"
        "[ \"$out\" = \"9\" ] || { echo \"FAIL: $out\"; exit 1; }\n"
        "echo ok\n"
    )
    return cand


def test_makefile_repair_end_to_end_from_snapshot(tmp_path):
    cand = _makefile_candidate(tmp_path)
    spec = parse_spec({
        "name": "makefile-scale-fix",
        "brief": "Remove the -DSCALE=10 injection from the Makefile.",
        "checks": [{"name": "makefile_tests", "command": "bash test_sum.sh",
                    "timeout_s": 60}],
    })
    stub = _SnapshotMakefileStub()
    report = run_repair(
        spec=spec, candidate_dir=cand, mode="judge",
        llm=stub, references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label=str(cand), max_rounds=3,
    )
    assert report["final_verdict"] == "PASS"
    assert report["stopped_reason"] == "passed"
    assert report["rounds_used"] == 1
    # The stub was actually handed the Makefile in its snapshot.
    assert "Makefile" in stub.seen_snapshots[0]
    for a in report["attempts"]:
        assert verify_chain(a["evidence"]) is True
    # The original candidate on disk is never modified.
    assert (cand / "Makefile").read_text() == MAKEFILE
