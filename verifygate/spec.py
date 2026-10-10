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
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path


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


def parse_spec(raw: dict) -> TaskSpec:
    if not isinstance(raw, dict):
        raise SpecError("spec must be a JSON object")
    name = raw.get("name")
    brief = raw.get("brief", "")
    checks_raw = raw.get("checks")
    if not name or not isinstance(name, str):
        raise SpecError("spec.name is required (string)")
    if not isinstance(checks_raw, list) or not checks_raw:
        raise SpecError("spec.checks must be a non-empty list")
    checks: list[Check] = []
    seen: set[str] = set()
    for i, c in enumerate(checks_raw):
        if not isinstance(c, dict) or not c.get("name") or not c.get("command"):
            raise SpecError(f"spec.checks[{i}] needs both 'name' and 'command'")
        if c["name"] in seen:
            raise SpecError(f"duplicate check name: {c['name']}")
        seen.add(c["name"])
        timeout = c.get("timeout_s", 60)
        if not isinstance(timeout, int) or timeout <= 0:
            raise SpecError(f"spec.checks[{i}].timeout_s must be a positive int")
        checks.append(Check(name=c["name"], command=c["command"], timeout_s=timeout))
    return TaskSpec(name=name, brief=brief, checks=tuple(checks), raw=raw)
