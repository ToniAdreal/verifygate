"""Context-only hunk rejection (v1.139.0).

A hunk whose body carries no '-'/'+' change line -- every line
context -- satisfies its declared counts and used to apply as a
no-op that still reported the file as changed: the patched tree
stayed byte-identical and the repair loop burned its rounds
re-verifying an unchanged candidate. The live case is a file
whose own lines begin with '-' (an Erlang ``-define`` header):
both live models space-prefixed every line instead of doubling
the marker. These tests pin the parse-time rejection, the
unchanged atomicity, and the repair loop's feedback path: a
garbled proposal is fed back and a correctly marked diff then
repairs FAIL -> PASS.
"""

import pytest

from verifygate.evidence import verify_chain
from verifygate.llm import LLMAdapter
from verifygate.patch import PatchError, apply_patch, parse_patch
from verifygate.repair import run_repair
from verifygate.spec import parse_spec
from verifygate.tavily import MockReferenceLookup

GARBLED = (
    "--- a/rates.txt\n"
    "+++ b/rates.txt\n"
    "@@ -1,2 +1,2 @@\n"
    " alpha\n"
    " beta\n"
)


def test_context_only_hunk_is_rejected_at_parse_time():
    with pytest.raises(PatchError, match="no change lines"):
        parse_patch(GARBLED)


def test_context_only_hunk_leaves_the_tree_untouched(tmp_path):
    (tmp_path / "rates.txt").write_text("alpha\nbeta\n")
    with pytest.raises(PatchError, match="no change lines"):
        apply_patch(tmp_path, GARBLED)
    assert (tmp_path / "rates.txt").read_text() == "alpha\nbeta\n"


def test_bare_context_only_hunk_is_rejected_at_parse_time():
    bare = (
        "--- a/rates.txt\n"
        "+++ b/rates.txt\n"
        "@@\n"
        " alpha\n"
        " beta\n"
    )
    with pytest.raises(PatchError, match="no change lines"):
        parse_patch(bare)


class _GarbledThenFixedStub(LLMAdapter):
    """First proposal is the live garble; the second is marked right."""

    name = "garbled-then-fixed-stub"

    def __init__(self):
        self.calls = 0
        self.feedback = []

    def generate_patch(self, brief, failing_evidence=None):
        self.calls += 1
        err = (failing_evidence or {}).get("patch_error")
        if err:
            self.feedback.append(err)
        if self.calls == 1:
            return (
                "--- a/data.txt\n"
                "+++ b/data.txt\n"
                "@@ -1,1 +1,1 @@\n"
                " -define(RATE, 0.03).\n"
            )
        return (
            "--- a/data.txt\n"
            "+++ b/data.txt\n"
            "@@ -1,1 +1,1 @@\n"
            "--define(RATE, 0.03).\n"
            "+-define(RATE, 0.10).\n"
        )

    def explain_failure(self, check_results):
        return "stub diagnosis"


def test_garbled_proposal_is_fed_back_and_repair_recovers(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "data.txt").write_text("-define(RATE, 0.03).\n")
    (cand / "check.py").write_text(
        "from pathlib import Path\n"
        "\n"
        "if '0.10' not in Path('data.txt').read_text():\n"
        "    raise SystemExit(1)\n"
        "print('ok')\n"
    )
    spec = parse_spec({
        "name": "context-only-feedback-fix",
        "brief": "Fix the rate in data.txt.",
        "checks": [
            {"name": "unit_tests", "command": "python3 check.py",
             "timeout_s": 120},
        ],
    })
    stub = _GarbledThenFixedStub()
    report = run_repair(
        spec=spec, candidate_dir=cand, mode="judge",
        llm=stub, references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label=str(cand), max_rounds=3,
    )
    assert report["final_verdict"] == "PASS"
    assert report["rounds_used"] == 2
    assert stub.feedback and "no change lines" in stub.feedback[0]
    for a in report["attempts"]:
        assert verify_chain(a["evidence"]) is True
    assert (cand / "data.txt").read_text() == "-define(RATE, 0.03).\n"
