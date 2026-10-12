"""No process outlives its check (v1.72.0).

``sandbox.run_command`` starts every check in a new session, and
its timeout path SIGKILLs the whole process group — but that kill
was timeout-only. A check that exited *normally* while leaving a
background child behind leaked that child onto the host, probed
against v1.71.0 before the fix: ``(sleep 2; echo leaked >
marker) & echo done`` exited 0, and the marker appeared two
seconds after the run had returned its PASS-shaped result. The
leftover child outlives the run, can write into the workspace the
runner is deleting, and can still be alive when replay re-runs
the same checks — the original and the replay then execute in
different worlds.

The process-group SIGKILL now runs after every check, pass or
fail. A child that detaches into its own session (``setsid``)
escapes the group exactly as it already did on the timeout path;
that is the documented limit of this sandbox.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from verifygate.evidence import verify_chain
from verifygate.mock_llm import MockLLMAdapter
from verifygate.runner import run_verification
from verifygate.sandbox import prepare_workspace, run_command
from verifygate.spec import parse_spec
from verifygate.tavily import MockReferenceLookup


@pytest.fixture()
def ws(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "placeholder.txt").write_text("x")
    return prepare_workspace(cand)


def _background_writer(marker: Path, delay: float = 1.0) -> str:
    return f"(sleep {delay}; echo leaked > {marker}) &"


def test_background_child_of_a_passing_check_is_killed(ws, tmp_path):
    marker = tmp_path / "marker.txt"
    r = run_command(
        _background_writer(marker) + " echo done", cwd=ws, timeout_s=10)
    assert r.exit_code == 0
    assert r.timed_out is False
    assert "done" in r.stdout_tail
    time.sleep(1.8)  # past the child's scheduled write
    assert not marker.exists()


def test_background_child_of_a_failing_check_is_killed(ws, tmp_path):
    marker = tmp_path / "marker.txt"
    r = run_command(
        _background_writer(marker) + " exit 3", cwd=ws, timeout_s=10)
    assert r.exit_code == 3
    assert r.timed_out is False
    time.sleep(1.8)
    assert not marker.exists()


def test_waited_for_background_job_still_completes(ws):
    # A check that backgrounds a job and waits for it is finished
    # before it exits: the sweep at exit has nothing to kill and
    # the job's output is the check's own.
    r = run_command(
        "sleep 0.3 & wait; echo waited", cwd=ws, timeout_s=10)
    assert r.exit_code == 0
    assert "waited" in r.stdout_tail


def test_foreground_work_is_unchanged(ws):
    start = time.monotonic()
    r = run_command("sleep 0.4; echo done", cwd=ws, timeout_s=10)
    elapsed = time.monotonic() - start
    assert r.exit_code == 0
    assert "done" in r.stdout_tail
    assert elapsed < 5  # the sweep adds no wait of its own


def test_timeout_still_kills_the_whole_group(ws, tmp_path):
    marker = tmp_path / "marker.txt"
    r = run_command(
        _background_writer(marker, delay=1.5) + " sleep 30",
        cwd=ws, timeout_s=1)
    assert r.timed_out is True
    assert r.exit_code is None
    time.sleep(1.2)  # the child's write was scheduled for t+1.5
    assert not marker.exists()


def test_backgrounding_check_seals_a_verifying_record(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "placeholder.txt").write_text("x")
    marker = tmp_path / "marker.txt"
    spec = parse_spec({
        "name": "background-child", "brief": "",
        "checks": [{"name": "leaves_a_child",
                    "command": _background_writer(marker) + " true",
                    "timeout_s": 5}],
    })
    record = run_verification(
        spec=spec, candidate_dir=cand, mode="judge",
        llm=MockLLMAdapter(), references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label="cand")
    assert record["verdict"] == "PASS"
    assert verify_chain(record)
    time.sleep(1.8)
    assert not marker.exists()
