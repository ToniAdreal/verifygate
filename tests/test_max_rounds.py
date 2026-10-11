"""Round-budget validation: max_rounds is a bounded, real int.

Before v1.22.0 the repair loop's only budget check was
``max_rounds < 0``. A bool budget (``True``) or a float (``2.5``)
produced a report whose stated ``max_rounds`` fails verify's own
non-bool-int narrative rule — the loop emitted reports that fail
their own verification, the v1.21.0 bug class one field over — a
float budget also silently allowed more rounds than stated
(2.5 -> 3), a string or ``None`` crashed the comparison with an
unhandled ``TypeError`` instead of the documented ``ValueError``,
and no upper bound existed at all: ``--max-rounds 1000000000`` was
accepted verbatim, each round a paid model call plus a full
re-verification. The budget is now validated like spec timeouts
have been since v1.7.0, and ``verify``'s narrative check mirrors
the bound.
"""

import json
from pathlib import Path

import pytest

from verifygate import cli
from verifygate.evidence import verify_chain
from verifygate.llm import LLMAdapter
from verifygate.mock_llm import MockLLMAdapter
from verifygate.repair import MAX_ROUNDS, run_repair
from verifygate.spec import load_spec, parse_spec
from verifygate.tavily import MockReferenceLookup
from verifygate.verify import document_ok, narrative_problems, verify_document
from verifygate.webview import render_document

APP = Path(__file__).resolve().parent.parent
SPEC = load_spec(APP / "samples" / "spec.json")
FAILING = APP / "samples" / "failing"
PASSING = APP / "samples" / "passing"


class _CountingStub(LLMAdapter):
    """Applicable-but-never-fixing patches; counts generation calls."""

    name = "counting-stub"

    def __init__(self):
        self.calls = 0

    def generate_patch(self, brief, failing_evidence=None):
        self.calls += 1
        return (
            "--- /dev/null\n"
            f"+++ b/note{self.calls}.txt\n"
            "@@ -0,0 +1,1 @@\n"
            "+still broken\n"
        )

    def explain_failure(self, check_results):
        return "stub diagnosis"


def _always_fails(tmp_path):
    candidate = tmp_path / "cand"
    candidate.mkdir()
    (candidate / "check.py").write_text("raise SystemExit(1)\n")
    spec = parse_spec({
        "name": "always-fails",
        "brief": "Make the check pass somehow.",
        "checks": [{"name": "c", "command": "python3 check.py",
                    "timeout_s": 10}],
    })
    return spec, candidate


def _repair(candidate, max_rounds, llm=None, spec=SPEC):
    return run_repair(
        spec=spec, candidate_dir=candidate, mode="judge",
        llm=llm or MockLLMAdapter(), references=MockReferenceLookup(),
        spec_label="samples/spec.json", candidate_label=str(candidate),
        max_rounds=max_rounds,
    )


@pytest.mark.parametrize("bad", [
    True, False,          # bool is an int subclass, not a budget
    2.5, 0.5,             # floats silently overrun the stated budget
    "3", None,            # crashed the old `< 0` check with TypeError
    -1, MAX_ROUNDS + 1, 10 ** 9,
])
def test_library_rejects_malformed_or_absurd_budgets(tmp_path, bad):
    spec, candidate = _always_fails(tmp_path)
    stub = _CountingStub()
    with pytest.raises(ValueError):
        _repair(candidate, bad, llm=stub, spec=spec)
    # Rejected up front: no verification run, no generation call.
    assert stub.calls == 0


def test_float_budget_no_longer_overruns(tmp_path):
    # Pre-fix, 2.5 ran 3 rounds with this stub. It is now a ValueError
    # before the first generation call.
    spec, candidate = _always_fails(tmp_path)
    stub = _CountingStub()
    with pytest.raises(ValueError):
        _repair(candidate, 2.5, llm=stub, spec=spec)
    assert stub.calls == 0


def test_boundary_budgets_are_accepted_and_verify():
    report = _repair(FAILING, 0)
    assert report["rounds_used"] == 0
    assert report["final_verdict"] == "FAIL"
    assert document_ok(verify_document(report))

    report = _repair(PASSING, MAX_ROUNDS)
    assert report["max_rounds"] == MAX_ROUNDS
    assert report["final_verdict"] == "PASS"
    assert document_ok(verify_document(report))

    report = _repair(FAILING, MAX_ROUNDS)
    assert report["final_verdict"] == "PASS"  # mock fixes it in 1 round
    assert report["rounds_used"] == 1
    assert document_ok(verify_document(report))
    for a in report["attempts"]:
        assert verify_chain(a["evidence"])


def test_max_rounds_constant_is_the_documented_ceiling():
    assert MAX_ROUNDS == 10


def test_cli_rejects_budget_above_maximum(tmp_path):
    out = tmp_path / "report.json"
    rc = cli.main([
        "repair", "--spec", str(APP / "samples" / "spec.json"),
        "--candidate", str(PASSING), "--mode", "judge",
        "--max-rounds", str(MAX_ROUNDS + 1), "--out", str(out),
    ])
    assert rc == 2
    assert not out.exists()  # a usage error writes no report


def test_cli_rejects_negative_budget(tmp_path):
    out = tmp_path / "report.json"
    rc = cli.main([
        "repair", "--spec", str(APP / "samples" / "spec.json"),
        "--candidate", str(PASSING), "--mode", "judge",
        "--max-rounds", "-1", "--out", str(out),
    ])
    assert rc == 2
    assert not out.exists()


def test_cli_accepts_budget_at_maximum(tmp_path):
    out = tmp_path / "report.json"
    rc = cli.main([
        "repair", "--spec", str(APP / "samples" / "spec.json"),
        "--candidate", str(PASSING), "--mode", "judge",
        "--max-rounds", str(MAX_ROUNDS), "--out", str(out),
    ])
    assert rc == 0
    assert json.loads(out.read_text())["max_rounds"] == MAX_ROUNDS


def _genuine_report():
    return _repair(FAILING, 3)


def test_verify_rejects_budget_above_loop_maximum():
    report = _genuine_report()
    assert document_ok(verify_document(report))
    report["max_rounds"] = 10 ** 9
    assert any("max_rounds" in p for p in narrative_problems(report))
    assert not document_ok(verify_document(report))


def test_verify_accepts_budget_at_loop_maximum():
    report = _genuine_report()
    report["max_rounds"] = MAX_ROUNDS
    assert narrative_problems(report) == []
    assert document_ok(verify_document(report))


def test_verify_cli_exits_1_on_inflated_budget(tmp_path):
    report = _genuine_report()
    report["max_rounds"] = 10 ** 9
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    assert cli.main(["verify", "--record", str(path)]) == 1


def test_webview_flags_inflated_budget():
    report = _genuine_report()
    report["max_rounds"] = 10 ** 9
    html = render_document(report)
    assert "SUMMARY MISMATCH" in html
    intact = render_document(_genuine_report())
    assert "SUMMARY MISMATCH" not in intact
