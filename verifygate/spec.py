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
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path


MAX_TIMEOUT_S = 3600  # one hour: a sane ceiling for a single check
MAX_CHECKS = 100  # generous ceiling: a gate spec is a handful of commands


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
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SpecError(f"cannot load spec {path}: {exc}") from exc
    return parse_spec(raw)


def _non_empty_str(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def parse_spec(raw: dict) -> TaskSpec:
    if not isinstance(raw, dict):
        raise SpecError("spec must be a JSON object")
    name = raw.get("name")
    brief = raw.get("brief", "")
    checks_raw = raw.get("checks")
    if not _non_empty_str(name):
        raise SpecError("spec.name is required (non-empty string)")
    if not isinstance(brief, str):
        raise SpecError("spec.brief must be a string")
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
        if not _non_empty_str(c.get("command")):
            raise SpecError(
                f"spec.checks[{i}].command must be a non-empty string "
                "(a whitespace-only command would pass vacuously)")
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
