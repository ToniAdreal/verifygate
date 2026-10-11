"""End-to-end verification run: spec + candidate -> evidence record.

Diagnosis resilience (v1.19.0): the diagnosis is commentary, exactly
like the Tavily references beside it — it can never be a verdict
input, so its failure must never destroy a verdict the deterministic
checks have already decided. Before this, ``llm.explain_failure``
was called unguarded: a real-adapter failure (network error,
malformed Token Factory response, empty completion after the retry —
all ``RuntimeError``) propagated out of the run, so a candidate that
had deterministically FAILed produced *no evidence record at all*
(the CLI reported an adapter error, exit 2), the repair loop aborted
on its first attempt, and replaying a FAIL record in real mode
reported "not reproduced" even though every check reproduced — only
the words had failed, and words are never compared. The explanation
call is now guarded the same way the reference lookup already was:
on failure the record seals a deterministic fallback explanation
rendered from the real check results, plus a ``diagnosis_error``
field documenting the degradation inside the hash chain. Calls the
adapter recorded before raising stay in the usage log and are sealed
as usual — the token spend is not dropped either.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .evidence import build_record, quote_label, tree_hash
from .llm import LLMAdapter
from .sandbox import prepare_workspace
from .spec import TaskSpec
from .tavily import ReferenceLookup, diagnosis_query
from .validators import overall_verdict, run_checks


def _fallback_explanation(check_dicts: list[dict], adapter_name: str,
                          error: str) -> str:
    """Deterministic stand-in when the LLM diagnosis is unavailable.

    Rendered only from the real check results (never invented), and
    explicit that no model explanation exists: the FAIL verdict was
    decided by the checks alone and does not depend on this text.
    """
    lines = [
        "## Diagnosis unavailable",
        "",
        f"The LLM adapter ({adapter_name}) failed to explain this "
        f"failure: {error}",
        "No model explanation was produced. The FAIL verdict was "
        "decided by the deterministic checks alone and does not "
        "depend on any explanation.",
        "",
        "Failing checks:",
    ]
    for c in check_dicts:
        if c.get("verdict") == "FAIL":
            if c.get("timed_out"):
                lines.append(
                    f"- {c.get('name')}: `{c.get('command')}` "
                    "timed out (exit code: none)")
            else:
                lines.append(
                    f"- {c.get('name')}: `{c.get('command')}` "
                    f"exited {c.get('exit_code')}")
    return "\n".join(lines)


def run_verification(
    *,
    spec: TaskSpec,
    candidate_dir: str | Path,
    mode: str,
    llm: LLMAdapter,
    references: ReferenceLookup,
    spec_label: str,
    candidate_label: str,
) -> dict:
    workspace = prepare_workspace(candidate_dir)
    try:
        results = run_checks(spec, workspace)
    finally:
        # The workspace is a throwaway copy: checks are done with it
        # the moment they have run, and leaving it behind leaks a full
        # candidate copy per run (a long CI/dogfood session can fill
        # the temp filesystem — observed in practice, Errno 28).
        shutil.rmtree(workspace, ignore_errors=True)
    verdict = overall_verdict(results)
    check_dicts = [r.to_dict() for r in results]

    diagnosis = None
    if verdict == "FAIL":
        # The explanation is advisory commentary (see module
        # docstring): guard it exactly like the reference lookup
        # below, so an adapter failure degrades to a sealed fallback
        # instead of destroying the record the checks just decided.
        diagnosis_error: str | None = None
        try:
            explanation = llm.explain_failure(check_dicts)
            if not isinstance(explanation, str):
                raise TypeError(
                    "explain_failure returned "
                    f"{type(explanation).__name__}, not str")
        except Exception as exc:  # diagnosis must never break a run
            diagnosis_error = (
                f"diagnosis failed ({type(exc).__name__}): "
                f"{str(exc)[:200]}")
            explanation = _fallback_explanation(
                check_dicts, llm.name, diagnosis_error)
        query = diagnosis_query(check_dicts)
        refs: list[dict] = []
        reference_error: str | None = None
        if query:
            # References are commentary, never verdict inputs: a lookup
            # failure (missing key, outage, malformed response) must
            # degrade to no references, never break the run. Adapters
            # are contractually non-raising, but guard here too so any
            # ReferenceLookup implementation is safe to plug in. The
            # degradation is noted inside the sealed record.
            try:
                refs = references.lookup(query) or []
            except Exception as exc:  # lookup must never break a run
                reference_error = (
                    f"reference lookup failed ({type(exc).__name__}): "
                    f"{str(exc)[:200]}")
            if reference_error is None:
                reference_error = getattr(references, "last_error", None)
        diagnosis = {
            "explanation": explanation,
            "references": refs,
            "llm_adapter": llm.name,
            "reference_source": references.name,
            "reference_error": reference_error,
            "diagnosis_error": diagnosis_error,
        }

    # Labels are quoted for the command line (evidence.quote_label):
    # a raw path containing a space or a quote would produce a replay
    # string that fails the record's own metadata template — the
    # record would report CHAIN INVALID the moment it was sealed.
    replay = (
        f"python3 -m verifygate run --spec {quote_label(spec_label)} "
        f"--candidate {quote_label(candidate_label)} --mode {mode}"
    )
    # Seal every real model call made since the previous record (in the
    # repair loop this includes the generate_patch call that produced
    # the candidate now being verified) into this record's hash chain.
    llm_usage = llm.drain_calls() if hasattr(llm, "drain_calls") else []
    return build_record(
        spec_name=spec.name,
        spec_hash=spec.hash,
        candidate_tree_hash=tree_hash(candidate_dir),
        mode=mode,
        check_results=check_dicts,
        verdict=verdict,
        diagnosis=diagnosis,
        replay_command=replay,
        llm_usage=llm_usage,
    )


def write_record(record: dict, out_path: str | Path) -> Path:
    out_path = Path(out_path)
    out_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return out_path
