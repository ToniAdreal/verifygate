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
from pathlib import Path

from . import __version__
from .llm import LLMAdapter
from .patch import PatchError, apply_patch
from .runner import run_verification
from .sandbox import iter_candidate_files, prepare_workspace
from .spec import TaskSpec
from .tavily import ReferenceLookup


def _check_dicts(record: dict) -> list[dict]:
    return [
        {k: v for k, v in e.items() if k not in ("kind", "hash")}
        for e in record["entries"]
        if e["kind"] == "check"
    ]


_TEXT_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".md", ".txt", ".toml",
    ".yaml", ".yml", ".cfg", ".ini", ".sh", ".go", ".rs", ".java", ".c",
    ".h", ".cpp", ".rb", ".php", ".html", ".css",
}
_MAX_FILE_CHARS = 4000
_MAX_TOTAL_CHARS = 12000


def _candidate_files(root: str | Path) -> dict[str, str]:
    """Snapshot the candidate's small text files for the patch prompt.

    A model asked for a unified diff must see the exact file contents
    the diff will be applied to — brief + check output alone cannot
    produce a diff whose context lines match. Binary, oversized, and
    housekeeping files are skipped; the snapshot is capped overall.
    """
    files: dict[str, str] = {}
    total = 0
    # Sandbox containment traversal: a candidate symlink pointing at a
    # host file must never leak that file's contents into an LLM prompt.
    for rel, source in iter_candidate_files(root):
        if Path(rel).suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            text = source.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if len(text) > _MAX_FILE_CHARS or total + len(text) > _MAX_TOTAL_CHARS:
            continue
        files[rel] = text
        total += len(text)
    return files


def _usage_totals(records_and_report: list) -> dict:
    """Aggregate token usage across sealed entries + unsealed calls."""
    calls = 0
    total_tokens = 0
    for item in records_and_report:
        for c in item:
            calls += 1
            total_tokens += (c.get("usage") or {}).get("total_tokens") or 0
    return {"llm_calls": calls, "total_tokens": total_tokens}


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
    owned_dir: Path | None = None  # latest repair-created patched copy
    patch_error: str | None = None
    failed_patch: dict | None = None
    patch_feedback: str | None = None
    stopped_reason = "passed" if record["verdict"] == "PASS" else "max_rounds"
    while record["verdict"] == "FAIL" and rounds_used < max_rounds:
        failing_evidence = {
            "spec_name": spec.name,
            "brief": spec.brief,
            "verdict": record["verdict"],
            "checks": _check_dicts(record),
            # The exact sources the proposed diff must apply to. The
            # mock ignores this; the real adapter embeds it verbatim.
            "candidate_files": _candidate_files(current_dir),
        }
        if patch_feedback:
            # A previous diff was rejected by the applier: hand the
            # exact rejection back so the next proposal can fix its
            # form, not just its content. Each retry costs one round.
            failing_evidence["patch_error"] = patch_feedback
        calls_before = len(llm.pending_calls()) if hasattr(llm, "pending_calls") else 0
        patch_text = llm.generate_patch(spec.brief, failing_evidence)
        rounds_used += 1
        patch_calls = (
            llm.pending_calls()[calls_before:]
            if hasattr(llm, "pending_calls") else []
        )
        patch_info = {
            "round": rounds_used,
            "llm_adapter": llm.name,
            "applied": False,
            "changed_files": [],
            "error": None,
            # Snapshot only — the calls stay pending and are sealed
            # into the next attempt's evidence record by the runner.
            "llm_usage": patch_calls,
            # The raw proposal, kept so a rejected patch stays
            # debuggable from the report alone (truncated for size).
            "patch_text": patch_text[:8000],
        }
        # Fresh copy through the hardened workspace path (scrubbed
        # traversal: no symlink escapes, no housekeeping files).
        new_dir = prepare_workspace(current_dir)
        try:
            changed = apply_patch(new_dir, patch_text)
        except PatchError as exc:
            # The rejected copy is never used again; do not leak it.
            shutil.rmtree(new_dir, ignore_errors=True)
            patch_info["error"] = str(exc)
            patch_error = str(exc)
            failed_patch = patch_info
            patch_feedback = str(exc)
            if rounds_used < max_rounds:
                continue  # retry with the rejection as feedback
            stopped_reason = "patch_not_applicable"
            break
        patch_info["applied"] = True
        patch_info["changed_files"] = changed
        patch_feedback = None
        # The previous repair-owned copy is superseded: its evidence
        # record is already sealed into `attempts`, so delete it
        # instead of leaking one full candidate copy per round. The
        # caller's own candidate directory is never touched, and the
        # final patched copy is kept (the CLI's --patched-out copies
        # from attempts[-1].candidate_dir after the loop returns).
        if owned_dir is not None:
            shutil.rmtree(owned_dir, ignore_errors=True)
        owned_dir = new_dir
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

    # Calls still pending were never sealed into a record (e.g. a patch
    # that did not apply ended the loop): surface them at report level
    # instead of dropping the usage on the floor.
    unsealed = llm.drain_calls() if hasattr(llm, "drain_calls") else []
    sealed_calls = [
        c
        for a in attempts
        for e in a["evidence"]["entries"]
        if e["kind"] == "llm_usage"
        for c in e["calls"]
    ]
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
        "failed_patch": failed_patch,
        "llm_usage_total": _usage_totals([sealed_calls, unsealed]),
        "unsealed_llm_usage": unsealed,
        "attempts": attempts,
    }
