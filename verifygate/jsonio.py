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
"""

from __future__ import annotations

import json
from pathlib import Path

MAX_SPEC_FILE_BYTES = 4 * 1024 * 1024  # 4 MiB
MAX_RECORD_FILE_BYTES = 64 * 1024 * 1024  # 64 MiB


class JsonFileError(ValueError):
    """A JSON input file is oversize, undecodable, or malformed."""


def load_json_file(path: str | Path, *, max_bytes: int, kind: str = "JSON"):
    """Load and decode a JSON file, rejecting it above ``max_bytes``.

    ``kind`` names the document in error messages ("spec",
    "record"). Raises :class:`JsonFileError` for an oversize file,
    non-UTF-8 content, or malformed JSON; ``OSError`` propagates
    for a missing/unreadable file, so callers keep their existing
    handling for both.
    """
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
