"""Sandboxed execution of check commands.

A candidate directory is copied into a fresh temporary workspace and each
check command runs there with a hard timeout and capped output. The
candidate on disk is never modified, and a hung check cannot hang the
harness. This is a local/CI-grade sandbox (temp dir + timeout), not a
multi-tenant security boundary — see CONCEPT.md risks.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

MAX_OUTPUT_CHARS = 4000
_IGNORE = shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "*.pyc")


@dataclass
class CommandResult:
    command: str
    exit_code: int | None  # None when the command never completed (timeout)
    duration_ms: int
    stdout_tail: str
    stderr_tail: str
    timed_out: bool = False


def _tail(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return "…[truncated]…\n" + text[-MAX_OUTPUT_CHARS:]


def prepare_workspace(candidate_dir: str | Path) -> Path:
    """Copy the candidate into a fresh temp dir and return its path."""
    candidate_dir = Path(candidate_dir)
    if not candidate_dir.is_dir():
        raise FileNotFoundError(f"candidate directory not found: {candidate_dir}")
    ws = Path(tempfile.mkdtemp(prefix="verifygate-"))
    shutil.copytree(candidate_dir, ws, dirs_exist_ok=True, ignore=_IGNORE)
    return ws


def run_command(command: str, cwd: Path, timeout_s: int) -> CommandResult:
    start = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        duration = int((time.monotonic() - start) * 1000)
        return CommandResult(
            command=command,
            exit_code=proc.returncode,
            duration_ms=duration,
            stdout_tail=_tail(proc.stdout or ""),
            stderr_tail=_tail(proc.stderr or ""),
        )
    except subprocess.TimeoutExpired as exc:
        duration = int((time.monotonic() - start) * 1000)
        return CommandResult(
            command=command,
            exit_code=None,
            duration_ms=duration,
            stdout_tail=_tail((exc.stdout or b"").decode("utf-8", "replace")
                              if isinstance(exc.stdout, bytes) else (exc.stdout or "")),
            stderr_tail=_tail((exc.stderr or b"").decode("utf-8", "replace")
                              if isinstance(exc.stderr, bytes) else (exc.stderr or "")),
            timed_out=True,
        )
