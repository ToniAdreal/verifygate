"""Verdict-aggregation validation (v1.55.0).

``validators.overall_verdict`` is the one function that turns
per-check results into the verdict sealed into an evidence
record, and it took its input on trust:

* an empty *iterator* aggregated to PASS (``results and
  all(...)``: an iterator is always truthy, ``all`` of nothing
  is true) while an empty list aggregated to FAIL — a vacuous
  PASS from the aggregator itself;
* ``None`` silently aggregated to FAIL, a verdict for checks
  that never ran;
* a string, or a list of plain dicts / ``None`` entries,
  crashed with an unhandled ``AttributeError``;
* each result's ``verdict`` field was trusted blindly: a result
  claiming PASS with exit code 3, or PASS while timed out,
  aggregated to PASS — against the module's one derivation rule
  (a check passes iff it exits 0 before its timeout), and the
  sealed record then failed the v1.12.0 consistency mirror, so
  a record VerifyGate emitted failed its own verification.

The input is now materialised and every entry validated through
``validate_check_result``: a ``CheckResult`` whose
verdict-bearing fields are the shapes ``run_checks`` emits (the
types and ranges ``evidence``'s structure mirror enforces) and
whose ``verdict`` is exactly the verdict those fields derive.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from verifygate.evidence import (
    MAX_DURATION_MS,
    MAX_EXIT_CODE,
    MIN_EXIT_CODE,
    build_record,
    verify_chain,
)
from verifygate.sandbox import prepare_workspace
from verifygate.spec import parse_spec
from verifygate.validators import (
    CheckResult,
    overall_verdict,
    run_checks,
    validate_check_result,
)

APP = Path(__file__).resolve().parents[1]


def _result(**overrides) -> CheckResult:
    base = dict(
        name="c1",
        command="true",
        exit_code=0,
        duration_ms=1,
        stdout_tail="",
        stderr_tail="",
        timed_out=False,
        verdict="PASS",
    )
    base.update(overrides)
    return CheckResult(**base)


def _fail(**overrides) -> CheckResult:
    base = dict(exit_code=1, verdict="FAIL")
    base.update(overrides)
    return _result(**base)


# -- genuine aggregates are unchanged ------------------------------------


def test_genuine_pass_and_fail_aggregates():
    assert overall_verdict([_result()]) == "PASS"
    assert overall_verdict([_fail()]) == "FAIL"
    assert overall_verdict([_result(), _fail()]) == "FAIL"
    assert overall_verdict([_result(), _result()]) == "PASS"


def test_genuine_timeout_and_signal_results_aggregate():
    timeout = _fail(exit_code=None, timed_out=True)
    assert overall_verdict([timeout]) == "FAIL"
    signal = _fail(exit_code=-9)
    assert overall_verdict([signal]) == "FAIL"
    assert overall_verdict([_result(exit_code=MIN_EXIT_CODE,
                                    verdict="FAIL"),
                            _result(exit_code=MAX_EXIT_CODE,
                                    verdict="FAIL")]) == "FAIL"


def test_empty_inputs_are_fail_in_every_shape():
    # The vacuous-PASS regression: an empty iterator was PASS.
    assert overall_verdict([]) == "FAIL"
    assert overall_verdict(()) == "FAIL"
    assert overall_verdict(iter([])) == "FAIL"
    assert overall_verdict(x for x in ()) == "FAIL"


def test_iterator_of_genuine_results_aggregates():
    assert overall_verdict(iter([_result()])) == "PASS"
    assert overall_verdict(iter([_result(), _fail()])) == "FAIL"


def test_run_checks_output_aggregates_end_to_end(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "placeholder.txt").write_text("x")
    spec = parse_spec({
        "name": "t", "brief": "",
        "checks": [{"name": "c1", "command": "true", "timeout_s": 10}],
    })
    results = run_checks(spec, prepare_workspace(cand))
    assert overall_verdict(results) == "PASS"
    for result in results:
        assert validate_check_result(result) is result


# -- the input itself -----------------------------------------------------


@pytest.mark.parametrize("bad", [
    None, "PASS", b"PASS", 123, 1.5, True, object(),
])
def test_non_iterable_or_string_inputs_are_value_errors(bad):
    with pytest.raises(ValueError):
        overall_verdict(bad)


@pytest.mark.parametrize("bad_entry", [
    {"verdict": "PASS"}, None, "PASS", 123, object(),
])
def test_non_check_result_entries_are_value_errors(bad_entry):
    with pytest.raises(ValueError):
        overall_verdict([bad_entry])
    with pytest.raises(ValueError):
        validate_check_result(bad_entry)


def test_a_dict_input_iterates_its_keys_and_is_rejected():
    with pytest.raises(ValueError):
        overall_verdict({"c1": _result()})


# -- verdict-bearing field shapes -----------------------------------------


@pytest.mark.parametrize("overrides", [
    dict(timed_out=1),                       # non-bool timed_out
    dict(timed_out="yes"),
    dict(duration_ms=True),                  # bool duration
    dict(duration_ms=1.5),                   # float duration
    dict(duration_ms=-1),                    # negative duration
    dict(duration_ms=MAX_DURATION_MS + 1),   # above the bound
    dict(duration_ms=10**12),                # the v1.33.0 probe shape
    dict(exit_code=None),                    # completed, no exit code
    dict(exit_code=True, verdict="FAIL"),    # bool exit code
    dict(exit_code="0"),                     # string exit code
    dict(exit_code=MAX_EXIT_CODE + 1, verdict="FAIL"),
    dict(exit_code=999_999, verdict="FAIL"),  # the v1.33.0 probe shape
    dict(exit_code=MIN_EXIT_CODE - 1, verdict="FAIL"),
    dict(timed_out=True, exit_code=0, verdict="FAIL"),  # timeout + code
])
def test_malformed_verdict_bearing_fields_are_value_errors(overrides):
    with pytest.raises(ValueError):
        validate_check_result(_result(**overrides))
    with pytest.raises(ValueError):
        overall_verdict([_result(**overrides)])


def test_boundary_durations_verify():
    assert validate_check_result(_result(duration_ms=0))
    assert validate_check_result(
        _result(duration_ms=MAX_DURATION_MS))


# -- the verdict must be the derived verdict -------------------------------


@pytest.mark.parametrize("result", [
    _result(exit_code=3),                              # PASS over exit 3
    _result(exit_code=None, timed_out=True),           # PASS over a timeout
    _fail(exit_code=0),                                # FAIL over exit 0
    _result(verdict="banana"),                         # unknown verdict
    _result(verdict="pass"),                           # case matters
    _result(verdict=None),                             # no verdict
])
def test_inconsistent_verdicts_are_value_errors(result):
    with pytest.raises(ValueError):
        validate_check_result(result)
    with pytest.raises(ValueError):
        overall_verdict([result])


def test_the_error_line_stays_bounded_for_a_huge_verdict():
    result = _result(verdict="x" * 500_000)
    with pytest.raises(ValueError) as excinfo:
        validate_check_result(result)
    assert len(str(excinfo.value)) < 300


def test_a_valid_result_is_returned_unchanged():
    result = _fail()
    assert validate_check_result(result) is result
    assert validate_check_result(result, name="results[0]") is result


# -- the aggregate seals a record that verifies ----------------------------


def test_aggregate_over_run_checks_results_seals_and_verifies(tmp_path):
    spec = parse_spec({
        "name": "t", "brief": "",
        "checks": [{"name": "c1", "command": "true", "timeout_s": 10}],
    })
    record = build_record(
        spec_name=spec.name,
        spec_hash=spec.hash,
        candidate_tree_hash="0" * 64,
        mode="judge",
        check_results=[_result().to_dict()],
        verdict=overall_verdict([_result()]),
        diagnosis=None,
        replay_command=(
            "python3 -m verifygate run --spec spec.json "
            "--candidate cand --mode judge"),
    )
    assert record["verdict"] == "PASS"
    assert verify_chain(record)
