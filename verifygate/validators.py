"""Deterministic validators — the only place verdicts are decided.

A check passes if and only if its command exits 0 inside the sandbox
before its timeout. No model output, heuristic, or score participates.

Entry validation (v1.51.0): :func:`run_checks` validates its own
arguments — the ``spec`` through ``spec.validate_spec`` and the
``workspace`` through ``sandbox.validate_candidate_dir`` — instead
of trusting that the runner validated them first. The runner does
validate both (the spec since v1.39.0), but a direct caller of the
validators bypassed that entirely: a hand-built spec with a
whitespace command sealed its vacuous PASS one layer down, and a
``workspace`` of ``None`` / an ``int`` / a missing path surfaced as
stringified-path nonsense inside ``run_command``. Each check's
command and timeout are additionally validated by the execution
primitive itself (``sandbox.run_command``), so the rules hold at
every layer a caller can reach.

Verdict-aggregation validation (v1.55.0): :func:`overall_verdict`
— the one function that turns per-check results into the verdict
sealed into the record — took its input on trust. An empty
*iterator* aggregated to PASS (``results and all(...)``: an
iterator is always truthy, and ``all`` of nothing is true) while
an empty list aggregated to FAIL — a vacuous PASS from the
aggregator itself, the v1.7.0 shape at the verdict layer.
``None`` silently aggregated to FAIL, sealing a verdict for
checks that never ran; a string or a list of plain dicts crashed
with an unhandled ``AttributeError``. Worst, the aggregate
trusted each result's ``verdict`` field blindly: a result
claiming PASS with exit code 3, or PASS while timed out,
aggregated to PASS — contradicting this module's own derivation
rule (a check passes iff it exits 0 before its timeout) and
producing a record the v1.12.0 consistency mirror rejects, so
the record failed its own verification the moment it was
written. The input is now materialised and every entry is
validated through :func:`validate_check_result` before anything
is aggregated: a :class:`CheckResult` whose verdict-bearing
fields (``timed_out``, ``exit_code``, ``duration_ms``) are the
shapes ``run_checks`` emits — the same types and ranges the
evidence mirror enforces — and whose ``verdict`` is exactly the
verdict those fields derive. An inconsistent result is a caller
bug (one ``ValueError``), never a verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .evidence import MAX_DURATION_MS, MAX_EXIT_CODE, MIN_EXIT_CODE
from .sandbox import run_command, validate_candidate_dir
from .spec import TaskSpec, validate_spec


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
    # Validate at this entry too (v1.51.0, see the module
    # docstring): the runner's validation protects the runner's
    # callers, not this function's.
    validate_spec(spec)
    workspace = validate_candidate_dir(workspace, name="workspace")
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


def validate_check_result(result, name: str = "check result") -> CheckResult:
    """Return a verdict-bearing-consistent :class:`CheckResult`.

    Verdict-aggregation validation (v1.55.0, see the module
    docstring): the fields an aggregate verdict is derived from
    must be the shapes :func:`run_checks` emits — ``timed_out`` a
    bool, ``exit_code`` ``None`` exactly when the check timed out
    and otherwise a real (non-bool) ``int`` within the POSIX /
    signal range, ``duration_ms`` a real (non-bool) ``int``
    within the measured-duration bound (the ranges are
    ``evidence``'s, the same ones its structure mirror enforces
    over a sealed record) — and ``verdict`` must be exactly the
    verdict those fields derive under this module's one rule (a
    check passes iff it exits 0 before its timeout). Anything
    else is a ``ValueError`` naming the field, never an aggregate.
    """
    if not isinstance(result, CheckResult):
        raise ValueError(
            f"{name} must be a CheckResult "
            f"(got {type(result).__name__})")
    if not isinstance(result.timed_out, bool):
        raise ValueError(
            f"{name}.timed_out must be a bool "
            f"(got {type(result.timed_out).__name__})")
    if isinstance(result.duration_ms, bool) \
            or not isinstance(result.duration_ms, int) \
            or not 0 <= result.duration_ms <= MAX_DURATION_MS:
        raise ValueError(
            f"{name}.duration_ms must be an int with "
            f"0 <= duration_ms <= {MAX_DURATION_MS} "
            f"(got {type(result.duration_ms).__name__})")
    if result.timed_out:
        if result.exit_code is not None:
            raise ValueError(
                f"{name}.exit_code must be None for a timed-out "
                f"check (got {type(result.exit_code).__name__})")
    elif isinstance(result.exit_code, bool) \
            or not isinstance(result.exit_code, int) \
            or not MIN_EXIT_CODE <= result.exit_code <= MAX_EXIT_CODE:
        raise ValueError(
            f"{name}.exit_code must be an int with "
            f"{MIN_EXIT_CODE} <= exit_code <= {MAX_EXIT_CODE} "
            f"(got {type(result.exit_code).__name__})")
    derived = "PASS" if (not result.timed_out
                         and result.exit_code == 0) else "FAIL"
    if result.verdict != derived:
        # The offending verdict is shown truncated, as the other
        # validators bound their error lines: a caller-supplied
        # verdict of any length must not bloat the one-line error.
        shown = repr(result.verdict)
        if len(shown) > 80:
            shown = shown[:77] + "..."
        raise ValueError(
            f"{name}.verdict {shown} does not match the "
            f"verdict its exit code / timeout derive ({derived})")
    return result


def overall_verdict(results: list[CheckResult]) -> str:
    """Aggregate per-check results into the one verdict.

    The input is materialised first (v1.55.0): an empty *iterator*
    previously aggregated to PASS — an iterator is always truthy
    and ``all`` of nothing is true — while an empty list
    aggregated to FAIL. Every entry is validated through
    :func:`validate_check_result` before aggregation, so the
    aggregate can only ever be computed over results whose
    verdicts are the ones their exit codes and timeouts derive.
    An empty input (any shape) is FAIL: no checks, no PASS.
    """
    if results is None or isinstance(results, (str, bytes)):
        raise ValueError(
            f"results must be an iterable of CheckResult "
            f"(got {type(results).__name__})")
    try:
        items = list(results)
    except TypeError:
        raise ValueError(
            f"results must be an iterable of CheckResult "
            f"(got {type(results).__name__})") from None
    for index, result in enumerate(items):
        validate_check_result(result, name=f"results[{index}]")
    return "PASS" if items and all(
        r.verdict == "PASS" for r in items) else "FAIL"
