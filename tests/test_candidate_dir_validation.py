"""Candidate-directory validation: the directory itself is validated.

Before v1.41.0 ``candidate_dir`` was the last run parameter
converted with ``Path(...)`` unchecked, on every entry point:

* a non-path value (``None``, an ``int``, a list, ``bytes``)
  crashed with an unhandled ``TypeError`` — in the runner before
  its checks, in the repair loop at ``Path(candidate_dir)``, in
  replay mid-replay (past the per-record path's
  ``OSError``/``RuntimeError`` handling, because replay validated
  ``str(candidate_dir)`` — a stringification, not the value), and
  in ``verify``'s candidate binding inside ``tree_hash``;
* an empty string silently became the *current* directory
  (``Path("")`` is ``"."``): a run verified whatever tree the
  caller happened to stand in and sealed its tree hash under an
  unrelated candidate label, while ``verify`` hashed the same
  wrong tree and reported a binding *mismatch* instead of a
  usage error;
* a regular file raised ``FileNotFoundError`` from the workspace
  copy but ``NotADirectoryError`` from the tree hash — the same
  fault, two exceptions, depending on which path noticed first;
* an over-long path produced a repair report whose stored
  attempt ``candidate_dir`` fields exceed the 1,000-character cap
  verification enforces over them — a report that fails its own
  verification.

Every entry point now validates the directory through
``sandbox.validate_candidate_dir`` before any workspace is
prepared, any check runs, any record is replayed, or any tree is
hashed: a non-string/non-Path, empty, or over-1,000-character
value is a ``ValueError`` (CLI exit 2), a missing or
not-a-directory value is the one ``FileNotFoundError`` every path
already produced for it, and ``verify``'s ``candidate_dir=None``
remains the unjudged "no binding" sentinel.
"""

from pathlib import Path

import pytest

from verifygate import cli
from verifygate.evidence import tree_hash, verify_chain
from verifygate.llm import LLMAdapter
from verifygate.mock_llm import MockLLMAdapter
from verifygate.repair import run_repair
from verifygate.replay import replay_document, replay_ok
from verifygate.runner import run_verification
from verifygate.sandbox import (
    MAX_CANDIDATE_DIR_CHARS,
    prepare_workspace,
    validate_candidate_dir,
)
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup
from verifygate.verify import (
    MAX_CANDIDATE_DIR_CHARS as VERIFY_MAX_CANDIDATE_DIR_CHARS,
)
from verifygate.verify import document_ok, verify_document

APP = Path(__file__).resolve().parent.parent
SPEC = load_spec(APP / "samples" / "spec.json")
FAILING = APP / "samples" / "failing"
PASSING = APP / "samples" / "passing"
A_FILE = PASSING / "stringtools.py"
MISSING = APP / "samples" / "does-not-exist"

BAD_TYPES = [123, None, True, ["samples/passing"], b"samples/passing",
             {"dir": "samples/passing"}, 2.5]
BAD_VALUES = BAD_TYPES + ["", "x" * (MAX_CANDIDATE_DIR_CHARS + 1),
                          "x" * 500_000]


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


def _run(candidate, llm=None):
    return run_verification(
        spec=SPEC, candidate_dir=candidate, mode="judge",
        llm=llm or MockLLMAdapter(), references=MockReferenceLookup(),
        spec_label="samples/spec.json", candidate_label="cand")


def _repair(candidate, llm=None):
    return run_repair(
        spec=SPEC, candidate_dir=candidate, mode="judge",
        llm=llm or MockLLMAdapter(), references=MockReferenceLookup(),
        spec_label="samples/spec.json", candidate_label="cand",
        max_rounds=3)


def _genuine_record():
    return _run(FAILING)


# -- the constant and the helper itself ---------------------------------


def test_candidate_dir_ceiling_is_the_documented_value():
    assert MAX_CANDIDATE_DIR_CHARS == 1000
    # One definition for both sides: the constant verify mirrors
    # over a report's stored attempt directories is the sandbox's.
    assert VERIFY_MAX_CANDIDATE_DIR_CHARS == MAX_CANDIDATE_DIR_CHARS


@pytest.mark.parametrize("good", [
    "samples/passing", ".", PASSING, FAILING,
])
def test_validate_candidate_dir_returns_valid_dirs(good):
    assert validate_candidate_dir(good) == Path(good)


def test_validate_candidate_dir_at_ceiling_is_judged_on_existence():
    # Exactly at the ceiling the value is well-formed: the failure
    # is the ordinary not-found, not the length rule.
    with pytest.raises(FileNotFoundError):
        validate_candidate_dir("x" * MAX_CANDIDATE_DIR_CHARS)


@pytest.mark.parametrize("bad", BAD_TYPES)
def test_validate_candidate_dir_rejects_non_paths(bad):
    with pytest.raises(ValueError) as excinfo:
        validate_candidate_dir(bad)
    assert "candidate_dir" in str(excinfo.value)


def test_validate_candidate_dir_rejects_empty_string():
    with pytest.raises(ValueError) as excinfo:
        validate_candidate_dir("")
    assert "empty" in str(excinfo.value)


@pytest.mark.parametrize("bad", ["x" * (MAX_CANDIDATE_DIR_CHARS + 1),
                                 "x" * 500_000])
def test_validate_candidate_dir_rejects_overlong_paths(bad):
    with pytest.raises(ValueError) as excinfo:
        validate_candidate_dir(bad)
    assert str(MAX_CANDIDATE_DIR_CHARS) in str(excinfo.value)
    # The error line itself is bounded, however huge the value was.
    assert len(str(excinfo.value)) < 200


def test_validate_candidate_dir_bounds_its_type_error_message():
    with pytest.raises(ValueError) as excinfo:
        validate_candidate_dir(["x" * 500_000])
    assert len(str(excinfo.value)) < 200


@pytest.mark.parametrize("bad", [MISSING, A_FILE, "no-such-dir",
                                 PASSING / "stringtools.py"])
def test_validate_candidate_dir_missing_or_file_is_file_not_found(bad):
    with pytest.raises(FileNotFoundError):
        validate_candidate_dir(bad)


def test_validate_candidate_dir_unstatable_path_is_file_not_found():
    # A single component over the filesystem's name limit (255 on
    # this platform) is under the length ceiling but cannot be
    # stat'ed: is_dir() may raise ENAMETOOLONG instead of returning
    # False. That is a not-a-directory, not a crash.
    with pytest.raises(FileNotFoundError):
        validate_candidate_dir("x" * 300)


def test_validate_candidate_dir_names_the_field():
    with pytest.raises(ValueError) as excinfo:
        validate_candidate_dir(123, name="candidate_label")
    assert "candidate_label" in str(excinfo.value)


# -- the low-level paths ---------------------------------------------------


@pytest.mark.parametrize("bad", BAD_VALUES)
def test_prepare_workspace_rejects_invalid_candidates(bad):
    with pytest.raises((ValueError, FileNotFoundError)):
        prepare_workspace(bad)


@pytest.mark.parametrize("bad", BAD_TYPES + [""])
def test_prepare_workspace_type_and_empty_faults_are_value_errors(bad):
    with pytest.raises(ValueError):
        prepare_workspace(bad)


@pytest.mark.parametrize("bad", BAD_VALUES)
def test_tree_hash_rejects_invalid_candidates(bad):
    with pytest.raises((ValueError, FileNotFoundError)):
        tree_hash(bad)


def test_tree_hash_file_is_file_not_found_not_not_a_directory():
    # The pre-fix tree hash raised NotADirectoryError for the same
    # file prepare_workspace reported as FileNotFoundError.
    with pytest.raises(FileNotFoundError):
        tree_hash(A_FILE)


def test_tree_hash_valid_dir_is_stable():
    assert tree_hash(PASSING) == tree_hash(PASSING)


# -- the runner ------------------------------------------------------------


@pytest.mark.parametrize("bad", BAD_VALUES)
def test_runner_rejects_invalid_candidates_before_any_work(bad):
    stub = _CountingStub()
    with pytest.raises((ValueError, FileNotFoundError)):
        _run(bad, llm=stub)
    # Rejected up front: no check ran, so no diagnosis was
    # requested (the failing candidate would have asked for one).
    assert stub.explain_calls == 0


@pytest.mark.parametrize("bad", BAD_TYPES + [""])
def test_runner_type_and_empty_faults_are_value_errors(bad):
    with pytest.raises(ValueError):
        _run(bad)


@pytest.mark.parametrize("bad", [MISSING, A_FILE])
def test_runner_missing_or_file_is_file_not_found(bad):
    with pytest.raises(FileNotFoundError):
        _run(bad)


def test_runner_rejects_invalid_candidate_before_the_labels_are_used():
    # An invalid candidate against an invalid label is judged in
    # entry order (labels first, as before) — but a *valid* pair of
    # labels cannot rescue an invalid candidate, and the candidate
    # error names the candidate, not the workspace copy.
    with pytest.raises(ValueError) as excinfo:
        run_verification(
            spec=SPEC, candidate_dir=123, mode="judge",
            llm=MockLLMAdapter(), references=MockReferenceLookup(),
            spec_label="samples/spec.json", candidate_label="cand")
    assert "candidate_dir" in str(excinfo.value)


def test_runner_valid_candidates_still_verify():
    assert _run(PASSING)["verdict"] == "PASS"
    record = _run(FAILING)
    assert record["verdict"] == "FAIL"
    assert verify_chain(record)
    # A string path and the equivalent Path seal the same tree hash.
    assert _run(str(PASSING))["entries"][0]["candidate_tree_hash"] \
        == _run(PASSING)["entries"][0]["candidate_tree_hash"]


# -- the repair loop -------------------------------------------------------


@pytest.mark.parametrize("bad", BAD_VALUES)
def test_repair_rejects_invalid_candidates_before_any_work(bad):
    stub = _CountingStub()
    with pytest.raises((ValueError, FileNotFoundError)):
        _repair(bad, llm=stub)
    assert stub.explain_calls == 0
    assert stub.patch_calls == 0


@pytest.mark.parametrize("bad", [MISSING, A_FILE])
def test_repair_missing_or_file_is_file_not_found(bad):
    with pytest.raises(FileNotFoundError):
        _repair(bad)


def test_repair_valid_candidate_still_repairs_and_verifies():
    report = _repair(FAILING)
    assert report["final_verdict"] == "PASS"
    assert document_ok(verify_document(report))


# -- replay ------------------------------------------------------------------


@pytest.mark.parametrize("bad", BAD_VALUES)
def test_replay_rejects_invalid_candidates_up_front(bad):
    with pytest.raises((ValueError, FileNotFoundError)) as excinfo:
        replay_document(
            _genuine_record(), spec=SPEC, candidate_dir=bad,
            mode="judge", llm=MockLLMAdapter(),
            references=MockReferenceLookup(),
            spec_label="samples/spec.json")
    # Judged under the derived-label name replay has always used.
    assert "candidate_label" in str(excinfo.value) \
        or isinstance(excinfo.value, FileNotFoundError)


def test_replay_file_candidate_is_a_usage_error_not_a_record_error():
    # Before the fix a file candidate surfaced only as a per-record
    # replay error (the tree hash's NotADirectoryError, caught by
    # the per-record path); now it fails before any record replays.
    with pytest.raises(FileNotFoundError):
        replay_document(
            _genuine_record(), spec=SPEC, candidate_dir=A_FILE,
            mode="judge", llm=MockLLMAdapter(),
            references=MockReferenceLookup(),
            spec_label="samples/spec.json")


def test_replay_valid_candidate_still_reproduces():
    results = replay_document(
        _genuine_record(), spec=SPEC, candidate_dir=FAILING,
        mode="judge", llm=MockLLMAdapter(),
        references=MockReferenceLookup(),
        spec_label="samples/spec.json")
    assert replay_ok(results)


# -- verify's candidate binding ----------------------------------------------


@pytest.mark.parametrize("bad", [b for b in BAD_TYPES if b is not None]
                         + [""])
def test_verify_rejects_malformed_candidate_bindings(bad):
    with pytest.raises(ValueError):
        verify_document(_genuine_record(), candidate_dir=bad)


@pytest.mark.parametrize("bad", [MISSING, A_FILE])
def test_verify_missing_or_file_binding_is_file_not_found(bad):
    with pytest.raises(FileNotFoundError):
        verify_document(_genuine_record(), candidate_dir=bad)


def test_verify_none_candidate_is_the_unjudged_sentinel():
    results = verify_document(_genuine_record(), candidate_dir=None)
    assert results[0][1]["candidate_match"] is None
    assert document_ok(results)


def test_verify_valid_candidate_binding_still_matches():
    results = verify_document(_genuine_record(), candidate_dir=FAILING)
    assert results[0][1]["candidate_match"] is True
    assert document_ok(results)


# -- the CLI -------------------------------------------------------------------


def test_cli_run_empty_candidate_is_a_usage_error(tmp_path):
    rc = cli.main(["run", "--spec", str(APP / "samples" / "spec.json"),
                   "--candidate", "", "--mode", "judge",
                   "--out", str(tmp_path / "out.json")])
    assert rc == 2
    assert not (tmp_path / "out.json").exists()


def test_cli_repair_empty_candidate_is_a_usage_error(tmp_path):
    rc = cli.main(["repair", "--spec", str(APP / "samples" / "spec.json"),
                   "--candidate", "", "--mode", "judge",
                   "--out", str(tmp_path / "out.json")])
    assert rc == 2
    assert not (tmp_path / "out.json").exists()


def test_cli_verify_empty_candidate_is_a_usage_error():
    rc = cli.main(["verify", "--record",
                   str(APP / "samples" / "evidence_passing.json"),
                   "--candidate", ""])
    assert rc == 2


def test_cli_replay_empty_candidate_is_a_usage_error():
    rc = cli.main(["replay", "--record",
                   str(APP / "samples" / "evidence_passing.json"),
                   "--spec", str(APP / "samples" / "spec.json"),
                   "--candidate", ""])
    assert rc == 2


def test_cli_run_file_candidate_is_a_usage_error(tmp_path):
    rc = cli.main(["run", "--spec", str(APP / "samples" / "spec.json"),
                   "--candidate", str(A_FILE), "--mode", "judge",
                   "--out", str(tmp_path / "out.json")])
    assert rc == 2
    assert not (tmp_path / "out.json").exists()
