"""Sandboxed execution of check commands — hardened (Oct 18–21 milestone).

A candidate directory is copied into a fresh temporary workspace and each
check command runs there. The candidate on disk is never modified, and a
hung check cannot hang the harness. Hardening rules, each pinned by tests:

* **Environment scrubbing** — check commands run with an allowlisted
  environment only (``PATH``, locale, ``HOME``, … plus a
  ``VERIFYGATE_SANDBOX=1`` marker). API keys, signing keys, tokens and
  every other host variable never reach candidate code, which is
  untrusted by definition.
* **Symlink containment** — the workspace copy never follows a symlink
  out of the candidate tree. A file symlink pointing *inside* the
  candidate is copied as a regular file; a symlink pointing outside is
  dropped entirely, and directory symlinks are never followed. The
  same rule drives ``evidence.tree_hash`` and the repair loop's
  candidate snapshot, so what is hashed and what is shown to the LLM
  is exactly what was verified — never a host file reached through a
  link.
* **Bounded output** — check stdout/stderr go to temp files, not
  memory; only a capped tail is read back, so a check printing
  gigabytes cannot OOM the harness.
* **Timeout kills the process group** — checks start in a new session
  and a timeout SIGKILLs the whole group, so a check cannot leave a
  detached child running after it has been failed.

This is a local/CI-grade sandbox (temp dir + timeout + scrubbed env),
not a multi-tenant security boundary — see CONCEPT.md risks.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

MAX_OUTPUT_CHARS = 4000
_READ_BYTES = 16_384  # > MAX_OUTPUT_CHARS even for multibyte text
_IGNORE = shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "*.pyc")
_IGNORED_NAMES = {".git", "__pycache__", ".pytest_cache"}

_ENV_ALLOWLIST = {
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TZ", "LANG", "TMPDIR",
}
_ENV_ALLOW_PREFIXES = ("LC_",)


@dataclass
class CommandResult:
    command: str
    exit_code: int | None  # None when the command never completed (timeout)
    duration_ms: int
    stdout_tail: str
    stderr_tail: str
    timed_out: bool = False


def sanitized_env(base: dict | None = None) -> dict:
    """Return the allowlisted environment check commands may see."""
    source = os.environ if base is None else base
    env = {
        k: v for k, v in source.items()
        if k in _ENV_ALLOWLIST or k.startswith(_ENV_ALLOW_PREFIXES)
    }
    env["VERIFYGATE_SANDBOX"] = "1"
    return env


def _tail(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return "…[truncated]…\n" + text[-MAX_OUTPUT_CHARS:]


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def iter_candidate_files(root: str | Path):
    """Yield ``(relative_path, absolute_source_path)`` for verified files.

    The single traversal rule shared by the workspace copy, the tree
    hash, and the repair loop's prompt snapshot: real directories are
    descended into (housekeeping names skipped); a file symlink is
    followed only when its resolved target stays inside ``root``;
    directory symlinks and links pointing outside are never followed.
    """
    root = Path(root)
    root_resolved = root.resolve()

    def walk(directory: Path, prefix: str):
        for entry in sorted(os.scandir(directory), key=lambda e: e.name):
            if entry.name in _IGNORED_NAMES or entry.name.endswith(".pyc"):
                continue
            rel = f"{prefix}{entry.name}"
            if entry.is_symlink():
                target = Path(entry.path).resolve()
                if _within(target, root_resolved) and target.is_file():
                    yield rel, target
                continue  # outside link or dir link: dropped
            if entry.is_dir(follow_symlinks=False):
                yield from walk(Path(entry.path), f"{rel}/")
            elif entry.is_file(follow_symlinks=False):
                yield rel, Path(entry.path)

    yield from walk(root, "")


def prepare_workspace(candidate_dir: str | Path) -> Path:
    """Copy the candidate into a fresh temp dir and return its path.

    Empty real directories are preserved; symlinks follow the
    containment rule in :func:`iter_candidate_files`.
    """
    candidate_dir = Path(candidate_dir)
    if not candidate_dir.is_dir():
        raise FileNotFoundError(f"candidate directory not found: {candidate_dir}")
    ws = Path(tempfile.mkdtemp(prefix="verifygate-"))
    # Recreate the real directory skeleton (incl. empty dirs), without
    # ever descending through a symlink.
    def make_dirs(directory: Path, dest: Path):
        for entry in sorted(os.scandir(directory), key=lambda e: e.name):
            if entry.name in _IGNORED_NAMES or entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                sub = dest / entry.name
                sub.mkdir(exist_ok=True)
                make_dirs(Path(entry.path), sub)

    make_dirs(candidate_dir, ws)
    for rel, source in iter_candidate_files(candidate_dir):
        target = ws / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return ws


def _read_tail(f) -> str:
    f.seek(0, os.SEEK_END)
    size = f.tell()
    f.seek(max(0, size - _READ_BYTES))
    return _tail(f.read().decode("utf-8", "replace"))


def run_command(command: str, cwd: Path, timeout_s: int) -> CommandResult:
    start = time.monotonic()
    timed_out = False
    exit_code: int | None = None
    with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cwd),
            stdout=out_f,
            stderr=err_f,
            env=sanitized_env(),
            start_new_session=True,
        )
        try:
            exit_code = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            proc.wait()
            exit_code = None
        duration = int((time.monotonic() - start) * 1000)
        return CommandResult(
            command=command,
            exit_code=exit_code,
            duration_ms=duration,
            stdout_tail=_read_tail(out_f),
            stderr_tail=_read_tail(err_f),
            timed_out=timed_out,
        )
