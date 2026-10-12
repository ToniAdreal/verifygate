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

Adapter validation (v1.42.0): ``llm`` and ``references`` are
validated up front the same way, through ``llm.validate_llm``
and ``tavily.validate_references`` — the per-record path catches
only ``OSError`` / ``RuntimeError`` from the runner, so a
malformed adapter (``None``, a string, a bare object) would
otherwise escape mid-replay as an unhandled ``AttributeError``
on a FAIL record, or silently replay a PASS record with no
adapter at all. A malformed adapter is now a usage error
before any record is replayed.

Malformed-document robustness (v1.47.0): the record being
replayed is untrusted, and two of replay's own reads assumed
its shape — ``verify.header_of`` (now guarded there) and
:func:`_check_summary`, which read ``.get`` on every entry the
record carried. A record whose ``entries`` was rewritten as a
list of strings, a bare string, or a number crashed replay with
an unhandled ``AttributeError`` / ``TypeError`` traceback —
past the per-record path's error handling — instead of simply
not reproducing. ``_check_summary`` now summarises only object
entries of a list ``entries`` (anything else summarises as no
checks, which cannot match a genuine run's summary), so a
malformed record replays as *not reproduced*, never as a crash.

Non-object patch linkage (v1.62.0): the reconstruction path read
each later attempt's ``patch_from_previous`` with ``.get`` on the
assumption it is an object — true of every report the repair loop
emits, but a report is an untrusted artifact, and v1.61.0's
narrative check already *fails* such a report rather than
trusting the field. A linkage rewritten as a bare string, a
number, a list, or ``true`` crashed :func:`replay_document` with
an unhandled ``AttributeError`` — past the per-record path, whose
doctrine since v1.47.0 is that a malformed record replays as *not
reproduced*, never as a crash (at the CLI the backstop caught it
as a usage error, exit 2, instead of the not-reproduced answer,
exit 1). The linkage is now judged with ``isinstance`` first: a
non-object linkage is simply no applied patch recorded, so the
attempt cannot be reconstructed and replay reports exactly that.

Result-aggregation validation (v1.56.0): :func:`replay_ok` —
the one function that turns per-record replay results into the
reproduced / not-reproduced answer — took its input on trust,
the v1.55.0 shape one layer up. An empty *iterator* aggregated
to True (``bool`` of an iterator is always true, and ``all`` of
nothing is true) while an empty list returned False; ``None``
silently returned False, an answer about records that were
never replayed; a string crashed with ``TypeError`` and a list
of plain dicts with ``KeyError``. The input is now materialised
and every entry validated through
:func:`validate_replay_result` before aggregation: an object
carrying the five match fields (``chain_ok`` /
``verdict_match`` / ``checks_match`` / ``candidate_match`` /
``spec_match``) as bools and ``error`` as a string or ``None``
— the shapes :func:`_replay_one` and the reconstruction path
emit. An empty input is False; a malformed one is one
``ValueError``, never an answer. ``verify.document_ok``
applies the same rule on its side (see that module).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .evidence import tree_hash, validate_label, validate_mode, verify_chain
from .llm import LLMAdapter, validate_llm
from .patch import PatchError, apply_patch
from .runner import run_verification
from .sandbox import prepare_workspace, validate_candidate_dir
from .spec import TaskSpec, validate_spec
from .tavily import ReferenceLookup, validate_references
from .verify import collect_records, header_of


def _check_summary(record: dict) -> list[tuple[str, str, int | None]]:
    """(name, verdict, exit_code) per check entry, in record order.

    Defensive by construction (v1.47.0): a malformed record's
    ``entries`` may be a non-list, or a list carrying non-object
    items — both summarise as the object check entries actually
    present (possibly none), never a crash. A genuine record's
    entries are all objects, so its summary is unchanged.
    """
    if not isinstance(record, dict):
        return []
    entries = record.get("entries")
    if not isinstance(entries, list):
        return []
    return [
        (e.get("name"), e.get("verdict"), e.get("exit_code"))
        for e in entries
        if isinstance(e, dict) and e.get("kind") == "check"
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
    The adapters are validated the same way (v1.42.0): a malformed
    ``llm`` / ``references`` is a usage error up front, not an
    ``AttributeError`` escaping the per-record path mid-replay
    (that path catches only ``OSError`` / ``RuntimeError``).
    A later attempt's patch linkage is judged by shape before it
    is read (v1.62.0): a non-object ``patch_from_previous`` is no
    applied patch recorded — the attempt cannot be reconstructed
    and its result says so — never an ``AttributeError``.
    Reconstruction copies are removed before returning; the caller's
    candidate directory is never modified.
    """
    validate_mode(mode)
    validate_spec(spec)
    validate_label(spec_label, "spec_label")
    validate_candidate_dir(candidate_dir, name="candidate_label")
    validate_llm(llm)
    validate_references(references)
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
                # The linkage is untrusted report data: a non-object
                # linkage (v1.62.0) is no applied patch recorded, not
                # an AttributeError past the per-record path.
                attempt = data["attempts"][idx] if is_report else None
                patch_info = attempt.get("patch_from_previous") \
                    if isinstance(attempt, dict) else None
                if not isinstance(patch_info, dict) \
                        or not patch_info.get("applied") \
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


def validate_replay_result(result, name: str = "result") -> dict:
    """Return a per-record replay result that is safe to aggregate.

    Result-aggregation validation (v1.56.0, see the module
    docstring): the fields :func:`replay_ok` judges must be the
    shapes the replay paths emit — the result an object, the
    five match fields (``chain_ok`` / ``verdict_match`` /
    ``checks_match`` / ``candidate_match`` / ``spec_match``)
    present and bools, and ``error`` a string or ``None``.
    Anything else is a ``ValueError`` naming the field, never
    an aggregate.
    """
    if not isinstance(result, dict):
        raise ValueError(
            f"{name} must be an object "
            f"(got {type(result).__name__})")
    for field in ("chain_ok", "verdict_match", "checks_match",
                  "candidate_match", "spec_match"):
        if field not in result or not isinstance(result[field], bool):
            raise ValueError(
                f"{name}.{field} must be a bool "
                f"(got {type(result.get(field)).__name__})")
    error = result.get("error")
    if error is not None and not isinstance(error, str):
        raise ValueError(
            f"{name}.error must be a string or None "
            f"(got {type(error).__name__})")
    return result


def replay_ok(results: list[dict]) -> bool:
    """True iff every record's chain is intact and its run reproduced.

    The input is materialised and every entry validated first
    (v1.56.0): an empty input — list or iterator — is False
    (an empty iterator previously aggregated to True), and a
    malformed entry is one ``ValueError``, never an answer and
    never a ``KeyError`` / ``TypeError`` crash.
    """
    if results is None or isinstance(results, (str, bytes)):
        raise ValueError(
            f"results must be an iterable of replay result objects "
            f"(got {type(results).__name__})")
    try:
        items = list(results)
    except TypeError:
        raise ValueError(
            f"results must be an iterable of replay result objects "
            f"(got {type(results).__name__})") from None
    for index, result in enumerate(items):
        validate_replay_result(result, name=f"results[{index}]")
    return bool(items) and all(
        r["chain_ok"] and r["verdict_match"] and r["checks_match"]
        and r["candidate_match"] and r["spec_match"] and r["error"] is None
        for r in items
    )
