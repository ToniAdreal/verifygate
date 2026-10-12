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
* **Candidate directory validation** — every entry point validates
  the candidate directory itself through
  :func:`validate_candidate_dir` before any workspace is prepared,
  any check runs, or any tree is hashed (v1.41.0). Before this, the
  parameter was converted with ``Path(...)`` unchecked: a non-path
  value (``None``, an ``int``, a list, ``bytes``) crashed with an
  unhandled ``TypeError`` — at different depths on different paths
  (the runner before its checks, replay mid-replay, past its
  ``OSError``/``RuntimeError`` handling, and ``verify``'s candidate
  binding inside ``tree_hash``); an empty string silently became
  the *current* directory (``Path("")`` is ``"."``), so a run
  verified whatever tree the caller happened to stand in and sealed
  its hash under an unrelated candidate label, while ``verify``
  hashed the same wrong tree and reported a binding mismatch
  instead of a usage error; and a regular file raised
  ``FileNotFoundError`` from the workspace copy but
  ``NotADirectoryError`` from the tree hash. A candidate must now
  be a non-empty ``str``/``Path`` of at most
  :data:`MAX_CANDIDATE_DIR_CHARS` characters naming an existing
  directory: a malformed value is a ``ValueError`` and a missing /
  not-a-directory value the one ``FileNotFoundError`` every path
  already produced, both before any work happens (CLI exit 2).
* **Traversal-root validation** — :func:`iter_candidate_files`,
  the single traversal behind the workspace copy, the tree hash,
  and the repair loop's prompt snapshot, validates its own
  ``root`` through :func:`validate_candidate_dir`, eagerly at
  call time (v1.58.0). Before this it coerced the root with a
  bare ``Path(...)``: every production caller happened to
  validate first, but a direct caller got a raw ``TypeError``
  for a non-path root, an empty string silently traversing the
  current directory, and a ``NotADirectoryError`` for a regular
  file instead of the one ``FileNotFoundError``.
* **Check-execution argument validation** — :func:`run_command`,
  the one execution primitive, validates its own arguments
  (v1.51.0): the ``command`` must be a non-empty string (whitespace
  only rejected) within the spec's command ceiling, ``cwd`` must
  name an existing directory (through
  :func:`validate_candidate_dir`), and ``timeout_s`` must be a real
  ``int`` (never ``bool``) between 1 and the spec's timeout ceiling
  — exactly the rules ``spec.parse_spec`` / ``validate_spec``
  enforce on the JSON and object paths. Before this, those rules
  bound a check only transitively, through a validated spec: a
  direct caller of the execution layer (the layer the test suite
  itself drives) got a ``TypeError`` traceback for a non-string
  command, a vacuous exit-0 result for an empty/whitespace command
  (the v1.7.0 vacuous-PASS shape, one layer down), a ``TypeError``
  for a string timeout, a silently-one-second ``True``, an
  unbounded ``None`` wait, an instant timeout verdict for ``0``,
  a float timeout the spec rules forbid, and stringified-path
  nonsense (``cwd=123`` looked for a directory literally named
  ``"123"``; a file raised ``NotADirectoryError`` instead of the
  one ``FileNotFoundError`` the candidate-directory rule promises).
  A malformed argument is now a ``ValueError`` before any process
  is spawned. ``validators.run_checks`` validates its ``spec``
  (``validate_spec``) and ``workspace`` arguments the same way, at
  its own entry, instead of trusting that the runner validated
  them first.
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
* **Bound-override validation** — the per-call ``max_files`` /
  ``max_bytes`` overrides of :func:`check_candidate_bounds` (and,
  through the shared :func:`validate_candidate_bound` helper, of
  ``patch.apply_patch``) are validated (v1.53.0): each must be a
  real ``int`` (never ``bool``) between 0 and the module ceiling
  in force at call time — an override may only *tighten* a bound,
  never loosen it past the ceiling it overrides. Before this, the
  overrides were taken on trust: ``True`` silently ran as a
  one-file bound, a float was compared as-is, a string crashed
  with an unhandled ``TypeError`` (inside ``apply_patch`` that
  ``TypeError`` is not a ``PatchError``, so it escaped the repair
  loop's rejection handling exactly like the v1.11.0 binary-target
  escape), a negative bound produced a nonsense
  ``CandidateTooLargeError`` naming a negative maximum, and an
  enormous override (``10**9`` files / ``10**12`` bytes) silently
  disabled the v1.23.0 / v1.25.0 ceilings for that call. A
  malformed override is now one ``ValueError`` before any file is
  counted, staged, or written. The same rule closes the last two
  numeric limits taken on trust: ``jsonio.load_json_file``'s
  ``max_bytes`` (a string crashed in ``max_bytes + 1``, a float /
  ``None`` in ``read()``, ``True`` named itself in the error) and
  ``tavily.normalise_references``' ``limit`` (a string / ``None``
  crashed the comparison, ``True`` ran as 1, a huge value silently
  lifted the reference ceiling) — see those modules.

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

from .spec import MAX_CHECK_COMMAND_CHARS, MAX_TIMEOUT_S

MAX_OUTPUT_CHARS = 4000
_READ_BYTES = 16_384  # > MAX_OUTPUT_CHARS even for multibyte text

# Candidate size ceilings (see the module docstring): generous bounds
# in the same spirit as MAX_TIMEOUT_S (spec.py) and MAX_ROUNDS
# (repair.py) — a real code change is far below them, while a
# vendored dependency tree or a huge binary is stopped before the
# harness copies or hashes it into the temp filesystem.
MAX_CANDIDATE_FILES = 10_000
MAX_CANDIDATE_BYTES = 256 * 1024 * 1024  # 256 MiB

# Ceiling over a candidate directory's path string (v1.41.0): the
# repair loop stores the submitted candidate's path verbatim in
# every attempt wrapper, and verification caps that stored field
# at this same 1,000 characters (verify.MAX_CANDIDATE_DIR_CHARS
# imports this constant) — a longer path could only produce a
# report that fails its own verification. Ordinary paths (the
# longest committed one is a few dozen characters) sit far below.
MAX_CANDIDATE_DIR_CHARS = 1000


def validate_candidate_dir(candidate_dir, name: str = "candidate_dir") -> Path:
    """Return ``candidate_dir`` as a ``Path`` to an existing directory.

    Candidate-directory validation (v1.41.0, see the module
    docstring): the type/emptiness/length faults are ``ValueError``
    (with the offending value's repr truncated, exactly as
    ``evidence.validate_label`` truncates it, so a huge value
    cannot bloat the error line), and a well-formed path that does
    not name a directory is the ``FileNotFoundError``
    ``prepare_workspace`` has always raised — one message, one
    exception, on every entry point, instead of the tree hash's
    ``NotADirectoryError`` for the same file. ``bool`` is rejected
    explicitly: it is not an ``int`` subclass problem here, it is
    simply not a path, and naming it keeps the rule obvious.
    """
    if isinstance(candidate_dir, bool) \
            or not isinstance(candidate_dir, (str, Path)):
        desc = repr(candidate_dir)
        if len(desc) > 80:
            desc = desc[:77] + "..."
        raise ValueError(
            f"{name} must be a string or Path naming a directory, "
            f"got {desc}")
    if isinstance(candidate_dir, str) and not candidate_dir:
        raise ValueError(f"{name} must not be an empty string")
    if len(str(candidate_dir)) > MAX_CANDIDATE_DIR_CHARS:
        raise ValueError(
            f"{name} must be at most {MAX_CANDIDATE_DIR_CHARS} "
            f"characters, got a {len(str(candidate_dir))}-character path")
    path = Path(candidate_dir)
    try:
        is_dir = path.is_dir()
    except OSError:
        # A path that cannot even be stat'ed — a component over
        # the filesystem's name limit raises ENAMETOOLONG out of
        # is_dir() on some Python versions instead of returning
        # False — does not name a directory either (the same
        # unstatable-target rule patch.py applies).
        is_dir = False
    if not is_dir:
        raise FileNotFoundError(
            f"candidate directory not found: {candidate_dir}")
    return path


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

    Traversal-root validation (v1.58.0): ``root`` is validated
    through :func:`validate_candidate_dir` *eagerly*, at call time —
    this function returns an iterator, but a malformed root is a
    ``ValueError`` from the call itself, not a surprise deferred to
    the first iteration, and a root naming no existing directory is
    the one ``FileNotFoundError``. Before this, the traversal — the
    one primitive every consumer of a candidate tree passes
    through — coerced its root with a bare ``Path(...)``: a non-path
    root crashed with a raw ``TypeError`` from ``Path``, an empty
    string silently traversed the *current directory* (``Path("")``
    is ``"."``), and a regular file raised ``NotADirectoryError``
    out of ``os.scandir`` instead of the ``FileNotFoundError`` every
    other path raises. Every production caller happened to validate
    first; the primitive now judges its own root, exactly as
    ``run_command`` judges its ``cwd`` (v1.51.0) and
    ``apply_patch`` its root (v1.57.0).
    """
    root = validate_candidate_dir(root, name="root")
    return _iter_candidate_files(root)


def _iter_candidate_files(root: Path):
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


def validate_candidate_bound(value, name: str, ceiling: int) -> int:
    """Return a per-call candidate bound that is safe to enforce.

    Bound-override validation (v1.53.0, see the module docstring):
    a bound override is a real ``int`` — never ``bool`` (``True``
    used to slip through as a one-file / one-byte bound) and never
    a float or a string (both compared as-is before: the float
    silently, the string as an unhandled ``TypeError``) — between
    0 and ``ceiling``, the module ceiling in force at call time.
    An override may tighten a bound for one call; it may never
    loosen it past the ceiling, so an enormous override cannot
    silently disable the candidate-size protection. Anything else
    is a ``ValueError`` before any work happens.
    """
    if isinstance(value, bool) or not isinstance(value, int) \
            or not 0 <= value <= ceiling:
        raise ValueError(
            f"{name} must be an int with 0 <= {name} <= {ceiling} "
            f"(got {type(value).__name__})")
    return value


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
    constants). A supplied override is validated through
    :func:`validate_candidate_bound` (v1.53.0) before the
    candidate directory itself is judged: it must be a real
    ``int`` between 0 and the ceiling in force — an override
    tightens a bound, it never loosens it.
    """
    if max_files is None:
        max_files = MAX_CANDIDATE_FILES
    else:
        max_files = validate_candidate_bound(
            max_files, "max_files", MAX_CANDIDATE_FILES)
    if max_bytes is None:
        max_bytes = MAX_CANDIDATE_BYTES
    else:
        max_bytes = validate_candidate_bound(
            max_bytes, "max_bytes", MAX_CANDIDATE_BYTES)
    validate_candidate_dir(root)
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
    candidate_dir = validate_candidate_dir(candidate_dir)
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


def validate_check_command(command, name: str = "command") -> str:
    """Return a check ``command`` that is safe to execute (v1.51.0).

    Mirrors the spec rules for a check command exactly (see the
    module docstring): a string, non-empty once stripped
    (whitespace-only rejected — the shell exits 0 on empty input,
    so an empty command is a vacuous PASS, not a check), and at
    most ``MAX_CHECK_COMMAND_CHARS`` characters. Anything else is
    a ``ValueError`` before any process is spawned.
    """
    if not isinstance(command, str) or not command.strip():
        raise ValueError(
            f"{name} must be a non-empty string "
            f"(got {type(command).__name__})")
    if len(command) > MAX_CHECK_COMMAND_CHARS:
        raise ValueError(
            f"{name} must be at most {MAX_CHECK_COMMAND_CHARS} "
            f"characters (got {len(command)})")
    return command


def validate_check_timeout(timeout_s, name: str = "timeout_s") -> int:
    """Return a check ``timeout_s`` that is safe to wait on (v1.51.0).

    Mirrors the spec rule for a check timeout exactly: a real
    ``int`` — never ``bool`` (``True`` used to slip through as a
    one-second wait) and never a float (the spec forbids float
    timeouts; the execution layer no longer accepts what the spec
    layer rejects) — between 1 and ``MAX_TIMEOUT_S``. ``None``
    (an unbounded wait), ``0`` / negatives (an instant timeout
    verdict for a check that never ran), strings, and containers
    are a ``ValueError`` before any process is spawned.
    """
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, int) \
            or not 1 <= timeout_s <= MAX_TIMEOUT_S:
        raise ValueError(
            f"{name} must be an int with 1 <= {name} <= "
            f"{MAX_TIMEOUT_S} (got {type(timeout_s).__name__})")
    return timeout_s


def run_command(command: str, cwd: Path, timeout_s: int) -> CommandResult:
    # The execution primitive validates its own arguments
    # (v1.51.0, see the module docstring): until now they were
    # bounded only transitively, through a validated spec, and a
    # direct caller met TypeErrors, vacuous exit-0 results, and
    # unbounded waits instead of one ValueError before any work.
    command = validate_check_command(command)
    cwd = validate_candidate_dir(cwd, name="cwd")
    timeout_s = validate_check_timeout(timeout_s)
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
