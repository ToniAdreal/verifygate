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

Label validation (v1.40.0): the ``spec_label`` / ``candidate_label``
parameters are validated here too, through
``evidence.validate_label`` — see that helper's docstring. Between
them, every caller-supplied run parameter that is sealed into the
record (mode, spec, and now the two labels that form its replay
string) is validated at this one sealing path's entry, whatever
route the caller took to supply it: a non-string label no longer
crashes ``quote_label`` after the checks have run, an empty label
no longer seals as a quoted empty path, and an over-long label no
longer seals a replay field of any length.

Candidate-directory validation (v1.41.0): ``candidate_dir`` itself
is validated here too, through ``sandbox.validate_candidate_dir``
— see that helper's docstring. It was the last run parameter
converted with ``Path(...)`` unchecked: a non-path value crashed
with an unhandled ``TypeError``, an empty string silently verified
the *current* directory's tree and sealed its hash under the
caller's candidate label, and a regular file's error depended on
which internal path noticed first. A malformed candidate is now a
``ValueError`` / ``FileNotFoundError`` here, before any workspace
is prepared or any check runs.

Adapter validation (v1.42.0): the ``llm`` and ``references``
arguments are validated here too, through ``llm.validate_llm``
and ``tavily.validate_references`` — see those helpers. They were
the last run parameters taken on trust: ``None`` / a string / a
bare object silently sealed a PASS with no adapter at all, and
crashed a FAIL run with an unhandled ``AttributeError`` on the
adapter's ``name`` after the guarded advisory call, losing the
record the checks had already decided. Validation is about
shape, not behaviour: an adapter whose callable methods *raise*
still degrades exactly as v1.19.0/v1.20.0 designed.

Signing-key validation (v1.45.0): the sealing key is resolved
here too, through ``evidence.signing_key`` — see that helper.
The key comes from the caller's environment, not an argument,
and was the last sealing input taken on trust: a
set-but-empty ``VERIFYGATE_SIGNING_KEY`` (the env fallback
applies only when the variable is unset) silently became an
empty HMAC key whose record disclosed a custom, non-dev key.
A malformed key is now a ``ValueError`` here, before any
workspace exists, exactly like the other run parameters.

Record-writer validation (v1.59.0): :func:`write_record` — the
one function that materialises a sealed record to disk, and the
CLI ``run`` handler's only write path — converted its
``out_path`` with a bare ``Path(...)`` and took its ``record``
on trust. The loaders got path validation in v1.46.0
(``jsonio.validate_file_path``); the writer was left: a
non-path ``out_path`` (``None``, an ``int``, a list, ``bytes``)
crashed with a raw ``TypeError``, an empty string resolved to
the current directory and surfaced as an ``IsADirectoryError``
instead of the one usage error, a non-object ``record`` (a
string, a list) was silently written as a JSON file no
VerifyGate consumer will ever accept as a record — overwriting
whatever file already sat at the destination — and a
non-serializable record escaped as a raw ``TypeError`` from
``json.dumps``, past the CLI handler's ``OSError``-only write
handling. Both arguments are now judged before anything is
written: the path through ``jsonio.validate_file_path`` (a
``JsonFileError``, a ``ValueError``), the record as a JSON
object that must serialise — serialised *first*, so a rejected
record leaves a pre-existing destination byte-for-byte
unchanged. Genuine filesystem faults (a missing parent, a
directory as the destination) stay ``OSError``, exactly as
every other write path reports them.

Error-note normalisation (v1.60.0): the two text helpers on this
sealing path judged only the *length* of what they were handed.
:func:`_cap_error` truncated a long string but copied any other
value verbatim — and the one note it does not format itself, a
lookup's ``last_error``, is adapter-supplied text a third-party
``ReferenceLookup`` may set to anything at all. An ``int`` or a
``dict`` note was sealed verbatim into the diagnosis entry,
whose structure check requires a string-or-``None`` note within
``MAX_ERROR_CHARS``: the record failed its own verification the
moment it was written. A non-serializable note (an exception
object, a set) was worse: ``build_record``'s canonical JSON
encoding raised a raw ``TypeError`` and the FAIL record the
checks had already decided was lost entirely — the v1.19.0 loss
class, through the note instead of the explanation.
:func:`normalise_explanation` likewise assumed a string and
crashed with a raw ``TypeError`` (``len()``) on any other type —
its runner call site guarantees a string today, but the helper
is the one sealing path for diagnosis text and now judges its
own input like every other sealing helper. A non-string error
note is now discarded for a deterministic runner-formatted
placeholder naming only the offending type (the note's content
is the lookup's own malformed value; nothing of it is sealed),
and a non-string explanation is one ``ValueError``. String
notes and explanations — every first-party value — pass through
exactly as before.

Adapter-surface resilience (v1.66.0): the adapters' *optional*
surface was still read directly on this path — ``llm.name`` /
``references.name`` while the diagnosis entry is assembled, and
``llm.drain_calls()`` at sealing time — although
``validate_llm`` / ``validate_references`` promise both stay
optional. A duck-typed adapter with no ``name`` crashed a FAIL
run with an ``AttributeError`` at that assembly (a PASS run,
which skips the diagnosis, noticed nothing), and a
``drain_calls`` that raised destroyed the record after every
check had run — the v1.19.0 loss class through the optional
surface instead of the advisory methods. Names are now read
through ``getattr`` (a missing name normalises to the fallback
label, exactly like an empty one), and the drain goes through
``llm.drain_calls_safely``: a missing, broken, or non-list
usage log degrades to no recorded usage, never to a lost
record. See the ``llm`` module docstring.
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
    signing_key,
    tree_hash,
    validate_label,
    validate_mode,
)
from .jsonio import validate_file_path, write_text_file
from .llm import LLMAdapter, drain_calls_safely, validate_llm
from .sandbox import prepare_workspace, remove_tree, validate_candidate_dir
from .spec import TaskSpec, validate_spec
from .tavily import (
    MAX_REFERENCES,
    ReferenceLookup,
    diagnosis_query,
    normalise_references,
    validate_references,
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

    A non-string input is one ``ValueError`` (v1.60.0): the
    helper previously assumed a string and crashed with a raw
    ``TypeError`` from ``len()`` — the runner's call site checks
    the adapter's return before calling this, but the sealing
    helper judges its own input like every other one.
    """
    if not isinstance(text, str):
        raise ValueError(
            "explanation must be a string, got "
            f"{type(text).__name__}")
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

    A non-string, non-``None`` value is normalised, not copied
    (v1.60.0): the structure check requires a string-or-``None``
    note, so an ``int`` / ``dict`` note sealed verbatim produced
    a record that failed its own verification, and a
    non-serializable note (an exception object, a set) crashed
    ``build_record``'s JSON encoding with a raw ``TypeError``
    and lost the record. The malformed value is discarded for a
    deterministic placeholder naming only its type — nothing of
    the lookup's own malformed content is sealed.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value[:MAX_ERROR_CHARS]
    return (f"malformed error note discarded "
            f"(got {type(value).__name__})")


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
    # Fail fast on invalid labels (v1.40.0): the labels form the
    # record's replay string, interpolated only after the checks
    # have run — a non-string label crashed there with a TypeError
    # and delivered no record, an empty or over-long one sealed a
    # replay field the metadata rules then had no length bound
    # for. Both labels are usage errors here, before any
    # workspace exists, never a run.
    validate_label(spec_label, "spec_label")
    validate_label(candidate_label, "candidate_label")
    # Fail fast on an invalid candidate directory (v1.41.0): a
    # non-path value crashed inside Path(...) below, an empty
    # string silently meant the current directory — the wrong tree,
    # sealed under the caller's label — and a regular file's error
    # depended on the path taken. Judged here, beside the labels,
    # before any workspace exists.
    validate_candidate_dir(candidate_dir)
    # Fail fast on malformed adapters (v1.42.0): a PASS would
    # otherwise seal with no adapter at all and a FAIL would crash
    # on the adapter's name after its guarded call, losing the
    # record. Judged here, before any workspace exists.
    validate_llm(llm)
    validate_references(references)
    # Fail fast on a malformed signing key (v1.45.0): the key is
    # resolved from the environment at sealing time, so a
    # set-but-empty / trivially short / control-carrying value
    # would otherwise be discovered only after every check had
    # run — and before this round it was not discovered at all:
    # it silently became the HMAC key while the record disclosed
    # a custom (non-dev) key. Resolving it here discards the
    # value; build_record resolves it again when it seals.
    signing_key()
    workspace = prepare_workspace(candidate_dir)
    try:
        results = run_checks(spec, workspace)
    finally:
        # The workspace is a throwaway copy: checks are done with it
        # the moment they have run, and leaving it behind leaks a full
        # candidate copy per run (a long CI/dogfood session can fill
        # the temp filesystem — observed in practice, Errno 28).
        remove_tree(workspace)
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
                normalise_adapter_name(
                    getattr(llm, "name", None), "unknown-llm-adapter"),
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
            # Both are optional surface (v1.66.0): read through
            # getattr, so a nameless adapter normalises to the
            # fallback label instead of crashing the assembly.
            "llm_adapter": normalise_adapter_name(
                getattr(llm, "name", None), "unknown-llm-adapter"),
            "reference_source": normalise_adapter_name(
                getattr(references, "name", None),
                "unknown-reference-lookup"),
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
    # verification. The drain itself is optional surface
    # (v1.66.0): a missing, raising, or non-list usage log degrades
    # to no recorded usage, never to a lost record.
    llm_usage = normalise_usage_calls(drain_calls_safely(llm))
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
    """Write ``record`` as JSON to ``out_path``; return the path.

    Record-writer validation (v1.59.0, see the module docstring):
    ``out_path`` is validated through
    :func:`jsonio.validate_file_path` — the same helper the
    loaders use (v1.46.0) — so a non-path or empty value is a
    ``JsonFileError`` (a ``ValueError``), never a raw
    ``TypeError`` from ``Path(...)`` and never an empty string
    silently meaning the current directory. ``record`` must be a
    dict: anything else was previously written verbatim as a
    JSON file no consumer accepts as a record. The record is
    serialised *before* the file is touched, and a
    non-serializable value (or a circular reference) is one
    ``ValueError`` — so a rejected record leaves a pre-existing
    destination byte-for-byte unchanged. Genuine filesystem
    faults (a missing parent directory, the destination being a
    directory) propagate as ``OSError``, as on every other write
    path.

    Special-file outputs (v1.74.0): the write itself goes
    through :func:`jsonio.write_text_file`, so an existing
    destination that is not a regular file (a FIFO, which
    blocked the write indefinitely; a device such as
    ``/dev/null``, which silently discarded the record while
    the CLI reported it written) is a ``JsonFileError``
    before anything is opened.
    """
    out_path = validate_file_path(out_path, "out_path")
    if not isinstance(record, dict):
        raise ValueError(
            "record must be a JSON object (dict), got "
            f"{type(record).__name__}")
    try:
        text = json.dumps(record, indent=2) + "\n"
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "record is not JSON-serializable: "
            f"{str(exc)[:200]}") from exc
    write_text_file(out_path, text)
    return out_path
