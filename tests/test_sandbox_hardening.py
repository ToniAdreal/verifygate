"""Sandbox hardening: env scrubbing, symlink containment, bounded
output, process-group timeouts, and hash/prompt consistency."""

import os
import time
from pathlib import Path

from verifygate.evidence import tree_hash
from verifygate.repair import _candidate_files
from verifygate.sandbox import (
    MAX_OUTPUT_CHARS,
    prepare_workspace,
    run_command,
    sanitized_env,
)


def _candidate(tmp_path: Path) -> Path:
    d = tmp_path / "cand"
    d.mkdir()
    (d / "real.txt").write_text("real content")
    return d


def test_secrets_never_reach_check_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "fake-nebius")
    monkeypatch.setenv("TAVILY_API_KEY", "fake-tavily")
    monkeypatch.setenv("VERIFYGATE_SIGNING_KEY", "fake-signing")
    monkeypatch.setenv("SOME_RANDOM_TOKEN", "fake-token")
    ws = prepare_workspace(_candidate(tmp_path))
    r = run_command(
        "python3 -c \"import os; print(sorted(os.environ))\"",
        cwd=ws, timeout_s=10)
    for leaked in ("NEBIUS", "TAVILY", "SIGNING", "SOME_RANDOM_TOKEN"):
        assert leaked not in r.stdout_tail
    assert "VERIFYGATE_SANDBOX" in r.stdout_tail  # the marker is set
    assert "PATH" in r.stdout_tail  # the allowlist still works


def test_sanitized_env_is_allowlist_only():
    env = sanitized_env({"PATH": "/bin", "AWS_SECRET_ACCESS_KEY": "x",
                         "LC_ALL": "C", "HOME": "/home/x"})
    assert env == {"PATH": "/bin", "LC_ALL": "C", "HOME": "/home/x",
                   "VERIFYGATE_SANDBOX": "1"}


def test_external_file_symlink_is_dropped(tmp_path):
    outside = tmp_path / "host-secret.txt"
    outside.write_text("host only")
    cand = _candidate(tmp_path)
    (cand / "escape.txt").symlink_to(outside)
    ws = prepare_workspace(cand)
    assert (ws / "real.txt").exists()
    assert not (ws / "escape.txt").exists()
    assert not (ws / "escape.txt").is_symlink()


def test_external_directory_symlink_is_not_followed(tmp_path):
    outside_dir = tmp_path / "host-dir"
    outside_dir.mkdir()
    (outside_dir / "loot.txt").write_text("loot")
    cand = _candidate(tmp_path)
    (cand / "dirlink").symlink_to(outside_dir, target_is_directory=True)
    ws = prepare_workspace(cand)
    assert not (ws / "dirlink").exists()


def test_internal_file_symlink_is_copied_as_regular_file(tmp_path):
    cand = _candidate(tmp_path)
    (cand / "alias.txt").symlink_to(cand / "real.txt")
    ws = prepare_workspace(cand)
    assert (ws / "alias.txt").is_file()
    assert not (ws / "alias.txt").is_symlink()
    assert (ws / "alias.txt").read_text() == "real content"


def test_tree_hash_ignores_external_symlink(tmp_path):
    cand = _candidate(tmp_path)
    before = tree_hash(cand)
    outside = tmp_path / "host.txt"
    outside.write_text("host")
    (cand / "escape.txt").symlink_to(outside)
    assert tree_hash(cand) == before


def test_candidate_files_snapshot_skips_external_symlink(tmp_path):
    cand = _candidate(tmp_path)
    (cand / "code.py").write_text("x = 1\n")
    outside = tmp_path / "host.py"
    outside.write_text("SECRET = 1\n")
    (cand / "leak.py").symlink_to(outside)
    files = _candidate_files(cand)
    assert "code.py" in files
    assert "leak.py" not in files


def test_output_is_bounded_regardless_of_volume(tmp_path):
    ws = prepare_workspace(_candidate(tmp_path))
    r = run_command(
        "python3 -c \"print('x' * 300000)\"", cwd=ws, timeout_s=20)
    assert r.exit_code == 0
    assert len(r.stdout_tail) <= MAX_OUTPUT_CHARS + 32
    assert "truncated" in r.stdout_tail
    assert r.stdout_tail.rstrip().endswith("x")


def test_timeout_kills_whole_process_group(tmp_path):
    ws = prepare_workspace(_candidate(tmp_path))
    marker = tmp_path / "child-survived"
    # The check spawns a detached child that would write a marker at
    # t+3s; the group kill at t=1s must prevent that entirely.
    cmd = (
        "python3 -c \"import subprocess, sys; "
        "subprocess.Popen([sys.executable, '-c', "
        f"\\\"import time, pathlib; time.sleep(3); "
        f"pathlib.Path('{marker}').write_text('x')\\\"]); "
        "import time; time.sleep(30)\""
    )
    start = time.monotonic()
    r = run_command(cmd, cwd=ws, timeout_s=1)
    assert r.timed_out is True
    assert time.monotonic() - start < 5
    time.sleep(3.5)  # past the child's scheduled write
    assert not marker.exists()
