"""Bounded JSON file input (v1.36.0).

Every JSON document VerifyGate reads from disk — a task spec, an
evidence record, a repair report — is an *untrusted* input: records
in particular are third-party artifacts that ``verify`` / ``view`` /
``replay`` exist to judge. Before this module, every loader read its
file in full and handed the whole text to ``json.loads`` with no
size bound at all, while every *field* inside those documents had
already been capped (spec text and check count, candidate files
and bytes, check tails, diagnosis text, usage calls, report
shape…). A 5 MiB padded spec ran to PASS and a 70 MiB padded record
verified INTACT in probes against v1.35.0; a genuinely hostile file
(gigabytes of whitespace-padded JSON, or a sparse-looking bomb)
would exhaust memory in the loader, before any of the field-level
checks could run — the verifier crashing on the artifact it was
asked to judge, instead of rejecting it with the CLI contract's
clean usage error (exit 2).

The ceilings are generous, not targets — derived from the largest
document the harness's own field caps can produce, with wide
headroom:

* :data:`MAX_SPEC_FILE_BYTES` (4 MiB): a spec at every field
  ceiling (200-char name, 10,000-char brief, 100 checks of a
  200-char name and a 4,000-char command) serialises to well under
  1 MiB even with JSON string escaping; the bundled specs are
  under 300 bytes.
* :data:`MAX_RECORD_FILE_BYTES` (64 MiB): a single record at every
  field ceiling is a few MiB; a repair report embeds at most
  ``MAX_ROUNDS + 1`` records plus their patch linkages. The
  largest committed report is ~13 KiB.

:func:`load_json_file` enforces the bound *while reading*: it
reads at most ``max_bytes + 1`` bytes, so an oversize file is
rejected from its first bytes rather than after being loaded into
memory, and the rejection is a :class:`JsonFileError` (a
``ValueError``) naming the file size limit — which the spec loader
re-raises as ``SpecError`` and the CLI maps to its one-line
``record error`` / exit 2, exactly like a malformed-JSON file.

File-path validation (v1.46.0): the *path* itself was the one
argument the loaders still converted unchecked. ``load_json_file``
handed it straight to ``open()``, which treats an ``int`` as a raw
file descriptor — a library caller passing ``1`` would have the
loader read from (and, via the context manager, *close*) that
descriptor — and ``spec.load_spec`` converted with ``Path(...)``
outside its error handling, so ``None``, an ``int``, a list, a
float, a bool, or a bytes value escaped as an unhandled
``TypeError`` traceback instead of the ``SpecError`` every other
malformed spec produces. Both loaders now validate the path
first, through :func:`validate_file_path`: a non-empty ``str`` or
a path-like whose ``__fspath__`` returns a non-empty ``str`` is
accepted and returned as a :class:`~pathlib.Path`; anything else
is a :class:`JsonFileError` naming the argument — which
``load_spec`` re-raises as ``SpecError``, so every malformed path
is the same one-line usage error (CLI exit 2) as every other
malformed input. The candidate *directory* got this treatment in
v1.41.0; the spec/record *files* were left.

Bound-override validation (v1.53.0): the loader's own
``max_bytes`` was the last numeric limit taken on trust. A
string crashed in ``max_bytes + 1`` and a float / ``None`` in
``read()`` — unhandled ``TypeError`` tracebacks instead of the
one :class:`JsonFileError` every other malformed input produces;
``True`` was accepted as a one-byte bound and named itself in
the error message ("exceeds the maximum of True bytes"); a
negative bound rejected every file with a nonsense maximum;
and an enormous value silently lifted the v1.36.0 file bound
for that call. ``max_bytes`` must now be a real ``int`` (never
``bool``) between 1 and :data:`MAX_RECORD_FILE_BYTES` — the
largest ceiling any caller legitimately passes — validated
before the path is even opened, as a :class:`JsonFileError`.

Special-file rejection (v1.73.0): the loader opened whatever
path it was given, so a path naming a *special file* — a FIFO,
a device, a socket, or a directory — was not rejected before
``open()``. A directory at least failed promptly with
``IsADirectoryError``, but a FIFO blocks ``open()`` / ``read()``
until a writer appears: ``verifygate verify --record <fifo>``
hung indefinitely, with no timeout and no output, on an artifact
the command exists to judge (the same indefinite-block shape a
check command reading stdin had before v1.69.0, and the same
special-file shape the candidate traversal has dropped since its
start — a candidate FIFO is skipped, never opened). The loader
now stats the path *before* opening it (following symlinks, so a
symlink to a regular file still loads) and rejects any existing
path that is not a regular file as a :class:`JsonFileError` —
the CLI's one-line record/spec error, exit 2. A missing path
keeps its established shape: the ``OSError`` from the stat
propagates, exactly as the ``OSError`` from ``open()`` did.

Special-file outputs (v1.74.0): the mirror image on the *write*
side was left. Every destination VerifyGate writes — an
evidence record (``runner.write_record``), a repair report,
a rendered HTML view — was written with a bare
``Path.write_text``, which opens whatever path it is given.
A FIFO destination blocked the write indefinitely, exactly
like the FIFO input did before v1.73.0: the run had finished,
its verdict was decided, and the CLI hung with no output at
all. A device destination was worse in the other direction:
writing a record to ``/dev/null`` *succeeded*, so the CLI
reported ``VERDICT: PASS -> /dev/null`` for a record that
exists nowhere. :func:`write_text_file` is now the one write
path for those destinations: it validates the path through
:func:`validate_file_path`, stats it before opening
(following symlinks, so a symlink to a regular file still
writes), and rejects any existing destination that is not a
regular file as a :class:`JsonFileError` — the CLI's one-line
output error, exit 2. A destination that does not exist yet
is created as a regular file, as before; a directory keeps
its established shape (the write attempt raises
``IsADirectoryError``, an ``OSError``, promptly). The repair
loop's ``--patched-out`` copy destination is judged by the
same rule in the CLI *before* any check runs or any model
call is made: an existing destination that is not a
directory is an output error, instead of a repair that runs
to completion and only then fails at the copy.

Nesting-depth bound (v1.75.0): the decoder itself was the
last unbounded recursion on the input path. ``json.loads``
decodes nested arrays/objects recursively, so a document
nested ~10,000 levels deep — a few tens of kilobytes, far
under the byte ceilings — died with an unhandled
``RecursionError`` traceback (exit 1) out of
:func:`load_json_file`, on every consumer at once:
``verify`` / ``view`` / ``replay`` on a record and ``run`` /
``repair`` on a spec. A document nested just under the
interpreter's limit was no better in principle: it parsed,
and every later recursive pass over it (canonical encoding,
rendering) ran one level from the same cliff. The loader now
converts a decoder ``RecursionError`` into the same
:class:`JsonFileError` as malformed JSON, and — defence in
depth, so no parsed document can take a later pass near the
cliff — rejects any document nested deeper than
:data:`MAX_JSON_DEPTH`, measured iteratively after decoding.
The ceiling is generous: the deepest genuine document (a
repair report: report → attempt → record → entry → usage →
call → usage) nests about ten levels.

Strict JSON decoding (v1.76.0): the loader's contract — every
malformed input is one :class:`JsonFileError` — had two holes
left in the decoder itself. (1) An integer literal longer than
the interpreter's conversion limit (4,300 digits) makes
``json.loads`` raise a plain ``ValueError`` that is *not* a
``JSONDecodeError``: it escaped the loader raw, and on the spec
path — whose loader re-raises only :class:`JsonFileError` and
``OSError`` as ``SpecError`` — the CLI died with an unhandled
traceback and exit 1 instead of its one-line spec error with
exit 2 (reproduced with a 6,000-digit timeout). (2) The decoder
accepted the non-standard constants ``NaN``, ``Infinity`` and
``-Infinity`` — Python extensions that are not JSON at all — so
a file no other JSON parser would accept loaded here. The
decoder now rejects both: oversize integer literals are the
same :class:`JsonFileError` as malformed JSON (naming only the
rule, never the literal), and the constants are rejected via
``parse_constant``. A numeric literal that overflows float
range (``1e999``) decodes to ``inf`` without passing through
``parse_constant`` at all, so the decoded document is also
scanned — iteratively — for a non-finite float. No genuine
document is affected: every document VerifyGate writes is
standard JSON with small ints and no floats.
"""

from __future__ import annotations

import json
import math
import os
import stat
from pathlib import Path

MAX_SPEC_FILE_BYTES = 4 * 1024 * 1024  # 4 MiB
MAX_RECORD_FILE_BYTES = 64 * 1024 * 1024  # 64 MiB
MAX_JSON_DEPTH = 100  # containers; genuine documents nest ~10


class JsonFileError(ValueError):
    """A JSON input file is oversize, undecodable, or malformed."""


def validate_file_path(path, name: str = "path") -> Path:
    """Return ``path`` as a :class:`Path`, or raise JsonFileError.

    File-path validation (v1.46.0, see the module docstring): a
    path is a non-empty string, or a path-like object whose
    ``__fspath__`` returns one (``Path`` itself qualifies). An
    ``int`` is never a path here — ``open()`` would treat it as a
    raw file descriptor, reading and closing a descriptor the
    caller never offered as a file — and ``None``, a bool, a
    container, a float, or a bytes value (whose ``__fspath__``
    form ``Path`` likewise rejects) is a usage error, not a
    ``TypeError`` from ``Path(...)`` / ``open(...)``. The error
    names the argument and the offending type only; a path string
    itself is never echoed (it can be arbitrarily long).
    """
    value = None
    if isinstance(path, str):
        value = path
    elif isinstance(path, os.PathLike):
        try:
            fspath = os.fspath(path)
        except TypeError:
            fspath = None
        if isinstance(fspath, str):
            value = fspath
    if not value:
        raise JsonFileError(
            f"{name} must be a non-empty file path string "
            f"(got {type(path).__name__})")
    return Path(value)


def validate_max_bytes(max_bytes, name: str = "max_bytes") -> int:
    """Return a ``max_bytes`` bound that is safe to enforce.

    Bound-override validation (v1.53.0, see the module docstring):
    a real ``int`` — never ``bool`` — between 1 and
    :data:`MAX_RECORD_FILE_BYTES`. Anything else is a
    :class:`JsonFileError` before any file is opened.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) \
            or not 1 <= max_bytes <= MAX_RECORD_FILE_BYTES:
        raise JsonFileError(
            f"{name} must be an int with 1 <= {name} <= "
            f"{MAX_RECORD_FILE_BYTES} (got {type(max_bytes).__name__})")
    return max_bytes


def load_json_file(path: str | Path, *, max_bytes: int, kind: str = "JSON"):
    """Load and decode a JSON file, rejecting it above ``max_bytes``.

    ``kind`` names the document in error messages ("spec",
    "record"). Raises :class:`JsonFileError` for an invalid path
    (v1.46.0 — validated before ``open()`` is ever reached, so an
    ``int`` is never mistaken for a file descriptor), an invalid
    ``max_bytes`` (v1.53.0 — validated before the path), an
    oversize file, non-UTF-8 content, malformed JSON (strictly
    decoded since v1.76.0: non-standard constants and oversize
    integer literals are the same rejection), or a
    document nested deeper than :data:`MAX_JSON_DEPTH`
    (v1.75.0 — a decoder ``RecursionError`` is the same
    rejection, never a traceback); ``OSError`` propagates for
    a missing/unreadable file, so callers keep their existing
    handling for both.
    """
    max_bytes = validate_max_bytes(max_bytes)
    path = validate_file_path(path, f"{kind} path")
    # Special-file rejection (v1.73.0, see the module docstring):
    # stat before open, following symlinks. An existing path that
    # is not a regular file (FIFO, device, socket, directory) is
    # a JsonFileError, never an open() that can block indefinitely
    # on a FIFO. A missing path raises OSError from the stat,
    # exactly the shape open() produced for it before.
    st = path.stat()
    if not stat.S_ISREG(st.st_mode):
        raise JsonFileError(
            f"{kind} file is not a regular file: {path}")
    with open(path, "rb") as fh:
        raw = fh.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise JsonFileError(
            f"{kind} file exceeds the maximum of {max_bytes} bytes: {path}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JsonFileError(f"{kind} file is not UTF-8: {path}: {exc}") from exc
    def _reject_constant(name: str):
        # Strict JSON (v1.76.0, see the module docstring): NaN /
        # Infinity / -Infinity are Python extensions, not JSON;
        # the three names are fixed strings, safe to name here.
        raise JsonFileError(
            f"{kind} file is not valid JSON: non-standard constant "
            f"{name}: {path}")

    try:
        data = json.loads(text, parse_constant=_reject_constant)
    except JsonFileError:
        raise
    except json.JSONDecodeError as exc:
        raise JsonFileError(str(exc)) from exc
    except ValueError:
        # Strict JSON (v1.76.0): the decoder's remaining
        # ValueError is the integer-literal digit limit (an int
        # with more digits than the interpreter converts). It is
        # a plain ValueError, not a JSONDecodeError, so before
        # this it escaped the loader raw — and on the spec path,
        # whose loader re-raises only JsonFileError / OSError,
        # it crashed the CLI with a traceback. The rejection
        # names only the rule, never the literal.
        raise JsonFileError(
            f"{kind} file is not valid JSON: an integer literal "
            f"exceeds the maximum number of digits: {path}") from None
    except RecursionError:
        # Nesting-depth bound (v1.75.0, see the module docstring):
        # the decoder recursed past the interpreter limit. The
        # stack has unwound by the time this handler runs, and
        # the rejection names only the ceiling — never the text.
        raise JsonFileError(
            f"{kind} file exceeds the maximum nesting depth of "
            f"{MAX_JSON_DEPTH}: {path}") from None
    if _json_depth(data) > MAX_JSON_DEPTH:
        raise JsonFileError(
            f"{kind} file exceeds the maximum nesting depth of "
            f"{MAX_JSON_DEPTH}: {path}")
    if _has_nonfinite_float(data):
        # Strict JSON (v1.76.0): a numeric literal that overflows
        # float range (1e999) decodes to inf without ever passing
        # through parse_constant; a decoded document carrying a
        # non-finite float holds a value JSON cannot represent.
        raise JsonFileError(
            f"{kind} file is not valid JSON: a numeric literal is "
            f"out of range: {path}")
    return data


def _has_nonfinite_float(data) -> bool:
    """Whether decoded JSON carries an inf/nan float (iterative)."""
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, float):
            if not math.isfinite(node):
                return True
        elif isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return False


def _json_depth(data) -> int:
    """Return the container nesting depth of decoded JSON.

    Scalars nest 0; a container nests one more than its deepest
    child (an empty container nests 1). Measured iteratively —
    measuring a hostile document must not itself recurse
    (v1.75.0).
    """
    depth = 0
    stack = [(data, 0)]
    while stack:
        node, level = stack.pop()
        if isinstance(node, dict):
            level += 1
            depth = max(depth, level)
            stack.extend((child, level) for child in node.values())
        elif isinstance(node, list):
            level += 1
            depth = max(depth, level)
            stack.extend((child, level) for child in node)
    return depth


def validate_output_path(path, name: str = "out_path") -> Path:
    """Return ``path`` as a writable destination, or raise.

    Special-file outputs (v1.74.0, see the module docstring):
    the path itself is validated through
    :func:`validate_file_path`; an *existing* destination is
    then statted, following symlinks. A regular file may be
    overwritten; a missing destination (including a broken
    symlink, whose target the write will create) is accepted
    and will be created as a regular file; a directory is
    accepted here so the write itself raises the established
    ``IsADirectoryError``. Anything else that already exists
    — a FIFO, a device, a socket — is a
    :class:`JsonFileError`: a FIFO write can block
    indefinitely and a device write can silently discard the
    document while reporting success. Other ``OSError``
    exceptions from the stat (a symlink loop, an unstatable component)
    propagate, exactly as they would from the write itself.
    """
    path = validate_file_path(path, name)
    try:
        st = path.stat()
    except FileNotFoundError:
        return path
    if stat.S_ISDIR(st.st_mode):
        return path
    if not stat.S_ISREG(st.st_mode):
        raise JsonFileError(
            f"{name} is not a regular file: {path}")
    return path


def write_text_file(path, text: str, *, name: str = "out_path") -> Path:
    """Write ``text`` (UTF-8) to ``path``; return the path.

    The one write path for VerifyGate's file destinations
    (v1.74.0): the destination is judged by
    :func:`validate_output_path` *before* anything is opened,
    so a special-file destination is a clean
    :class:`JsonFileError` (a ``ValueError``), never a blocked
    or silently-discarding write. Genuine filesystem faults
    (a missing parent directory, a directory destination)
    propagate as ``OSError``, as on every other write path.
    """
    path = validate_output_path(path, name)
    path.write_text(text, encoding="utf-8")
    return path
