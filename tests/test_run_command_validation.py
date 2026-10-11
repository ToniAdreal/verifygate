"""Check-execution argument validation (v1.51.0).

``sandbox.run_command`` is the one execution primitive — every
verdict VerifyGate ever seals passes through it — but its
arguments were bounded only transitively, through a validated
spec. A direct caller of the execution layer (the layer the
test suite itself drives) met, per argument:

* command: a ``TypeError`` traceback for a non-string; a vacuous
  exit-0 result for an empty/whitespace command (the v1.7.0
  vacuous-PASS shape, one layer down — the shell exits 0 on
  empty input, so nothing was checked and everything "passed").
* timeout_s: a ``TypeError`` for a string; ``True`` silently
  running as one second; ``None`` accepted as an unbounded wait;
  ``0`` producing an instant timeout verdict for a check that
  never got to run; a float accepted though the spec rules
  forbid float timeouts.
* cwd: ``123`` stringified into a lookup for a directory
  literally named "123"; a regular file raising
  ``NotADirectoryError`` instead of the one ``FileNotFoundError``
  the candidate-directory rule (v1.41.0) promises; ``None``
  stringified into "None".

All three arguments are now validated at the primitive, with
exactly the rules the spec layer enforces (command: non-empty
string within ``MAX_CHECK_COMMAND_CHARS``; timeout: a real
``int`` in ``1..MAX_TIMEOUT_S``; cwd: an existing directory via
``validate_candidate_dir``), and ``validators.run_checks``
validates its ``spec`` and ``workspace`` at its own entry
instead of trusting the runner to have done it.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from verifygate.sandbox import (
    prepare_workspace,
    run_command,
    validate_check_command,
    validate_check_timeout,
)
from verifygate.spec import (
    MAX_CHECK_COMMAND_CHARS,
    MAX_TIMEOUT_S,
    parse_spec,
)
from verifygate.validators import run_checks

APP = Path(__file__).resolve().parents[1]


@pytest.fixture()
def ws(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "placeholder.txt").write_text("x")
    return prepare_workspace(cand)


def _spec(command="true", timeout_s=5):
    return parse_spec({
        "name": "t", "brief": "",
        "checks": [{"name": "c1", "command": command,
                    "timeout_s": timeout_s}],
    })


# -- command -------------------------------------------------------------


@pytest.mark.parametrize("bad", [123, None, True, ["true"], {"c": 1}, b"true"])
def test_non_string_command_is_a_value_error(ws, bad):
    with pytest.raises(ValueError, match="command"):
        run_command(bad, cwd=ws, timeout_s=5)


@pytest.mark.parametrize("bad", ["", "   ", "\n\t "])
def test_empty_or_whitespace_command_is_a_value_error(ws, bad):
    # The pre-fix probe: both sealed a vacuous exit-0 result.
    with pytest.raises(ValueError, match="non-empty"):
        run_command(bad, cwd=ws, timeout_s=5)


def test_command_at_the_ceiling_runs(ws):
    command = "true #" + "x" * (MAX_CHECK_COMMAND_CHARS - len("true #"))
    assert len(command) == MAX_CHECK_COMMAND_CHARS
    r = run_command(command, cwd=ws, timeout_s=5)
    assert r.exit_code == 0


def test_command_above_the_ceiling_is_a_value_error(ws):
    with pytest.raises(ValueError, match="at most"):
        run_command("x" * (MAX_CHECK_COMMAND_CHARS + 1),
                    cwd=ws, timeout_s=5)


def test_validate_check_command_returns_valid_verbatim():
    assert validate_check_command(" make test ") == " make test "


# -- timeout -------------------------------------------------------------


@pytest.mark.parametrize("bad", ["5", None, True, False, 0, -1, 2.5,
                                 MAX_TIMEOUT_S + 1, 10 ** 9, [5]])
def test_bad_timeout_is_a_value_error(ws, bad):
    with pytest.raises(ValueError, match="timeout_s"):
        run_command("true", cwd=ws, timeout_s=bad)


def test_validate_check_timeout_boundaries():
    assert validate_check_timeout(1) == 1
    assert validate_check_timeout(MAX_TIMEOUT_S) == MAX_TIMEOUT_S


def test_timeout_still_produces_a_timeout_result(ws):
    r = run_command("sleep 30", cwd=ws, timeout_s=1)
    assert r.timed_out is True
    assert r.exit_code is None


# -- cwd -------------------------------------------------------------------


@pytest.mark.parametrize("bad", [None, 123, True, ["x"], b"x"])
def test_non_path_cwd_is_a_value_error(ws, bad):
    with pytest.raises(ValueError, match="cwd"):
        run_command("true", cwd=bad, timeout_s=5)


def test_empty_cwd_is_a_value_error(ws):
    with pytest.raises(ValueError, match="cwd"):
        run_command("true", cwd="", timeout_s=5)


def test_missing_cwd_is_the_one_file_not_found(ws, tmp_path):
    with pytest.raises(FileNotFoundError):
        run_command("true", cwd=tmp_path / "nope", timeout_s=5)


def test_file_cwd_is_the_one_file_not_found(ws):
    # Was NotADirectoryError before the fix — the same fault the
    # candidate-directory validation unified in v1.41.0.
    with pytest.raises(FileNotFoundError):
        run_command("true", cwd=APP / "samples" / "spec.json",
                    timeout_s=5)


def test_valid_run_is_unchanged(ws):
    r = run_command("python3 -c \"print('ok')\"", cwd=ws, timeout_s=10)
    assert r.exit_code == 0
    assert r.timed_out is False
    assert "ok" in r.stdout_tail


# -- run_checks entry --------------------------------------------------------


def test_run_checks_rejects_an_invalid_spec_object(ws):
    # A hand-built spec bypassing parse_spec: whitespace command,
    # raw that binds none of the fields that would run.
    bad = replace(_spec(), checks=replace(_spec().checks[0],
                                          command="   "))
    with pytest.raises(ValueError):
        run_checks(bad, ws)


@pytest.mark.parametrize("bad", [None, 123, "", "missing-place"])
def test_run_checks_rejects_a_bad_workspace(ws, tmp_path, bad):
    target = tmp_path / bad if bad == "missing-place" else bad
    with pytest.raises((ValueError, FileNotFoundError)):
        run_checks(_spec(), target)


def test_run_checks_rejects_a_file_workspace(ws):
    with pytest.raises(FileNotFoundError):
        run_checks(_spec(), APP / "samples" / "spec.json")


def test_run_checks_valid_still_decides_verdicts(ws):
    results = run_checks(_spec("true"), ws)
    assert results[0].verdict == "PASS"
    results = run_checks(_spec("false"), ws)
    assert results[0].verdict == "FAIL"
