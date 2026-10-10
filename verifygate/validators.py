"""Deterministic validators — the only place verdicts are decided.

A check passes if and only if its command exits 0 inside the sandbox
before its timeout. No model output, heuristic, or score participates.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .sandbox import run_command
from .spec import TaskSpec


@dataclass
class CheckResult:
    name: str
    command: str
    exit_code: int | None
    duration_ms: int
    stdout_tail: str
    stderr_tail: str
    timed_out: bool
    verdict: str  # "PASS" | "FAIL"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "command": self.command,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "timed_out": self.timed_out,
            "verdict": self.verdict,
        }


def run_checks(spec: TaskSpec, workspace: Path) -> list[CheckResult]:
    results: list[CheckResult] = []
    for check in spec.checks:
        r = run_command(check.command, cwd=workspace, timeout_s=check.timeout_s)
        verdict = "PASS" if (not r.timed_out and r.exit_code == 0) else "FAIL"
        results.append(
            CheckResult(
                name=check.name,
                command=check.command,
                exit_code=r.exit_code,
                duration_ms=r.duration_ms,
                stdout_tail=r.stdout_tail,
                stderr_tail=r.stderr_tail,
                timed_out=r.timed_out,
                verdict=verdict,
            )
        )
    return results


def overall_verdict(results: list[CheckResult]) -> str:
    return "PASS" if results and all(r.verdict == "PASS" for r in results) else "FAIL"
