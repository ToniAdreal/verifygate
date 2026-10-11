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

Replay note: a patched attempt's candidate is an ephemeral temp copy
that the loop (or the CLI) deletes, so its record's ``replay`` field
must not point at that path — it names the ``verifygate replay``
command instead, which reconstructs each patched tree from the
original candidate plus the patches recorded in the report. Since
chain v3 the ``replay`` field is sealed into the record hash, so the
correction goes through ``evidence.update_replay_command`` — the one
sanctioned post-seal edit, which re-seals the record it touches.

Round-budget validation (v1.22.0): ``max_rounds`` is validated the
way spec timeouts have been since v1.7.0 — a real ``int`` (JSON
``true`` is an ``int`` subclass and used to slip through as 1), in
range ``0..MAX_ROUNDS``. Before this, the only check was
``max_rounds < 0``: a bool or float budget produced a report whose
own ``max_rounds`` verification rejects (``verify`` requires a
non-bool int), so the loop happily emitted reports that fail their
own verification; a string or ``None`` crashed the comparison with
an unhandled ``TypeError`` instead of the documented ``ValueError``;
a float budget of 2.5 silently allowed 3 rounds; and there was no
upper bound at all — ``--max-rounds 1000000000`` was accepted
verbatim, each round being a paid Token Factory call plus a full
re-verification, with a full evidence record embedded per attempt.
``verify``'s narrative check mirrors the bound: a report claiming a
budget above ``MAX_ROUNDS`` is not one this loop can have produced.

Generation resilience (v1.20.0): ``llm.generate_patch`` is guarded
exactly like the diagnosis call in the runner. Before this, a
generation failure — a network error, a malformed Token Factory
response, an empty completion after the retry (all ``RuntimeError``
from the real adapter), or a non-string return — propagated out of
the loop: attempt 0's already-decided FAIL evidence was never
returned, the CLI reported an adapter error (exit 2) and wrote no
report at all — the same loss v1.19.0 fixed for the diagnosis, one
call later. A generation failure now ends the loop instead: the
failed round counts as used, the failure is preserved as the
report's ``failed_patch`` with ``error_kind: generation_failed``,
``stopped_reason`` is ``generation_failed``, and the report —
attempt 0's sealed record included — is delivered with final
verdict FAIL (CLI exit 1, not 2). Unlike a rejected diff, a
generation failure is not retried: there is no proposal to give
feedback on, and repeating the identical prompt would only spend
more of the caller's token budget on a failure that may be an
outage or an auth error. Calls the adapter recorded before failing
stay pending and are surfaced as the report's unsealed usage, so
the token spend is not dropped either.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from . import __version__
from .evidence import quote_label, update_replay_command
from .llm import LLMAdapter
from .patch import PatchError, apply_patch
from .runner import run_verification
from .sandbox import iter_candidate_files, prepare_workspace
from .spec import TaskSpec
from .tavily import ReferenceLookup

# A generous ceiling, not a target: the default is 3, and every round
# costs one LLM patch call plus a full re-verification, with a full
# evidence record embedded per attempt. The bound exists so a typo'd
# or adversarial budget cannot loop (and, in real mode, bill) without
# limit — the same reasoning that caps a check's timeout at 3600s.
MAX_ROUNDS = 10


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
    # bool is an int subclass: JSON true would otherwise become a
    # 1-round budget whose report then fails verify's non-bool-int
    # rule; a float would silently allow more rounds than stated; a
    # string/None would crash the old ``< 0`` comparison outright.
    if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) \
            or not 0 <= max_rounds <= MAX_ROUNDS:
        raise ValueError(
            f"max_rounds must be an int between 0 and {MAX_ROUNDS}")
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
        rounds_used += 1
        try:
            patch_text = llm.generate_patch(spec.brief, failing_evidence)
            if not isinstance(patch_text, str):
                raise TypeError(
                    "generate_patch returned "
                    f"{type(patch_text).__name__}, not str")
        except Exception as exc:  # generation must never destroy the report
            gen_error = (
                f"patch generation failed ({type(exc).__name__}): "
                f"{str(exc)[:500]}")
            patch_info = {
                "round": rounds_used,
                "llm_adapter": llm.name,
                "applied": False,
                "changed_files": [],
                "error": gen_error,
                "error_kind": "generation_failed",
                # Snapshot only — the calls stay pending and are
                # surfaced as the report's unsealed usage below.
                "llm_usage": (
                    llm.pending_calls()[calls_before:]
                    if hasattr(llm, "pending_calls") else []),
                # No proposal exists to preserve.
                "patch_text": "",
            }
            patch_error = gen_error
            failed_patch = patch_info
            stopped_reason = "generation_failed"
            break
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
            "error_kind": None,
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
            patch_info["error_kind"] = "patch_rejected"
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
        # The runner's replay string points at the ephemeral patched
        # copy (deleted before the caller can ever run it). Replace it
        # with the truthful instruction: replay the whole report from
        # the original candidate; `verifygate replay` reconstructs
        # this attempt's tree from the recorded patch.
        update_replay_command(record, (
            f"python3 -m verifygate replay --record <repair-report.json> "
            f"--spec {quote_label(spec_label)} "
            f"--candidate {quote_label(candidate_label)} "
            f"--mode {mode}  # attempt {rounds_used}: patched copy, "
            f"reconstructed from the report's recorded patch"
        ))
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
