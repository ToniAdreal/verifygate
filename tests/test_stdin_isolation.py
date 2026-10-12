"""Closed stdin for check commands (v1.69.0).

``sandbox.run_command`` is the one execution primitive, and it
spawned check commands with no ``stdin`` argument — so a check
inherited the harness's own stdin. Two consequences, both probed
against v1.68.0 before the fix:

* **The verdict depended on the harness's stdin.** The same
  candidate and the same check (``read answer; test "$answer" =
  yes``) sealed PASS when the harness's stdin carried ``yes``
  and FAIL when it carried nothing. That input is in neither
  the spec nor the candidate, so it is not in the evidence
  record either — and replay, which runs in a different process
  with different stdin, could never reproduce the verdict the
  record claims to make replayable.
* **A reading check hung until its timeout.** A check that
  consumes stdin (``cat``, ``read``, ``input()``) blocked for
  the full timeout whenever the harness's stdin was a terminal
  or a held-open pipe, instead of seeing end-of-input.

Check commands now run with stdin attached to ``/dev/null``:
a read sees immediate EOF, the verdict is a function of the
spec and the candidate only, and a check can never consume
input meant for the harness or the operator.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from verifygate.evidence import verify_chain
from verifygate.mock_llm import MockLLMAdapter
from verifygate.runner import run_verification
from verifygate.sandbox import prepare_workspace, run_command
from verifygate.spec import parse_spec
from verifygate.tavily import MockReferenceLookup

APP = Path(__file__).resolve().parents[1]

READ_CHECK = 'read answer; echo "got:$answer"; test "$answer" = yes'


@pytest.fixture()
def ws(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "placeholder.txt").write_text("x")
    return prepare_workspace(cand)


def _child_run_command(command: str, timeout_s: int = 10) -> subprocess.Popen:
    """A child process whose stdin is a pipe this test controls."""
    code = (
        "import sys;"
        f"sys.path.insert(0, {str(APP)!r});"
        "from pathlib import Path;"
        "from verifygate.sandbox import run_command;"
        f"r = run_command({command!r}, Path({str(_WS)!r}), {timeout_s});"
        "print(r.exit_code, r.timed_out)"
    )
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)


_WS = ""  # set per-test by the subprocess tests


def test_read_check_sees_eof_not_the_harness_stdin(ws, tmp_path):
    # Feed the child harness "yes" on its stdin: before the fix
    # the check read that line and passed. The check must now
    # see EOF (empty answer) and fail, exactly as it would under
    # replay in any other process.
    global _WS
    _WS = str(ws)
    proc = _child_run_command(READ_CHECK)
    out, _ = proc.communicate(input="yes\n", timeout=60)
    assert out.strip() == "1 False"


def test_read_check_result_is_stdin_independent(ws):
    global _WS
    _WS = str(ws)
    results = []
    for fed in ("yes\n", "no\n", ""):
        proc = _child_run_command(READ_CHECK)
        out, _ = proc.communicate(input=fed, timeout=60)
        results.append(out.strip())
    assert results == ["1 False"] * 3


def test_stdin_consumer_does_not_wait_for_the_harness(ws):
    # The child's stdin pipe is held open without data: before
    # the fix a `cat` check blocked until its timeout (10 s);
    # with /dev/null it sees EOF and finishes immediately.
    global _WS
    _WS = str(ws)
    proc = _child_run_command("cat > /dev/null; echo done", timeout_s=10)
    try:
        deadline = time.monotonic() + 8
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert proc.poll() is not None, "check waited on harness stdin"
        out = proc.stdout.read()
        assert out.strip() == "0 False"
    finally:
        if proc.stdin:
            proc.stdin.close()
        proc.wait(timeout=30)


def test_cat_check_completes_without_timeout(ws):
    r = run_command("cat > /dev/null; echo done", cwd=ws, timeout_s=5)
    assert r.exit_code == 0
    assert r.timed_out is False
    assert "done" in r.stdout_tail


def test_python_input_gets_eof_error_not_a_hang(ws):
    r = run_command(
        "python3 -c \"import sys; print(len(sys.stdin.read()))\"",
        cwd=ws, timeout_s=5)
    assert r.exit_code == 0
    assert r.timed_out is False
    assert r.stdout_tail.strip() == "0"


def test_genuine_check_output_is_unchanged(ws):
    r = run_command("printf 'hello\\n'; printf 'err\\n' >&2",
                    cwd=ws, timeout_s=5)
    assert r.exit_code == 0
    assert "hello" in r.stdout_tail
    assert "err" in r.stderr_tail


def test_stdin_reading_check_seals_a_verifying_record(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "placeholder.txt").write_text("x")
    spec = parse_spec({
        "name": "stdin-eof", "brief": "",
        "checks": [{"name": "reads_eof",
                    "command": 'answer=$(cat); test -z "$answer"',
                    "timeout_s": 5}],
    })
    record = run_verification(
        spec=spec, candidate_dir=cand, mode="judge",
        llm=MockLLMAdapter(), references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label="cand")
    assert record["verdict"] == "PASS"
    assert verify_chain(record)
