"""Spec-object validation: a constructed TaskSpec obeys parse_spec.

Before v1.39.0 every spec rule was enforced only by ``parse_spec``,
i.e. only on the JSON path. ``TaskSpec``/``Check`` are plain
dataclasses, so a library caller could construct one directly and
hand it to the runner, the repair loop, or replay, bypassing every
rule: a whitespace-only command sealed a vacuous PASS, a bool
``timeout_s`` ran as 1 second (and a float as 2.5), an empty check
list sealed a record that failed its own verification, a
500,000-character name did the same — and with ``raw`` empty (the
dataclass default) the sealed ``spec_hash`` was the hash of ``{}``,
binding none of the name/brief/checks that actually ran. Every
entry point now calls ``spec.validate_spec`` before any work: the
fields must satisfy the same rules ``parse_spec`` enforces, and
``raw`` must parse back to exactly those fields, so the sealed
hash always binds the spec that ran.
"""

import pytest

from verifygate.evidence import verify_chain
from verifygate.llm import LLMAdapter
from verifygate.mock_llm import MockLLMAdapter
from verifygate.repair import run_repair
from verifygate.replay import replay_document
from verifygate.runner import run_verification
from verifygate.spec import (
    MAX_BRIEF_CHARS,
    MAX_CHECK_COMMAND_CHARS,
    MAX_CHECK_NAME_CHARS,
    MAX_CHECKS,
    MAX_SPEC_NAME_CHARS,
    MAX_TIMEOUT_S,
    Check,
    SpecError,
    TaskSpec,
    parse_spec,
    validate_spec,
)
from verifygate.tavily import MockReferenceLookup

RAW = {
    "name": "probe-task",
    "brief": "Fix the thing.",
    "checks": [{"name": "gate", "command": "true", "timeout_s": 60}],
}
GOOD_CHECK = Check(name="gate", command="true", timeout_s=60)


def _spec(**overrides):
    fields = {
        "name": "probe-task",
        "brief": "Fix the thing.",
        "checks": (GOOD_CHECK,),
        "raw": RAW,
    }
    fields.update(overrides)
    return TaskSpec(**fields)


def _check_spec(check):
    raw = {
        "name": "probe-task",
        "brief": "Fix the thing.",
        "checks": [{
            "name": getattr(check, "name", "gate"),
            "command": getattr(check, "command", "true"),
            "timeout_s": getattr(check, "timeout_s", 60),
        }],
    }
    return _spec(checks=(check,), raw=raw)


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


def _candidate(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "x.txt").write_text("hi\n")
    return cand


def _run(spec, candidate, llm=None):
    return run_verification(
        spec=spec, candidate_dir=candidate, mode="judge",
        llm=llm or MockLLMAdapter(), references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label=str(candidate))


# -- the helper itself ---------------------------------------------------


def test_parsed_spec_validates():
    spec = parse_spec(RAW)
    assert validate_spec(spec) is spec


def test_direct_spec_matching_raw_validates():
    assert validate_spec(_spec()) is not None


@pytest.mark.parametrize("bad", [
    None, "probe-task", {"name": "probe-task"}, [GOOD_CHECK], 42,
])
def test_non_taskspec_rejected(bad):
    with pytest.raises(SpecError):
        validate_spec(bad)


@pytest.mark.parametrize("overrides", [
    {"name": ""},
    {"name": "   "},
    {"name": None},
    {"name": 42},
    {"name": "x" * (MAX_SPEC_NAME_CHARS + 1)},
    {"brief": None},
    {"brief": 42},
    {"brief": "x" * (MAX_BRIEF_CHARS + 1)},
    {"checks": ()},
    {"checks": []},
    {"checks": "gate"},
    {"checks": None},
    {"checks": tuple(
        Check(name=f"c{i}", command="true") for i in range(MAX_CHECKS + 1))},
    {"checks": (GOOD_CHECK, GOOD_CHECK)},  # duplicate check name
    {"checks": ({"name": "gate", "command": "true"},)},  # not a Check
    {"checks": ("gate",)},
    {"raw": {}},
    {"raw": None},
    {"raw": "probe-task"},
    # raw parses, but to different fields than the object carries:
    {"name": "other-task"},
    {"brief": "A different brief."},
    {"checks": (Check(name="gate", command="false", timeout_s=60),)},
])
def test_invalid_spec_objects_rejected(overrides):
    with pytest.raises(SpecError):
        validate_spec(_spec(**overrides))


@pytest.mark.parametrize("check", [
    Check(name="", command="true"),
    Check(name="   ", command="true"),
    Check(name=None, command="true"),
    Check(name="x" * (MAX_CHECK_NAME_CHARS + 1), command="true"),
    Check(name="gate", command=""),
    Check(name="gate", command="   "),  # vacuous PASS if it ran
    Check(name="gate", command=None),
    Check(name="gate", command="x" * (MAX_CHECK_COMMAND_CHARS + 1)),
    Check(name="gate", command="true", timeout_s=True),
    Check(name="gate", command="true", timeout_s=False),
    Check(name="gate", command="true", timeout_s=2.5),
    Check(name="gate", command="true", timeout_s="60"),
    Check(name="gate", command="true", timeout_s=None),
    Check(name="gate", command="true", timeout_s=0),
    Check(name="gate", command="true", timeout_s=-1),
    Check(name="gate", command="true", timeout_s=MAX_TIMEOUT_S + 1),
])
def test_invalid_checks_rejected(check):
    with pytest.raises(SpecError):
        validate_spec(_check_spec(check))


def test_boundary_timeouts_accepted():
    for timeout in (1, MAX_TIMEOUT_S):
        spec = _check_spec(Check(name="gate", command="true",
                                 timeout_s=timeout))
        assert validate_spec(spec) is spec


# -- the runner ------------------------------------------------------------


def test_runner_valid_direct_spec_passes_and_verifies(tmp_path):
    cand = _candidate(tmp_path)
    record = _run(_spec(), cand)
    assert record["verdict"] == "PASS"
    assert verify_chain(record)
    # The sealed hash binds the fields that ran: it is the parsed
    # spec's hash, not the hash of an empty raw.
    assert record["entries"][0]["spec_hash"] == parse_spec(RAW).hash


def test_runner_rejects_invalid_spec_before_any_work(tmp_path):
    # The candidate does not exist: an invalid spec must raise
    # SpecError, not FileNotFoundError — validation precedes the
    # workspace — and no adapter call is ever made.
    stub = _CountingStub()
    with pytest.raises(SpecError):
        _run(_spec(name=""), tmp_path / "missing", llm=stub)
    assert stub.explain_calls == 0 and stub.patch_calls == 0


def test_runner_rejects_whitespace_command(tmp_path):
    cand = _candidate(tmp_path)
    with pytest.raises(SpecError):
        _run(_check_spec(Check(name="gate", command="   ")), cand)


# -- the repair loop -------------------------------------------------------


def test_repair_rejects_invalid_spec_with_zero_generation(tmp_path):
    cand = _candidate(tmp_path)
    stub = _CountingStub()
    with pytest.raises(SpecError):
        run_repair(
            spec=_spec(checks=()), candidate_dir=cand, mode="judge",
            llm=stub, references=MockReferenceLookup(),
            spec_label="spec.json", candidate_label=str(cand),
            max_rounds=3)
    assert stub.patch_calls == 0 and stub.explain_calls == 0


# -- replay -------------------------------------------------------------------


def test_replay_rejects_invalid_spec_up_front(tmp_path):
    cand = _candidate(tmp_path)
    record = _run(_spec(), cand)
    with pytest.raises(SpecError):
        replay_document(
            record, spec=_spec(raw={}), candidate_dir=cand, mode="judge",
            llm=MockLLMAdapter(), references=MockReferenceLookup(),
            spec_label="spec.json")


def test_replay_valid_spec_still_replays(tmp_path):
    cand = _candidate(tmp_path)
    record = _run(_spec(), cand)
    results = replay_document(
        record, spec=_spec(), candidate_dir=cand, mode="judge",
        llm=MockLLMAdapter(), references=MockReferenceLookup(),
        spec_label="spec.json")
    assert results and all(r["verdict_match"] for r in results)
