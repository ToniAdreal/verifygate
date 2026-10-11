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

Reference normalisation (v1.26.0): the lookup beside the diagnosis
was guarded against *raising*, but its return value was sealed
verbatim — a third-party lookup returning malformed references
(non-object items, non-string fields, extra keys) produced a record
that failed the chain's own structure check the moment it was
written, and a lookup returning 500 well-formed references sealed
all 500. Returned references now pass through
``tavily.normalise_references`` on this, the one sealing path: at
most ``MAX_REFERENCES`` items in exactly the adapters' normalised
shape, and a non-list return degrades to no references with the
malformation noted in the record.

Explanation bound (v1.27.0): the diagnosis text itself was the last
unbounded text on this sealing path — ``explain_failure`` could
return a string of any length and it was sealed verbatim (a
500,000-character stub reply produced a ~500 KB record that
verified INTACT), and even the first-party texts could run long:
the mock renders per failing check, so a maximum-size all-failing
spec produced ~67,000 characters, and the fallback below ~33,000.
The explanation — model, mock, or fallback, it makes no difference
— now passes through :func:`normalise_explanation` here, the one
sealing path: at most ``MAX_EXPLANATION_CHARS`` characters, with
the truncation marked in the sealed text itself so no reader
mistakes a cut diagnosis for a complete one. ``evidence``'s
structure check mirrors the cap.

Error-note bound (v1.29.0): the diagnosis entry's error notes were
still unbounded on one path — the runner's own formatted errors
truncate their exception text to 200 characters, but a lookup's
``last_error`` was copied into the record verbatim, so a
third-party lookup could seal a note of any length (and produce a
record the v1.29.0 structure mirror rejects). Both notes now pass
through :func:`_cap_error` on this sealing path, so every sealed
note is within ``MAX_ERROR_CHARS`` by construction.

Spec-object validation (v1.39.0): the ``spec`` is validated here
too, through ``spec.validate_spec`` — see that helper's docstring.
Between them, every caller-supplied run parameter that is sealed
into the record (mode, spec name/brief/checks/timeouts, and the
spec hash) is now validated at this one sealing path's entry,
whatever route the caller took to build it.

Mode validation (v1.38.0): the run ``mode`` is validated here, at
the one sealing path's entry (``evidence.validate_mode``), before
any workspace is prepared or any check runs. Before this, the
mode was validated only by the CLI's argparse choices: a library
caller passing ``bogus``, ``REAL``, ``judge `` (trailing space),
an empty string, ``None``, a number, or a shell fragment
(``judge; echo pwned``) got a fully-run PASS/FAIL whose record
failed its own verification instantly — the header mode is not a
real mode, and the replay string (which interpolates the mode
raw) matches no template; the fragment form also smuggled a
second shell command into the replay string a reader is told to
copy and run. An invalid mode is now a ``ValueError`` before any
work happens (the CLI maps it to its exit-2 usage error).

Usage sealing normalisation (v1.35.0): the last verbatim inputs on
this path were the adapter-supplied names (``llm.name`` /
``references.name``, sealed into the diagnosis entry) and the
usage calls drained from the adapter (sealed into the
``llm_usage`` entry). An adapter whose name was empty or enormous,
or which recorded a malformed call (a non-object ``usage`` crashed
``build_record`` outright; a 500,000-character model string or an
int ``finish_reason`` sealed a record that failed its own chain
verification), could still break the "a record VerifyGate emits
must verify" rule. Both now pass through the ``evidence``
normalisation helpers on this, the one sealing path — exactly as
the explanation, the references, and the error notes already did.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .evidence import (
    MAX_ERROR_CHARS,
    MAX_EXPLANATION_CHARS,
    build_record,
    normalise_adapter_name,
    normalise_usage_calls,
    quote_label,
    tree_hash,
    validate_mode,
)
from .llm import LLMAdapter
from .sandbox import prepare_workspace
from .spec import TaskSpec, validate_spec
from .tavily import (
    MAX_REFERENCES,
    ReferenceLookup,
    diagnosis_query,
    normalise_references,
)
from .validators import overall_verdict, run_checks


def normalise_explanation(text: str) -> str:
    """Bound a diagnosis text to the sealing cap (v1.27.0).

    A text at or under ``MAX_EXPLANATION_CHARS`` is returned
    unchanged — every honest diagnosis (the longest committed
    real-mode one is 2,022 characters) passes through byte-identical.
    A longer text is cut to exactly the cap, ending with a marker
    that names the limit, so the sealed record states plainly that
    its diagnosis was truncated instead of silently dropping the
    tail. Applied to model, mock, and fallback text alike: this is
    the one sealing path, and ``evidence`` mirrors the cap.
    """
    if len(text) <= MAX_EXPLANATION_CHARS:
        return text
    marker = (
        f"\n\n[Diagnosis truncated by VerifyGate at the "
        f"{MAX_EXPLANATION_CHARS}-character sealing limit]"
    )
    return text[:MAX_EXPLANATION_CHARS - len(marker)] + marker


def _cap_error(value):
    """Bound a diagnosis error note to the sealing cap (v1.29.0).

    ``None`` passes through; a string longer than
    ``MAX_ERROR_CHARS`` is truncated to it. Applied to both error
    notes the runner seals, so a lookup's ``last_error`` — the one
    note the runner does not format itself — cannot seal a record
    the structure check then rejects for its length.
    """
    if isinstance(value, str) and len(value) > MAX_ERROR_CHARS:
        return value[:MAX_ERROR_CHARS]
    return value


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
    # Fail fast on an invalid mode (v1.38.0): no workspace, no
    # checks, no adapter calls for a run whose record could never
    # verify (see the module docstring).
    validate_mode(mode)
    # Fail fast on an invalid spec object (v1.39.0): parse_spec's
    # rules bind only the JSON path, but TaskSpec is a plain
    # dataclass a library caller can construct directly — a
    # whitespace command, a bool/float timeout, an empty check
    # list, an over-long name, or an empty/mismatched raw (whose
    # hash binds none of the fields that ran) must be a usage
    # error here, before any workspace exists, never a run.
    validate_spec(spec)
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
                check_dicts,
                normalise_adapter_name(llm.name, "unknown-llm-adapter"),
                diagnosis_error)
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
                raw_refs = references.lookup(query)
            except Exception as exc:  # lookup must never break a run
                reference_error = (
                    f"reference lookup failed ({type(exc).__name__}): "
                    f"{str(exc)[:200]}")
                raw_refs = []
            if raw_refs is None:
                raw_refs = []
            if not isinstance(raw_refs, list):
                # A lookup returning a non-list has not produced
                # references; sealing it verbatim would emit a record
                # that fails the chain's own structure check. Degrade
                # to no references and note it in the record, exactly
                # like a lookup failure.
                if reference_error is None:
                    reference_error = (
                        "reference lookup returned a malformed result "
                        "(not a list); references skipped")
                raw_refs = []
            # References are sealed verbatim into the record, so their
            # shape is normalised here — the one sealing path — no
            # matter which lookup implementation produced them: at
            # most MAX_REFERENCES items, each exactly the adapters'
            # {title, url, snippet, source} string shape with the
            # per-field caps evidence's structure check mirrors.
            refs = normalise_references(raw_refs, MAX_REFERENCES)
            if reference_error is None:
                reference_error = getattr(references, "last_error", None)
        diagnosis = {
            # The one sealing path bounds the text (v1.27.0): model,
            # mock, and fallback explanations are all truncated to
            # MAX_EXPLANATION_CHARS here, so no adapter — and no
            # maximum-size spec — can bloat the sealed record.
            "explanation": normalise_explanation(explanation),
            "references": refs,
            # Adapter/lookup names are sealed verbatim no longer
            # (v1.35.0): they pass through the same normalisation
            # as every other adapter-supplied value on this path.
            "llm_adapter": normalise_adapter_name(
                llm.name, "unknown-llm-adapter"),
            "reference_source": normalise_adapter_name(
                references.name, "unknown-reference-lookup"),
            # Both error notes are capped on the same path
            # (v1.29.0): the runner's own are short by construction,
            # but a lookup's last_error is the lookup's own text.
            "reference_error": _cap_error(reference_error),
            "diagnosis_error": _cap_error(diagnosis_error),
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
    # The drained calls are adapter-supplied, so they are normalised
    # on this sealing path first (v1.35.0): a malformed call must
    # never crash build_record or seal a record that fails its own
    # verification.
    llm_usage = normalise_usage_calls(
        llm.drain_calls() if hasattr(llm, "drain_calls") else [])
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
