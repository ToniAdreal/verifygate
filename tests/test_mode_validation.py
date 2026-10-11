"""Mode validation: the run mode is 'judge' or 'real', everywhere.

Before v1.38.0 the mode was validated only by the CLI's argparse
``choices``. Every library entry point accepted any value verbatim:
the runner sealed it into the record header and interpolated it raw
into the record's ``replay`` string, so mode ``bogus`` — or
``judge; echo pwned``, a shell injection into the command a reader
is told to copy and run — produced a fully-run PASS whose record
failed its own verification the moment it was written (the
structure check requires a real header mode and a replay template
ending in one). The repair loop delivered a whole report of such
records, replay ran under any mode it was handed, and the CLI's
adapter factory silently treated any non-``judge`` value as real
mode — including the replay path's mode, which comes from a
record's own untrusted header — so a bogus header mode could select
real, paid Token Factory adapters instead of a usage error. Every
entry point now validates through ``evidence.validate_mode``
before any work happens: an invalid mode is a ``ValueError``
(CLI exit 2), never a run. ``build_record`` stays a permissive
assembler, as in earlier rounds: its structure mirror is what
judges a record sealed over an invalid mode, however produced.
"""

import json
from pathlib import Path

import pytest

from verifygate import cli
from verifygate.evidence import MODES, build_record, validate_mode, verify_chain
from verifygate.llm import LLMAdapter
from verifygate.mock_llm import MockLLMAdapter
from verifygate.repair import run_repair
from verifygate.replay import replay_document, replay_ok
from verifygate.runner import run_verification
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup
from verifygate.verify import document_ok, verify_document

APP = Path(__file__).resolve().parent.parent
SPEC = load_spec(APP / "samples" / "spec.json")
FAILING = APP / "samples" / "failing"
PASSING = APP / "samples" / "passing"

BAD_MODES = [
    "bogus", "REAL", "Judge", "judge ", " judge", "", "judge; echo pwned",
    "judge && rm -rf /", "real\n", None, 123, True, ["judge"], "x" * 500,
]

CHECK = {
    "name": "unit_tests", "command": "python3 -m pytest -q",
    "exit_code": 0, "duration_ms": 42, "stdout_tail": "4 passed",
    "stderr_tail": "", "timed_out": False, "verdict": "PASS",
}


class _CountingStub(LLMAdapter):
    name = "counting-stub"

    def __init__(self):
        self.explain_calls = 0
        self.patch_calls = 0

    def generate_patch(self, brief, failing_evidence=None):
        self.patch_calls += 1
        return ""

    def explain_failure(self, check_results):
        self.explain_calls += 1
        return "stub diagnosis"


def _run(candidate, mode, llm=None):
    return run_verification(
        spec=SPEC, candidate_dir=candidate, mode=mode,
        llm=llm or MockLLMAdapter(), references=MockReferenceLookup(),
        spec_label="samples/spec.json", candidate_label=str(candidate))


def _repair(candidate, mode, llm=None):
    return run_repair(
        spec=SPEC, candidate_dir=candidate, mode=mode,
        llm=llm or MockLLMAdapter(), references=MockReferenceLookup(),
        spec_label="samples/spec.json", candidate_label=str(candidate),
        max_rounds=3)


# -- the helper itself ---------------------------------------------------


def test_modes_constant_is_the_two_run_modes():
    assert MODES == ("judge", "real")


@pytest.mark.parametrize("mode", ["judge", "real"])
def test_validate_mode_returns_valid_modes(mode):
    assert validate_mode(mode) == mode


@pytest.mark.parametrize("bad", BAD_MODES)
def test_validate_mode_rejects_invalid_modes(bad):
    with pytest.raises(ValueError):
        validate_mode(bad)


def test_validate_mode_error_message_is_bounded():
    with pytest.raises(ValueError) as excinfo:
        validate_mode("x" * 500_000)
    assert len(str(excinfo.value)) < 200


# -- the runner ------------------------------------------------------------


@pytest.mark.parametrize("bad", BAD_MODES)
def test_runner_rejects_invalid_mode_before_any_work(bad):
    stub = _CountingStub()
    with pytest.raises(ValueError):
        _run(FAILING, bad, llm=stub)
    # Rejected up front: no check ran, so no diagnosis was requested
    # (the failing candidate would have asked for one).
    assert stub.explain_calls == 0


@pytest.mark.parametrize("mode", ["judge", "real"])
def test_runner_accepts_valid_modes_and_records_verify(mode):
    record = _run(PASSING, mode)
    assert record["verdict"] == "PASS"
    header = record["entries"][0]
    assert header["mode"] == mode
    assert record["replay"].endswith(f"--mode {mode}")
    assert verify_chain(record)


def test_runner_shell_fragment_mode_never_reaches_a_replay_string():
    with pytest.raises(ValueError):
        _run(PASSING, "judge; echo pwned")


# -- the repair loop -------------------------------------------------------


@pytest.mark.parametrize("bad", BAD_MODES)
def test_repair_rejects_invalid_mode_before_any_work(bad):
    stub = _CountingStub()
    with pytest.raises(ValueError):
        _repair(FAILING, bad, llm=stub)
    assert stub.explain_calls == 0
    assert stub.patch_calls == 0


def test_repair_valid_mode_still_repairs_and_verifies():
    report = _repair(FAILING, "judge")
    assert report["mode"] == "judge"
    assert report["final_verdict"] == "PASS"
    assert document_ok(verify_document(report))


# -- replay ------------------------------------------------------------------


def _genuine_report():
    return _repair(FAILING, "judge")


@pytest.mark.parametrize("bad", BAD_MODES)
def test_replay_rejects_invalid_mode(bad):
    with pytest.raises(ValueError):
        replay_document(
            _genuine_report(), spec=SPEC, candidate_dir=FAILING, mode=bad,
            llm=MockLLMAdapter(), references=MockReferenceLookup(),
            spec_label="samples/spec.json")


def test_replay_valid_modes_reproduce_a_record():
    record = _run(FAILING, "judge")
    for mode in ("judge", "real"):
        results = replay_document(
            record, spec=SPEC, candidate_dir=FAILING, mode=mode,
            llm=MockLLMAdapter(), references=MockReferenceLookup(),
            spec_label="samples/spec.json")
        assert replay_ok(results)


# -- the CLI adapter factory -------------------------------------------------


@pytest.mark.parametrize("bad", BAD_MODES)
def test_adapters_rejects_invalid_mode_instead_of_selecting_real(bad):
    with pytest.raises(ValueError):
        cli._adapters(bad)


def test_adapters_judge_selects_the_mocks():
    llm, references = cli._adapters("judge")
    assert isinstance(llm, MockLLMAdapter)
    assert isinstance(references, MockReferenceLookup)


def test_cli_argparse_rejects_invalid_mode_with_exit_2():
    with pytest.raises(SystemExit) as excinfo:
        cli.main([
            "run", "--spec", str(APP / "samples" / "spec.json"),
            "--candidate", str(PASSING), "--mode", "bogus",
        ])
    assert excinfo.value.code == 2


def test_cli_replay_with_bogus_header_mode_is_a_usage_error(tmp_path):
    # A record sealed (via the permissive assembler) under a bogus
    # header mode: replay derives its mode from that untrusted
    # header. Pre-fix the adapter factory treated it as real mode;
    # now it is a one-line usage error, exit 2, and no adapter is
    # ever constructed for a paid call.
    record = build_record(
        spec_name=SPEC.name, spec_hash=SPEC.hash,
        candidate_tree_hash="b" * 64, mode="banana",
        check_results=[CHECK], verdict="PASS", diagnosis=None,
        replay_command=(
            "python3 -m verifygate run --spec s --candidate c "
            "--mode judge"))
    assert verify_chain(record) is False  # the mirror already judges it
    path = tmp_path / "bogus-mode.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    rc = cli.main([
        "replay", "--record", str(path),
        "--spec", str(APP / "samples" / "spec.json"),
        "--candidate", str(PASSING),
    ])
    assert rc == 2
