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
* **Permission fidelity** — the workspace copy preserves each file's
  permission bits (``shutil.copymode`` after the content copy). Before
  v1.9.0 the copy was content-only, so an executable script arrived
  non-executable and a check invoking it directly (``./check.sh``)
  failed with exit 126 inside VerifyGate while passing on disk —
  the sandbox judged a different tree than the one submitted. The
  reverse also holds: a non-executable file is never made executable.
* **Candidate size bounds** — a candidate may carry at most
  :data:`MAX_CANDIDATE_FILES` verified files totalling at most
  :data:`MAX_CANDIDATE_BYTES` bytes (v1.23.0). Before this, the copy,
  the tree hash, and every repair round re-copied the candidate with
  no bound at all: a candidate carrying a vendored ``node_modules``
  tree, build output, or one huge binary was copied in full, once
  per verification and once per repair round, into a temp filesystem
  that this project has already filled in practice (the v1.0.0
  Errno 28 incident). The bounds are checked *before* any copy is
  made (a rejected candidate leaves no workspace behind) and in
  ``evidence.tree_hash``, so every entry point — run, repair,
  verify's candidate binding, replay — rejects the same oversized
  candidate with the same one-line usage error instead of copying
  or hashing it first. The bounds count exactly the files the
  containment traversal would verify (ignored names and escaping
  symlinks excluded), and they are generous ceilings, not targets:
  the bundled samples are three orders of magnitude below them.

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

# Candidate size ceilings (see the module docstring): generous bounds
# in the same spirit as MAX_TIMEOUT_S (spec.py) and MAX_ROUNDS
# (repair.py) — a real code change is far below them, while a
# vendored dependency tree or a huge binary is stopped before the
# harness copies or hashes it into the temp filesystem.
MAX_CANDIDATE_FILES = 10_000
MAX_CANDIDATE_BYTES = 256 * 1024 * 1024  # 256 MiB


class CandidateTooLargeError(ValueError):
    """Raised when a candidate exceeds the size bounds above.

    A ``ValueError`` so every CLI entry point reports it through its
    existing usage-error path (one line, exit 2), never a traceback.
    """
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


def check_candidate_bounds(
    root: str | Path,
    *,
    max_files: int | None = None,
    max_bytes: int | None = None,
) -> tuple[int, int]:
    """Count the candidate's verified files/bytes; reject oversize.

    Returns ``(file_count, total_bytes)`` for an in-bounds candidate
    and raises :class:`CandidateTooLargeError` as soon as a bound is
    exceeded (the scan stops there — an oversized candidate is not
    walked to the end just to be rejected). Only the files
    :func:`iter_candidate_files` yields are counted: exactly the set
    the workspace copy, tree hash, and repair snapshot operate on.
    ``max_files`` / ``max_bytes`` default to the module ceilings at
    call time (tests may override them per call or monkeypatch the
    constants).
    """
    if max_files is None:
        max_files = MAX_CANDIDATE_FILES
    if max_bytes is None:
        max_bytes = MAX_CANDIDATE_BYTES
    count = 0
    total = 0
    for _rel, source in iter_candidate_files(root):
        count += 1
        if count > max_files:
            raise CandidateTooLargeError(
                f"candidate exceeds the maximum of {max_files} files "
                f"(more than {max_files} verified files under {root})")
        total += source.stat().st_size
        if total > max_bytes:
            raise CandidateTooLargeError(
                f"candidate exceeds the maximum total size of "
                f"{max_bytes} bytes (verified files under {root} "
                f"pass that bound)")
    return count, total


def prepare_workspace(candidate_dir: str | Path) -> Path:
    """Copy the candidate into a fresh temp dir and return its path.

    Empty real directories are preserved; symlinks follow the
    containment rule in :func:`iter_candidate_files`.

    Ownership: the caller owns the returned directory and must remove
    it when done. ``run_verification`` removes its internal workspace
    before returning; the repair loop removes rejected and superseded
    copies and keeps only the final patched copy for its caller.
    Leaking these copies once filled a temp filesystem in practice
    (see tests/test_workspace_cleanup.py).
    """
    candidate_dir = Path(candidate_dir)
    if not candidate_dir.is_dir():
        raise FileNotFoundError(f"candidate directory not found: {candidate_dir}")
    # Pre-flight: an oversized candidate is rejected before any copy
    # exists, so the rejection itself cannot fill the temp filesystem
    # it protects (and leaves no workspace behind for the caller to
    # clean up).
    check_candidate_bounds(candidate_dir)
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
        # copyfile copies content only: without this the workspace
        # copy of an executable script is not executable, and a check
        # invoking it directly fails in the sandbox while passing on
        # disk. Preserve the source's permission bits exactly.
        shutil.copymode(source, target)
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
