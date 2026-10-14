"""Operator interrupts stop the check, not just the harness (v1.105.0).

Probed against v1.104.0 before the fix: SIGINT during a
``sleep 60`` check killed the CLI promptly — but the check's
process group was never swept (the leader-exit and timeout paths
sweep it; the exception path skipped both), so the orphaned
shell kept running inside its deleted workspace until the sleep
ended on its own, and the CLI died with an unhandled
``KeyboardInterrupt`` traceback. An operator who stops a run
believes the run — including the untrusted check it was
executing — has stopped.

Two halves, both pinned here: ``sandbox.run_command`` sweeps
the process group on any abnormal exit out of its drain loop
before the exception propagates, and the CLI converts SIGINT /
SIGTERM into one line on stderr and exit 130, writing nothing.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from verifygate import cli
from verifygate.sandbox import prepare_workspace, run_command

APP_ROOT = Path(__file__).resolve().parent.parent
SLEEP = "sleep 43.7"  # distinctive: pgrep collisions are implausible


def _candidate(tmp_path: Path) -> Path:
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "placeholder.txt").write_text("x")
    return cand


def test_interrupt_inside_run_command_sweeps_the_group(tmp_path):
    ws = prepare_workspace(_candidate(tmp_path))
    marker = tmp_path / "check-survived"
    timer = threading.Timer(0.5, signal.raise_signal, args=(signal.SIGINT,))
    start = time.monotonic()
    timer.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            run_command(f"sleep 4; touch {marker}", cwd=ws, timeout_s=30)
    finally:
        timer.cancel()
    # The interrupt must end the call promptly, not at the
    # check's natural end (t=4s) or its timeout (t=30s).
    assert time.monotonic() - start < 3
    time.sleep(4)  # past the check's scheduled marker write
    assert not marker.exists()


def _sleeping_run(tmp_path: Path):
    cand = _candidate(tmp_path)
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({
        "name": "interrupt-probe",
        "brief": "probe",
        "checks": [{"name": "sleepy", "command": SLEEP, "timeout_s": 120}],
    }))
    out = tmp_path / "record.json"
    proc = subprocess.Popen(
        [sys.executable, "-m", "verifygate", "run",
         "--spec", str(spec), "--candidate", str(cand),
         "--mode", "judge", "--out", str(out)],
        cwd=APP_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True)
    return proc, out


def _check_pids():
    found = subprocess.run(
        ["pgrep", "-f", SLEEP], capture_output=True, text=True).stdout
    return [p for p in found.split() if int(p) != os.getpid()]


def _wait_for_check_running():
    # Signalling after a fixed sleep is flaky on a slow box: the
    # CLI's own interpreter startup (imports) can outlast it, and
    # a signal that lands during import is not the behaviour
    # under test. Poll until the check itself is running.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if _check_pids():
            return
        time.sleep(0.2)
    raise AssertionError("check process never started")


def _assert_check_group_dead():
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        found = subprocess.run(
            ["pgrep", "-f", SLEEP], capture_output=True, text=True).stdout
        # The pgrep process itself never matches (its own cmdline
        # carries the pattern inside a different argv shape), but
        # exclude this test process's subtree defensively.
        pids = [p for p in found.split() if int(p) != os.getpid()]
        if not pids:
            return
        time.sleep(0.2)
    raise AssertionError(f"check process group survived the interrupt: {pids}")


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM],
                         ids=["sigint", "sigterm"])
def test_cli_interrupt_exits_130_and_stops_the_check(tmp_path, sig):
    proc, out = _sleeping_run(tmp_path)
    _wait_for_check_running()  # the check is running in its workspace
    start = time.monotonic()
    proc.send_signal(sig)
    _, stderr = proc.communicate(timeout=20)
    assert time.monotonic() - start < 15
    assert proc.returncode == 130
    assert "interrupted" in stderr
    assert "Traceback" not in stderr
    assert not out.exists()  # no record for a run that never decided
    _assert_check_group_dead()


def test_main_converts_keyboard_interrupt(monkeypatch, capsys):
    def boom(argv):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_dispatch", boom)
    assert cli.main([]) == 130
    err = capsys.readouterr().err
    assert "interrupted" in err
    assert "Traceback" not in err


def test_main_passes_dispatch_result_through(monkeypatch):
    before = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(cli, "_dispatch", lambda argv: 7)
    assert cli.main([]) == 7
    # The SIGTERM conversion is scoped to the dispatch: the
    # caller's handler is restored afterwards.
    assert signal.getsignal(signal.SIGTERM) == before
