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
"""

from __future__ import annotations

import json
import os
from pathlib import Path

MAX_SPEC_FILE_BYTES = 4 * 1024 * 1024  # 4 MiB
MAX_RECORD_FILE_BYTES = 64 * 1024 * 1024  # 64 MiB


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


def load_json_file(path: str | Path, *, max_bytes: int, kind: str = "JSON"):
    """Load and decode a JSON file, rejecting it above ``max_bytes``.

    ``kind`` names the document in error messages ("spec",
    "record"). Raises :class:`JsonFileError` for an invalid path
    (v1.46.0 — validated before ``open()`` is ever reached, so an
    ``int`` is never mistaken for a file descriptor), an oversize
    file, non-UTF-8 content, or malformed JSON; ``OSError``
    propagates for a missing/unreadable file, so callers keep
    their existing handling for both.
    """
    path = validate_file_path(path, f"{kind} path")
    with open(path, "rb") as fh:
        raw = fh.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise JsonFileError(
            f"{kind} file exceeds the maximum of {max_bytes} bytes: {path}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JsonFileError(f"{kind} file is not UTF-8: {path}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise JsonFileError(str(exc)) from exc
