"""Repair loop: FAIL evidence -> LLM patch -> apply -> re-verify.

This is the agentic half of VerifyGate, kept on a short deterministic
leash. Each round:

  1. the current candidate is verified (validators decide, as always);
  2. on FAIL, the failing check results are handed to the LLM adapter,
     which *proposes* a unified diff — advisory text, nothing more;
  3. the diff is applied to a fresh copy of the candidate (the original
     on disk is never modified; a diff that does not apply cleanly
     stops the loop rather than being forced in);
  4. the patched copy is verified from scratch.

The loop ends on the first PASS, when no usable patch is produced, or
after ``max_rounds`` patch attempts — whichever comes first. The report
embeds every attempt's full evidence record, so each intermediate
verdict stays independently replayable and tamper-evident.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from . import __version__
from .llm import LLMAdapter
from .patch import PatchError, apply_patch
from .runner import run_verification
from .sandbox import _IGNORE
from .spec import TaskSpec
from .tavily import ReferenceLookup


def _check_dicts(record: dict) -> list[dict]:
    return [
        {k: v for k, v in e.items() if k not in ("kind", "hash")}
        for e in record["entries"]
        if e["kind"] == "check"
    ]


def run_repair(
    *,
    spec: TaskSpec,
    candidate_dir: str | Path,
    mode: str,
    llm: LLMAdapter,
    references: ReferenceLookup,
    spec_label: str,
    candidate_label: str,
    max_rounds: int = 3,
) -> dict:
    if max_rounds < 0:
        raise ValueError("max_rounds must be >= 0")
    attempts: list[dict] = []
    current_dir = Path(candidate_dir)
    current_label = candidate_label
    patch_info: dict | None = None
    record = run_verification(
        spec=spec, candidate_dir=current_dir, mode=mode, llm=llm,
        references=references, spec_label=spec_label,
        candidate_label=current_label,
    )
    attempts.append({
        "round": 0,
        "candidate_dir": str(current_dir),
        "verdict": record["verdict"],
        "record_hash": record["record_hash"],
        "patch_from_previous": None,
        "evidence": record,
    })

    rounds_used = 0
    patch_error: str | None = None
    stopped_reason = "passed" if record["verdict"] == "PASS" else "max_rounds"
    while record["verdict"] == "FAIL" and rounds_used < max_rounds:
        failing_evidence = {
            "spec_name": spec.name,
            "brief": spec.brief,
            "verdict": record["verdict"],
            "checks": _check_dicts(record),
        }
        patch_text = llm.generate_patch(spec.brief, failing_evidence)
        rounds_used += 1
        patch_info = {
            "round": rounds_used,
            "llm_adapter": llm.name,
            "applied": False,
            "changed_files": [],
            "error": None,
        }
        new_dir = Path(tempfile.mkdtemp(prefix="verifygate-repair-"))
        shutil.copytree(current_dir, new_dir, dirs_exist_ok=True, ignore=_IGNORE)
        try:
            changed = apply_patch(new_dir, patch_text)
        except PatchError as exc:
            patch_info["error"] = str(exc)
            patch_error = str(exc)
            stopped_reason = "patch_not_applicable"
            break
        patch_info["applied"] = True
        patch_info["changed_files"] = changed
        current_dir = new_dir
        current_label = str(new_dir)
        record = run_verification(
            spec=spec, candidate_dir=current_dir, mode=mode, llm=llm,
            references=references, spec_label=spec_label,
            candidate_label=current_label,
        )
        attempts.append({
            "round": rounds_used,
            "candidate_dir": str(current_dir),
            "verdict": record["verdict"],
            "record_hash": record["record_hash"],
            "patch_from_previous": patch_info,
            "evidence": record,
        })
        if record["verdict"] == "PASS":
            stopped_reason = "passed"

    return {
        "tool": "verifygate",
        "version": __version__,
        "kind": "repair-report",
        "spec_name": spec.name,
        "mode": mode,
        "llm_adapter": llm.name,
        "max_rounds": max_rounds,
        "rounds_used": rounds_used,
        "final_verdict": attempts[-1]["verdict"],
        "stopped_reason": stopped_reason,
        "last_patch_error": patch_error,
        "attempts": attempts,
    }
