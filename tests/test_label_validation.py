"""Label validation: spec/candidate labels are validated, everywhere.

Before v1.40.0 the labels were the last caller-supplied run
parameters with no validation at all. They are interpolated
through ``evidence.quote_label`` into the record's sealed
``replay`` string, and ``quote_label`` assumed a string:

* a library caller passing an ``int``, ``None``, or a list label
  crashed with an unhandled ``TypeError`` from the regex engine —
  in the runner and the repair loop *after* every check had
  already run (no record / report delivered), and in replay
  mid-replay, where the per-record path catches only ``OSError``
  / ``RuntimeError``;
* an empty label sealed the quoted empty string (``''``) as if it
  were a path;
* a label's length was unbounded — a 500,000-character label
  sealed a 500,000-character ``replay`` field that verified
  INTACT, because the metadata rules judged the replay string
  only against templates that cap no length.

Every entry point now validates both labels through
``evidence.validate_label`` before any workspace is prepared, any
check runs, or any record is replayed: a non-string, empty, or
over-``MAX_LABEL_CHARS`` label is a ``ValueError`` (CLI exit 2),
never a crash and never a run. ``quote_label`` applies the same
validation as a backstop, and ``_metadata_ok`` mirrors the bound
with ``MAX_REPLAY_CHARS`` — the largest replay string validated
labels can produce — so a record carrying a longer replay string
fails verification however it was sealed.
"""

import json
from pathlib import Path

import pytest

from verifygate.evidence import (
    MAX_LABEL_CHARS,
    MAX_REPLAY_CHARS,
    build_record,
    quote_label,
    validate_label,
    verify_chain,
)
from verifygate.llm import LLMAdapter
from verifygate.mock_llm import MockLLMAdapter
from verifygate.repair import run_repair
from verifygate.replay import replay_document, replay_ok
from verifygate.runner import run_verification
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup
from verifygate.verify import collect_records, document_ok, verify_document
from verifygate.webview import render_document

APP = Path(__file__).resolve().parent.parent
SPEC = load_spec(APP / "samples" / "spec.json")
FAILING = APP / "samples" / "failing"
PASSING = APP / "samples" / "passing"

BAD_LABELS = [
    123, None, True, ["samples/spec.json"], b"samples/spec.json",
    "", "x" * (MAX_LABEL_CHARS + 1), "x" * 500_000,
]

CHECK = {
    "name": "unit", "command": "true", "exit_code": 0,
    "duration_ms": 1, "stdout_tail": "", "stderr_tail": "",
    "timed_out": False, "verdict": "PASS",
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


def _run(candidate, llm=None, **labels):
    return run_verification(
        spec=SPEC, candidate_dir=candidate, mode="judge",
        llm=llm or MockLLMAdapter(), references=MockReferenceLookup(),
        spec_label=labels.get("spec_label", "samples/spec.json"),
        candidate_label=labels.get("candidate_label", str(candidate)))


def _repair(candidate, llm=None, **labels):
    return run_repair(
        spec=SPEC, candidate_dir=candidate, mode="judge",
        llm=llm or MockLLMAdapter(), references=MockReferenceLookup(),
        spec_label=labels.get("spec_label", "samples/spec.json"),
        candidate_label=labels.get("candidate_label", str(candidate)),
        max_rounds=3)


# -- the constants and the helper itself --------------------------------


def test_label_ceilings_are_the_documented_values():
    assert MAX_LABEL_CHARS == 1000
    assert MAX_REPLAY_CHARS == 16_384
    # The replay ceiling covers the worst case it is derived from:
    # three labels, each at the raw cap, each character a single
    # quote expanding to the 5-character shlex sequence, plus the
    # templates' fixed text.
    assert MAX_REPLAY_CHARS >= 3 * (5 * MAX_LABEL_CHARS + 2) + 200


@pytest.mark.parametrize("label", [
    "samples/spec.json", ".", "<repair-report.json>", "my spec.json",
    "候选", "a'b", "x" * MAX_LABEL_CHARS, "bad\nlabel",
])
def test_validate_label_returns_valid_labels(label):
    # A control-character label is valid here by design: quote_label
    # degrades it to the placeholder (the v1.21.0 contract).
    assert validate_label(label) == label


@pytest.mark.parametrize("bad", BAD_LABELS)
def test_validate_label_rejects_invalid_labels(bad):
    with pytest.raises(ValueError):
        validate_label(bad)


def test_validate_label_names_the_field_and_bounds_its_message():
    with pytest.raises(ValueError) as excinfo:
        validate_label(123, "spec_label")
    assert "spec_label" in str(excinfo.value)
    with pytest.raises(ValueError) as excinfo:
        validate_label("x" * 500_000, "candidate_label")
    assert "candidate_label" in str(excinfo.value)
    assert len(str(excinfo.value)) < 200


@pytest.mark.parametrize("bad", BAD_LABELS)
def test_quote_label_rejects_invalid_labels_as_value_error(bad):
    # The backstop: a direct caller's bad label is a clean
    # ValueError, never the regex engine's TypeError.
    with pytest.raises(ValueError):
        quote_label(bad)


def test_quote_label_valid_forms_unchanged():
    assert quote_label("samples/spec.json") == "samples/spec.json"
    assert quote_label("my spec.json") == "'my spec.json'"
    assert quote_label("bad\nlabel") == "<unprintable-label>"


# -- the runner ----------------------------------------------------------


@pytest.mark.parametrize("bad", BAD_LABELS)
@pytest.mark.parametrize("field", ["spec_label", "candidate_label"])
def test_runner_rejects_invalid_labels_before_any_work(bad, field):
    stub = _CountingStub()
    with pytest.raises(ValueError):
        _run(FAILING, llm=stub, **{field: bad})
    # Rejected up front: no check ran, so no diagnosis was
    # requested (the failing candidate would have asked for one).
    assert stub.explain_calls == 0


def test_runner_rejects_invalid_label_before_the_workspace_exists():
    # An invalid label against a *missing* candidate is the label
    # error (ValueError), not the workspace's FileNotFoundError:
    # the labels are judged before any workspace is prepared.
    with pytest.raises(ValueError) as excinfo:
        _run(APP / "samples" / "does-not-exist", spec_label=123)
    assert "spec_label" in str(excinfo.value)


def test_runner_at_ceiling_labels_seal_a_verifying_record():
    label = "a" * MAX_LABEL_CHARS
    record = _run(PASSING, spec_label=label, candidate_label=label)
    assert record["verdict"] == "PASS"
    assert len(record["replay"]) <= MAX_REPLAY_CHARS
    assert verify_chain(record)


def test_runner_valid_labels_still_verify():
    record = _run(PASSING)
    assert record["verdict"] == "PASS"
    assert verify_chain(record)


# -- the repair loop -----------------------------------------------------


@pytest.mark.parametrize("bad", BAD_LABELS)
@pytest.mark.parametrize("field", ["spec_label", "candidate_label"])
def test_repair_rejects_invalid_labels_before_any_work(bad, field):
    stub = _CountingStub()
    with pytest.raises(ValueError):
        _repair(FAILING, llm=stub, **{field: bad})
    assert stub.explain_calls == 0
    assert stub.patch_calls == 0


def test_repair_valid_labels_still_repair_and_verify():
    report = _repair(FAILING)
    assert report["final_verdict"] == "PASS"
    assert document_ok(verify_document(report))


# -- replay --------------------------------------------------------------


def _genuine_record():
    return _run(FAILING)


@pytest.mark.parametrize("bad", BAD_LABELS)
def test_replay_rejects_invalid_spec_label_up_front(bad):
    with pytest.raises(ValueError):
        replay_document(
            _genuine_record(), spec=SPEC, candidate_dir=FAILING,
            mode="judge", llm=MockLLMAdapter(),
            references=MockReferenceLookup(), spec_label=bad)


def test_replay_rejects_an_overlong_candidate_label_up_front():
    with pytest.raises(ValueError) as excinfo:
        replay_document(
            _genuine_record(), spec=SPEC,
            candidate_dir="x" * (MAX_LABEL_CHARS + 1), mode="judge",
            llm=MockLLMAdapter(), references=MockReferenceLookup(),
            spec_label="samples/spec.json")
    assert "candidate_label" in str(excinfo.value)


def test_replay_valid_labels_still_reproduce_a_record():
    results = replay_document(
        _genuine_record(), spec=SPEC, candidate_dir=FAILING,
        mode="judge", llm=MockLLMAdapter(),
        references=MockReferenceLookup(), spec_label="samples/spec.json")
    assert replay_ok(results)


# -- the verification mirror ---------------------------------------------


def _record_with_replay(replay):
    return build_record(
        spec_name="demo", spec_hash="a" * 64,
        candidate_tree_hash="b" * 64, mode="judge",
        check_results=[CHECK], verdict="PASS", diagnosis=None,
        replay_command=replay)


def _replay_of_total_length(total):
    prefix = "python3 -m verifygate run --spec "
    suffix = " --candidate c --mode judge"
    return prefix + "a" * (total - len(prefix) - len(suffix)) + suffix


def test_record_at_the_replay_ceiling_verifies():
    record = _record_with_replay(_replay_of_total_length(MAX_REPLAY_CHARS))
    assert len(record["replay"]) == MAX_REPLAY_CHARS
    assert verify_chain(record)


def test_record_above_the_replay_ceiling_fails_verification():
    record = _record_with_replay(
        _replay_of_total_length(MAX_REPLAY_CHARS + 1))
    assert verify_chain(record) is False
    results = verify_document(record)
    assert not document_ok(results)


def test_huge_label_record_fails_verification_and_renders_invalid():
    # The pre-fix probe shape: a 500,000-character label, sealed
    # through the permissive assembler.
    record = _record_with_replay(
        "python3 -m verifygate run --spec " + "x" * 500_000
        + " --candidate c --mode judge")
    assert verify_chain(record) is False
    assert "CHAIN INVALID" in render_document(record)


def test_all_committed_fixtures_still_verify():
    paths = sorted((APP / "samples").glob("*.json")) \
        + sorted((APP / "samples-js").glob("*.json")) \
        + [APP / "evidence_self_check.json"]
    checked = 0
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("tool") != "verifygate":
            continue
        for _label, record in collect_records(data):
            assert len(record["replay"]) <= MAX_REPLAY_CHARS, path
            assert verify_chain(record), path
            checked += 1
    assert checked >= 12
