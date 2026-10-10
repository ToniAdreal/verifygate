"""End-to-end verification run: spec + candidate -> evidence record."""

from __future__ import annotations

import json
from pathlib import Path

from .evidence import build_record, tree_hash
from .llm import LLMAdapter
from .sandbox import prepare_workspace
from .spec import TaskSpec
from .tavily import ReferenceLookup, diagnosis_query
from .validators import overall_verdict, run_checks


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
    results = run_checks(spec, workspace)
    verdict = overall_verdict(results)
    check_dicts = [r.to_dict() for r in results]

    diagnosis = None
    if verdict == "FAIL":
        explanation = llm.explain_failure(check_dicts)
        query = diagnosis_query(check_dicts)
        refs = references.lookup(query) if query else []
        diagnosis = {
            "explanation": explanation,
            "references": refs,
            "llm_adapter": llm.name,
            "reference_source": references.name,
        }

    replay = (
        f"python3 -m verifygate run --spec {spec_label} "
        f"--candidate {candidate_label} --mode {mode}"
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
