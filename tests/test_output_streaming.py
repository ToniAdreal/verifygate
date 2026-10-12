"""Streamed, bounded output capture (v1.78.0).

``sandbox.run_command`` spooled check stdout/stderr to temp
files and capped only the *read-back*: the spool itself grew
without bound for as long as the check ran. A probe against
v1.77.0 measured a check printing 200 MB growing the temp
filesystem by the full 200 MB mid-run — on the 512 MB tmpfs
this project already filled once in practice (the v1.0.0
Errno 28 incident), a ~600 MB print, or a verbose stdout and
stderr together, exhausts the temp filesystem while the
verdict is still being decided.

Output is now streamed through pipes into a bounded in-memory
tail buffer (:class:`verifygate.sandbox._TailBuffer`): only
the last ``_READ_BYTES`` bytes of each stream are kept, the
rest is discarded as it arrives, and the sealed tails are
byte-for-byte what the spool produced. The process-group
sweep runs when the leader exits, and draining is bounded by
a grace period, so a background or session-detached child
holding a pipe open cannot hang the capture.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from verifygate import sandbox
from verifygate.evidence import verify_chain
from verifygate.mock_llm import MockLLMAdapter
from verifygate.runner import run_verification
from verifygate.sandbox import (
    _READ_BYTES,
    MAX_OUTPUT_CHARS,
    _TailBuffer,
    prepare_workspace,
    run_command,
)
from verifygate.spec import parse_spec
from verifygate.tavily import MockReferenceLookup

MEGABYTE_PRINT = (
    "python3 -c \"import sys; "
    "sys.stdout.write('x' * 20_000_000)\""
)


@pytest.fixture()
def ws(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "placeholder.txt").write_text("x")
    return prepare_workspace(cand)


def test_tail_buffer_keeps_only_the_last_bytes():
    buf = _TailBuffer()
    buf.append(b"a" * _READ_BYTES)
    buf.append(b"b" * 100)
    # White-box: the buffer never holds more than the tail bound,
    # however much was appended.
    assert len(buf._data) == _READ_BYTES
    assert buf.tail().endswith("b" * 100)


def test_tail_buffer_tail_shape_matches_the_spool():
    buf = _TailBuffer()
    buf.append(("line\n" * 5000).encode())
    tail = buf.tail()
    assert tail.startswith("…[truncated]…\n")
    assert len(tail) <= MAX_OUTPUT_CHARS + len("…[truncated]…\n")


def test_small_output_is_captured_verbatim(ws):
    r = run_command("printf 'hello\\n'; printf 'err\\n' >&2; exit 3",
                    cwd=ws, timeout_s=5)
    assert r.exit_code == 3
    assert r.timed_out is False
    assert r.stdout_tail == "hello\n"
    assert r.stderr_tail == "err\n"


def test_huge_stdout_tail_is_correct_and_bounded(ws):
    r = run_command(MEGABYTE_PRINT, cwd=ws, timeout_s=30)
    assert r.exit_code == 0
    assert r.timed_out is False
    assert len(r.stdout_tail) <= MAX_OUTPUT_CHARS + 20
    assert r.stdout_tail.startswith("…[truncated]…\n")
    assert set(r.stdout_tail.split("\n", 1)[1]) == {"x"}


def test_huge_stdout_and_stderr_keep_separate_tails(ws):
    r = run_command(
        "python3 -c \"import sys; sys.stdout.write('o' * 5_000_000); "
        "sys.stderr.write('e' * 5_000_000)\"",
        cwd=ws, timeout_s=30)
    assert r.exit_code == 0
    assert set(r.stdout_tail.split("\n", 1)[1]) == {"o"}
    assert set(r.stderr_tail.split("\n", 1)[1]) == {"e"}


def test_capture_uses_no_temp_file_spool(ws, monkeypatch):
    # The regression pin for the unbounded spool: run_command
    # must never reach for a TemporaryFile again.
    def _no_spool(*args, **kwargs):
        raise AssertionError("run_command spooled output to a temp file")

    monkeypatch.setattr(sandbox.tempfile, "TemporaryFile", _no_spool)
    r = run_command(MEGABYTE_PRINT, cwd=ws, timeout_s=30)
    assert r.exit_code == 0


def test_endless_output_still_times_out(ws):
    r = run_command("while true; do printf 'y'; done",
                    cwd=ws, timeout_s=1)
    assert r.timed_out is True
    assert r.exit_code is None
    assert 800 < r.duration_ms < 10_000


def test_detached_child_holding_the_pipe_cannot_hang_capture(ws):
    # A setsid child escapes the process-group sweep (the
    # documented limit) and keeps the stdout pipe open after the
    # leader exits: draining must stop at the grace bound, far
    # inside the check's own timeout.
    start = time.monotonic()
    r = run_command(
        "setsid sh -c 'sleep 30' & echo done", cwd=ws, timeout_s=20)
    elapsed = time.monotonic() - start
    assert r.exit_code == 0
    assert "done" in r.stdout_tail
    assert elapsed < 10


def test_check_closing_its_streams_still_runs_to_its_exit(ws):
    r = run_command("exec 1>&- 2>&-; sleep 1; exit 7",
                    cwd=ws, timeout_s=5)
    assert r.exit_code == 7
    assert r.timed_out is False
    assert 800 < r.duration_ms < 4_000


def test_unicode_output_tail_decodes(ws):
    r = run_command("python3 -c \"print('héllo ✓ ' * 1000)\"",
                    cwd=ws, timeout_s=5)
    assert r.exit_code == 0
    assert r.stdout_tail.rstrip().endswith("✓")


def test_huge_output_check_seals_a_verifying_record(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "placeholder.txt").write_text("x")
    spec = parse_spec({
        "name": "huge-output", "brief": "",
        "checks": [{"name": "loud", "command": MEGABYTE_PRINT,
                    "timeout_s": 30}],
    })
    record = run_verification(
        spec=spec, candidate_dir=cand, mode="judge",
        llm=MockLLMAdapter(), references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label="cand")
    assert record["verdict"] == "PASS"
    check = [e for e in record["entries"] if e["kind"] == "check"][0]
    assert len(check["stdout_tail"]) <= MAX_OUTPUT_CHARS + 20
    assert verify_chain(record)
