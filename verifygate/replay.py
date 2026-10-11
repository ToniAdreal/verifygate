"""True replay of evidence records: re-run the checks, compare verdicts.

``verifygate verify`` answers "is this record intact?" without running
anything. Replay answers the other half of the record's promise —
"does re-running the checks reproduce the sealed verdict?" — the way
the record's own ``replay`` field has always claimed is possible.

For a single record, replay re-verifies the candidate on disk and
compares the verdict and every per-check verdict/exit code against the
sealed entries, and checks that the candidate's tree hash still matches
the sealed header (a candidate edited after the fact is a mismatch,
not a reproduction). The supplied spec is likewise bound to the
record: its hash must equal the sealed header's ``spec_hash``
(``spec_match``). Before v1.8.0 replay never made that comparison, so
a spec with the same checks but a different name and brief — a
different task that happens to run the same commands — reported
REPLAY MATCHED against a record sealed for the original task.

For a repair report, attempt 0 is replayed from the submitted candidate
and each later attempt is *reconstructed*: the report already carries
the patch that produced it (``patch_from_previous.patch_text``), so
replay applies those patches in order to fresh copies of the original
candidate — the original is never modified — and re-verifies each
reconstructed tree. A reconstructed tree whose hash differs from the
sealed header did not reproduce the attempt, whatever its verdict.

Two properties are deliberate:

  * The replay *mode* is the caller's choice. Verdicts come from the
    deterministic checks, which are mode-independent, so a real-mode
    record can be replayed in Judge Mode with zero keys and zero model
    calls; only the diagnosis words would differ, and words are never
    compared.
  * Nothing about the new run is sealed or written back. Replay is a
    comparison, printed for the caller; the original records stand.

Label validation (v1.40.0): the ``spec_label`` and the candidate
label derived from ``candidate_dir`` are validated up front,
through ``evidence.validate_label`` — the per-record path catches
only ``OSError`` / ``RuntimeError`` from the runner, so an invalid
label (a non-string crashing ``quote_label`` with a ``TypeError``,
an empty or over-long one sealing a replay field) would otherwise
escape mid-replay instead of failing before any record is
replayed, exactly the escape the spec-object validation (v1.39.0)
closed for the spec itself.

Candidate-directory validation (v1.41.0): ``candidate_dir`` itself
is validated up front, through ``sandbox.validate_candidate_dir``
(judged under the ``candidate_label`` name, as the value replay
derives its candidate label from). Before this, the v1.40.0 check
validated ``str(candidate_dir)`` — which *stringifies* a bad value
instead of judging it, so an ``int`` or ``None`` candidate passed
that check and crashed with a ``TypeError`` inside the per-record
path (past its ``OSError``/``RuntimeError`` handling), an empty
string silently meant the current directory, and a regular file
surfaced only as a per-record replay error from the tree hash's
``NotADirectoryError``. A malformed candidate is now a usage
error before any record is replayed; the directory validation
subsumes the derived-label check (a validated directory's string
is a non-empty label within the same 1,000-character ceiling).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .evidence import tree_hash, validate_label, validate_mode, verify_chain
from .llm import LLMAdapter
from .patch import PatchError, apply_patch
from .runner import run_verification
from .sandbox import prepare_workspace, validate_candidate_dir
from .spec import TaskSpec, validate_spec
from .tavily import ReferenceLookup
from .verify import collect_records, header_of


def _check_summary(record: dict) -> list[tuple[str, str, int | None]]:
    """(name, verdict, exit_code) per check entry, in record order."""
    return [
        (e.get("name"), e.get("verdict"), e.get("exit_code"))
        for e in record.get("entries", [])
        if e.get("kind") == "check"
    ]


def _replay_one(
    *,
    label: str,
    expected: dict,
    spec: TaskSpec,
    candidate_dir: Path,
    mode: str,
    llm: LLMAdapter,
    references: ReferenceLookup,
    spec_label: str,
) -> dict:
    """Re-verify ``candidate_dir`` and compare against ``expected``."""
    header = header_of(expected)
    result = {
        "label": label,
        "chain_ok": verify_chain(expected),
        "expected_verdict": expected.get("verdict"),
        "actual_verdict": None,
        "verdict_match": False,
        "checks_match": False,
        "candidate_match": False,
        "spec_match": header.get("spec_hash") == spec.hash,
        "error": None,
    }
    try:
        result["candidate_match"] = (
            tree_hash(candidate_dir) == header.get("candidate_tree_hash"))
        actual = run_verification(
            spec=spec, candidate_dir=candidate_dir, mode=mode, llm=llm,
            references=references, spec_label=spec_label,
            candidate_label=str(candidate_dir),
        )
    except (OSError, RuntimeError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    result["actual_verdict"] = actual.get("verdict")
    result["verdict_match"] = actual.get("verdict") == expected.get("verdict")
    result["checks_match"] = (
        _check_summary(actual) == _check_summary(expected))
    return result


def replay_document(
    data,
    *,
    spec: TaskSpec,
    candidate_dir: str | Path,
    mode: str,
    llm: LLMAdapter,
    references: ReferenceLookup,
    spec_label: str,
) -> list[dict]:
    """Replay a record / repair report; one result dict per record.

    Raises ValueError for non-VerifyGate input (via ``collect_records``)
    and for an invalid ``mode`` (v1.38.0 — validated before any record
    is replayed; the CLI's replay mode can come from a record's own
    header, an untrusted field, so it must never be trusted verbatim).
    The ``spec`` object is validated the same way (v1.39.0 — a
    directly-constructed TaskSpec bypasses parse_spec, and replay's
    per-record path catches only OSError/RuntimeError from the
    runner, so an invalid spec must fail here, up front, not escape
    as an unhandled error mid-replay).
    The labels are validated the same way (v1.40.0): the caller's
    ``spec_label``, and the candidate label the per-record path
    derives as ``str(candidate_dir)`` for the runner, must both be
    usable replay labels before any record is replayed.
    The candidate directory itself is validated the same way
    (v1.41.0): ``validate_candidate_dir`` judges the value itself,
    not its stringification — a non-path, empty, over-long, or
    not-a-directory candidate is a usage error up front, and its
    guarantees (a string form that is a non-empty label within the
    same ceiling) subsume the v1.40.0 derived-label check.
    Reconstruction copies are removed before returning; the caller's
    candidate directory is never modified.
    """
    validate_mode(mode)
    validate_spec(spec)
    validate_label(spec_label, "spec_label")
    validate_candidate_dir(candidate_dir, name="candidate_label")
    pairs = collect_records(data)
    is_report = isinstance(data, dict) and "attempts" in data
    results: list[dict] = []
    owned: list[Path] = []
    current_dir = Path(candidate_dir)
    try:
        for idx, (label, expected) in enumerate(pairs):
            if idx > 0:
                # Reconstruct this attempt's candidate: fresh copy of
                # the previous tree + the patch the report recorded.
                patch_info = data["attempts"][idx].get("patch_from_previous") \
                    if is_report else None
                if not patch_info or not patch_info.get("applied") \
                        or not patch_info.get("patch_text"):
                    results.append({
                        "label": label,
                        "chain_ok": verify_chain(expected),
                        "expected_verdict": expected.get("verdict"),
                        "actual_verdict": None,
                        "verdict_match": False,
                        "checks_match": False,
                        "candidate_match": False,
                        "spec_match": header_of(expected).get("spec_hash")
                                      == spec.hash,
                        "error": "no applied patch recorded for this "
                                 "attempt; cannot reconstruct it",
                    })
                    break
                new_dir = prepare_workspace(current_dir)
                owned.append(new_dir)
                try:
                    apply_patch(new_dir, patch_info["patch_text"])
                except PatchError as exc:
                    results.append({
                        "label": label,
                        "chain_ok": verify_chain(expected),
                        "expected_verdict": expected.get("verdict"),
                        "actual_verdict": None,
                        "verdict_match": False,
                        "checks_match": False,
                        "candidate_match": False,
                        "spec_match": header_of(expected).get("spec_hash")
                                      == spec.hash,
                        "error": f"recorded patch no longer applies: {exc}",
                    })
                    break
                current_dir = new_dir
            results.append(_replay_one(
                label=label, expected=expected, spec=spec,
                candidate_dir=current_dir, mode=mode, llm=llm,
                references=references, spec_label=spec_label,
            ))
    finally:
        for d in owned:
            shutil.rmtree(d, ignore_errors=True)
    return results


def replay_ok(results: list[dict]) -> bool:
    """True iff every record's chain is intact and its run reproduced."""
    return bool(results) and all(
        r["chain_ok"] and r["verdict_match"] and r["checks_match"]
        and r["candidate_match"] and r["spec_match"] and r["error"] is None
        for r in results
    )
