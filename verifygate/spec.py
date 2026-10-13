"""Task spec loading and validation.

A task spec is a small JSON document that states what a candidate code
change must prove before it may be delivered:

{
  "name": "stringtools-slugify-fix",
  "brief": "Fix slugify() so it lowercases and hyphenates correctly.",
  "checks": [
    {"name": "unit_tests",   "command": "python3 -m pytest -q", "timeout_s": 60},
    {"name": "repro_script", "command": "python3 repro.py",     "timeout_s": 30}
  ]
}

Checks are ordinary shell commands run inside a sandboxed copy of the
candidate. Exit code 0 = pass, anything else = fail. That is the whole
contract — deliberately boring, deliberately deterministic.

Validation is strict about types (hardened in v1.7.0 — before this,
the checks below were accepted and each broke something downstream):

* ``name`` / ``brief`` and every check ``name`` / ``command`` must be
  strings; a check name or command that is empty or all whitespace is
  rejected — a whitespace-only command is not a check at all: the
  shell exits 0 on it, so it would seal a *vacuous PASS*.
* A non-string ``command`` (e.g. a JSON number) used to pass the old
  truthiness test and then crash the runner with an unhandled
  ``TypeError`` traceback (exit 1), violating the CLI contract that
  usage errors exit 2 with a one-line message. Malformed specs now
  fail here, as :class:`SpecError`, before any check runs.
* ``timeout_s`` must be a real ``int`` — JSON ``true`` is an ``int``
  subclass in Python and used to be silently accepted as a 1-second
  timeout — between 1 and :data:`MAX_TIMEOUT_S` seconds, so a hung
  or absurd spec cannot hang the harness indefinitely.
* The number of checks is bounded at :data:`MAX_CHECKS` (v1.24.0).
  Per-check timeouts were capped, repair rounds were capped, and
  candidate size was capped, but a spec could still carry an
  unbounded number of checks: every check runs sequentially (each
  entitled to its full timeout, so a 5,000-check probe spec carried
  an aggregate timeout budget of ~208 days) and every check's result
  is sealed into the evidence record. A gate spec is a handful of
  commands by design (a whole test suite is one check); the ceiling
  is generous, and ``evidence``'s structure check mirrors it, so a
  record claiming more checks than a valid spec can carry fails
  verification too.
* The spec's *text* fields are bounded (v1.28.0): ``name`` and each
  check ``name`` at :data:`MAX_SPEC_NAME_CHARS` /
  :data:`MAX_CHECK_NAME_CHARS`, ``brief`` at
  :data:`MAX_BRIEF_CHARS`, and each check ``command`` at
  :data:`MAX_CHECK_COMMAND_CHARS`. Before this, every other
  dimension of a spec was capped but its strings were not: a probe
  spec carrying a 100,000-character name, a 500,000-character brief,
  and 100,000-character check names/commands was accepted verbatim.
  The name and the check names/commands are sealed into every
  evidence record the spec produces (header + one entry per check),
  and the brief is embedded verbatim in every real-mode patch prompt
  — a paid Token Factory call — so one oversized spec bloated every
  record and every prompt it touched. The ceilings are generous
  (the bundled specs' longest fields are a 148-character brief and
  20-character commands), and ``evidence``'s structure check mirrors
  the sealed fields' caps, so a record carrying a longer spec name
  or check name/command than a valid spec can produce fails
  verification too.
* The same rules are enforced on a constructed ``TaskSpec``
  object, not just on parsed JSON (v1.39.0): ``TaskSpec``/``Check``
  are plain dataclasses, so a library caller could bypass
  :func:`parse_spec` entirely — :func:`validate_spec` is called by
  the runner, the repair loop, and replay before any work, and it
  also requires ``raw`` to parse back to exactly the fields that
  will run, so the sealed ``spec_hash`` always binds them.
* The spec *file* itself is size-bounded on load (v1.36.0):
  :func:`load_spec` reads it through ``jsonio.load_json_file``
  with ``MAX_SPEC_FILE_BYTES`` (4 MiB — above what a spec at every
  field ceiling above serialises to), so a padded or hostile file
  is rejected as a ``SpecError`` from its first bytes instead of
  being loaded into memory in full before the field checks run.
* The spec file *path* is validated on the same load (v1.46.0):
  ``load_spec`` converted it with ``Path(...)`` outside its error
  handling, so a non-path value (``None``, an ``int``, a list, a
  float, a bool, bytes) escaped as an unhandled ``TypeError``
  instead of a ``SpecError`` — and the record loader's unchecked
  ``open()`` would have treated an ``int`` as a raw file
  descriptor. The path now goes through
  ``jsonio.validate_file_path`` first, inside the ``SpecError``
  mapping, exactly as the candidate directory is validated
  before any run (v1.41.0).
* Sealed and spawned text must be *encodable* (v1.97.0): a JSON
  ``\\uXXXX`` escape can decode to a lone surrogate — not valid
  Unicode, unencodable as UTF-8 — and a command string can carry
  a NUL byte, which no process can ever receive (commands cross
  the exec boundary as NUL-terminated strings). A spec name or
  check name carrying a lone surrogate was accepted and then
  crashed evidence sealing *after* the checks ran; a command
  carrying a surrogate crashed the spawn the same way; a command
  carrying a NUL byte crashed the spawn with ``ValueError:
  embedded null byte`` — in a multi-check spec, after the checks
  before it had already run, leaving no record for their decided
  verdicts. ``parse_spec`` and ``validate_spec`` now reject all
  three shapes up front (:func:`check_command_problem` states
  the command rule, shared with ``sandbox``); ``sandbox``
  mirrors the command rule at spawn time and ``evidence``
  mirrors it in the structure check, so a record claiming such
  a command fails verification too. A NUL byte in a *name* is
  still accepted: it is valid UTF-8 text, it seals and verifies
  like any other, and only the command crosses the spawn
  boundary.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .jsonio import (
    MAX_SPEC_FILE_BYTES,
    JsonFileError,
    has_lone_surrogates,
    load_json_file,
    validate_file_path,
)


MAX_TIMEOUT_S = 3600  # one hour: a sane ceiling for a single check
MAX_CHECKS = 100  # generous ceiling: a gate spec is a handful of commands
# Spec text ceilings (v1.28.0, see the module docstring): generous
# over every honest spec — the bundled specs peak at a 23-character
# name, a 148-character brief, 12-character check names, and
# 20-character commands.
MAX_SPEC_NAME_CHARS = 200
MAX_BRIEF_CHARS = 10_000
MAX_CHECK_NAME_CHARS = 200
MAX_CHECK_COMMAND_CHARS = 4_000


class SpecError(ValueError):
    """Raised when a task spec is malformed."""


@dataclass(frozen=True)
class Check:
    name: str
    command: str
    timeout_s: int = 60


@dataclass(frozen=True)
class TaskSpec:
    name: str
    brief: str
    checks: tuple[Check, ...]
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def hash(self) -> str:
        canonical = json.dumps(self.raw, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_spec(path: str | Path) -> TaskSpec:
    # The display form of the path, computed up front and truncated:
    # the path is caller-supplied and can be arbitrarily long (or
    # not a path at all), and neither it nor an OSError's own
    # filename echo belongs in full in a one-line error.
    shown = str(path)
    if len(shown) > 120:
        shown = shown[:117] + "..."
    try:
        # The path is validated inside the SpecError mapping
        # (v1.46.0): Path(...) on a non-path value used to raise a
        # bare TypeError here, before the try block was reached.
        path = validate_file_path(path, "spec path")
        raw = load_json_file(path, max_bytes=MAX_SPEC_FILE_BYTES, kind="spec")
    except JsonFileError as exc:
        raise SpecError(f"cannot load spec {shown}: {exc}") from exc
    except OSError as exc:
        # strerror, not str(exc): the latter repeats the filename,
        # which `shown` already carries in truncated form.
        raise SpecError(
            f"cannot load spec {shown}: {exc.strerror or exc}") from exc
    return parse_spec(raw)


def _non_empty_str(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def check_command_problem(command: str) -> str | None:
    """Why a check ``command`` can never run, or ``None`` (v1.97.0).

    A command crosses two boundaries no other spec text crosses:
    it is *spawned* (the operating system receives it as a
    NUL-terminated string, so a NUL byte makes the spawn itself
    impossible) and it is *sealed* (the evidence chain hashes
    UTF-8 text, so a lone surrogate makes the sealing
    impossible). Either character therefore condemns the check
    before it starts, whatever the shell would have made of the
    rest. Shared by :func:`parse_spec`, :func:`validate_spec`
    and ``sandbox.validate_check_command`` so the rule is stated
    once.
    """
    if "\x00" in command:
        return ("contains a NUL byte, which no process can ever "
                "receive (commands are passed as NUL-terminated "
                "strings), so the check could never run")
    if has_lone_surrogates(command):
        return ("contains lone surrogates — characters that are "
                "not valid Unicode and cannot be encoded as "
                "UTF-8 — so the check could neither be spawned "
                "nor sealed into an evidence record")
    return None


def parse_spec(raw: dict) -> TaskSpec:
    if not isinstance(raw, dict):
        raise SpecError("spec must be a JSON object")
    name = raw.get("name")
    brief = raw.get("brief", "")
    checks_raw = raw.get("checks")
    if not _non_empty_str(name):
        raise SpecError("spec.name is required (non-empty string)")
    if has_lone_surrogates(name):
        raise SpecError(
            "spec.name contains lone surrogates — characters that "
            "are not valid Unicode and cannot be encoded as "
            "UTF-8 — so it could never be sealed into an evidence "
            "record")
    if len(name) > MAX_SPEC_NAME_CHARS:
        raise SpecError(
            f"spec.name must be at most {MAX_SPEC_NAME_CHARS} "
            f"characters (got {len(name)}); the name is sealed into "
            "every evidence record the spec produces")
    if not isinstance(brief, str):
        raise SpecError("spec.brief must be a string")
    if len(brief) > MAX_BRIEF_CHARS:
        raise SpecError(
            f"spec.brief must be at most {MAX_BRIEF_CHARS} "
            f"characters (got {len(brief)}); the brief is embedded "
            "verbatim in every patch-generation prompt")
    if not isinstance(checks_raw, list) or not checks_raw:
        raise SpecError("spec.checks must be a non-empty list")
    if len(checks_raw) > MAX_CHECKS:
        raise SpecError(
            f"spec.checks must carry at most {MAX_CHECKS} checks "
            f"(got {len(checks_raw)}); each check runs sequentially "
            "with its own timeout and is sealed into the evidence "
            "record — fold suites into a single gate command")
    checks: list[Check] = []
    seen: set[str] = set()
    for i, c in enumerate(checks_raw):
        if not isinstance(c, dict):
            raise SpecError(f"spec.checks[{i}] must be an object")
        if not _non_empty_str(c.get("name")):
            raise SpecError(
                f"spec.checks[{i}].name is required (non-empty string)")
        if has_lone_surrogates(c["name"]):
            raise SpecError(
                f"spec.checks[{i}].name contains lone surrogates — "
                "characters that are not valid Unicode and cannot "
                "be encoded as UTF-8 — so it could never be sealed "
                "into an evidence record")
        if len(c["name"]) > MAX_CHECK_NAME_CHARS:
            raise SpecError(
                f"spec.checks[{i}].name must be at most "
                f"{MAX_CHECK_NAME_CHARS} characters "
                f"(got {len(c['name'])}); check names are sealed "
                "into the evidence record")
        if not _non_empty_str(c.get("command")):
            raise SpecError(
                f"spec.checks[{i}].command must be a non-empty string "
                "(a whitespace-only command would pass vacuously)")
        command_problem = check_command_problem(c["command"])
        if command_problem is not None:
            raise SpecError(
                f"spec.checks[{i}].command {command_problem}")
        if len(c["command"]) > MAX_CHECK_COMMAND_CHARS:
            raise SpecError(
                f"spec.checks[{i}].command must be at most "
                f"{MAX_CHECK_COMMAND_CHARS} characters "
                f"(got {len(c['command'])}); commands are sealed "
                "into the evidence record — put long scripts in a "
                "file and gate on running it")
        if c["name"] in seen:
            raise SpecError(f"duplicate check name: {c['name']}")
        seen.add(c["name"])
        timeout = c.get("timeout_s", 60)
        # bool is an int subclass: JSON true would otherwise become a
        # 1-second timeout instead of a spec error.
        if isinstance(timeout, bool) or not isinstance(timeout, int) \
                or not 1 <= timeout <= MAX_TIMEOUT_S:
            raise SpecError(
                f"spec.checks[{i}].timeout_s must be an int between 1 "
                f"and {MAX_TIMEOUT_S}")
        checks.append(Check(name=c["name"], command=c["command"], timeout_s=timeout))
    return TaskSpec(name=name, brief=brief, checks=tuple(checks), raw=raw)


def validate_spec(spec) -> TaskSpec:
    """Return ``spec`` if it is a valid TaskSpec, else raise SpecError.

    Spec-object validation (v1.39.0): every rule above was enforced
    only by :func:`parse_spec`, i.e. only on the JSON path — but
    ``TaskSpec``/``Check`` are plain dataclasses, so a library caller
    could construct one directly and hand it to the runner, the
    repair loop, or replay, bypassing every check. A whitespace-only
    command sealed a vacuous PASS, a bool or float ``timeout_s`` ran
    as 1 / 2.5 seconds, an empty check list sealed a FAIL record, a
    500,000-character name sealed a record that failed its own
    verification — and with ``raw`` empty (the dataclass default) the
    sealed ``spec_hash`` was the hash of ``{}``, binding none of the
    fields that actually ran. The entry points now call this helper
    before any workspace is prepared or any check runs: the spec
    must carry the same field values :func:`parse_spec` enforces,
    and its ``raw`` must be a non-empty object that parses back to
    exactly those fields, so the hash sealed into the record always
    binds the spec that ran. Specs produced by
    :func:`parse_spec`/:func:`load_spec` satisfy this by
    construction.
    """
    if not isinstance(spec, TaskSpec):
        raise SpecError(
            f"spec must be a TaskSpec produced by parse_spec/load_spec, "
            f"got {type(spec).__name__}")
    if not _non_empty_str(spec.name):
        raise SpecError("spec.name is required (non-empty string)")
    if has_lone_surrogates(spec.name):
        raise SpecError(
            "spec.name contains lone surrogates — characters that "
            "are not valid Unicode and cannot be encoded as "
            "UTF-8 — so it could never be sealed into an evidence "
            "record")
    if len(spec.name) > MAX_SPEC_NAME_CHARS:
        raise SpecError(
            f"spec.name must be at most {MAX_SPEC_NAME_CHARS} characters")
    if not isinstance(spec.brief, str):
        raise SpecError("spec.brief must be a string")
    if len(spec.brief) > MAX_BRIEF_CHARS:
        raise SpecError(
            f"spec.brief must be at most {MAX_BRIEF_CHARS} characters")
    checks = spec.checks
    if isinstance(checks, (str, bytes)) or not isinstance(checks, (tuple, list)):
        raise SpecError("spec.checks must be a non-empty tuple of Check")
    if not checks:
        raise SpecError("spec.checks must be a non-empty tuple of Check")
    if len(checks) > MAX_CHECKS:
        raise SpecError(
            f"spec.checks must carry at most {MAX_CHECKS} checks")
    seen: set[str] = set()
    for i, c in enumerate(checks):
        if not isinstance(c, Check):
            raise SpecError(f"spec.checks[{i}] must be a Check")
        if not _non_empty_str(c.name):
            raise SpecError(
                f"spec.checks[{i}].name is required (non-empty string)")
        if has_lone_surrogates(c.name):
            raise SpecError(
                f"spec.checks[{i}].name contains lone surrogates — "
                "characters that are not valid Unicode and cannot "
                "be encoded as UTF-8 — so it could never be sealed "
                "into an evidence record")
        if len(c.name) > MAX_CHECK_NAME_CHARS:
            raise SpecError(
                f"spec.checks[{i}].name must be at most "
                f"{MAX_CHECK_NAME_CHARS} characters")
        if not _non_empty_str(c.command):
            raise SpecError(
                f"spec.checks[{i}].command must be a non-empty string "
                "(a whitespace-only command would pass vacuously)")
        command_problem = check_command_problem(c.command)
        if command_problem is not None:
            raise SpecError(
                f"spec.checks[{i}].command {command_problem}")
        if len(c.command) > MAX_CHECK_COMMAND_CHARS:
            raise SpecError(
                f"spec.checks[{i}].command must be at most "
                f"{MAX_CHECK_COMMAND_CHARS} characters")
        if c.name in seen:
            raise SpecError(f"duplicate check name: {c.name}")
        seen.add(c.name)
        if isinstance(c.timeout_s, bool) or not isinstance(c.timeout_s, int) \
                or not 1 <= c.timeout_s <= MAX_TIMEOUT_S:
            raise SpecError(
                f"spec.checks[{i}].timeout_s must be an int between 1 "
                f"and {MAX_TIMEOUT_S}")
    if not isinstance(spec.raw, dict) or not spec.raw:
        raise SpecError(
            "spec.raw must be the non-empty object the spec was parsed "
            "from (use parse_spec/load_spec): without it the sealed "
            "spec_hash binds none of the fields that ran")
    reparsed = parse_spec(spec.raw)
    if (reparsed.name, reparsed.brief, reparsed.checks) != (
            spec.name, spec.brief, tuple(checks)):
        raise SpecError(
            "spec fields do not match spec.raw: the sealed spec_hash "
            "would not bind the checks that ran (build the spec with "
            "parse_spec/load_spec)")
    return spec
