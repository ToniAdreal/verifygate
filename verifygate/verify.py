"""Standalone verification of evidence records.

The evidence record is VerifyGate's product surface: a third party who
receives one should be able to check it *without* re-running any check,
without keys, and without network access. This module is that check:

  * the hash chain + HMAC signature of every record is recomputed
    (``evidence.verify_chain``) — any edit to a verdict, a check result,
    a diagnosis, or a sealed token-usage figure breaks it;
  * optionally, the record is bound to the artifacts on disk: the spec
    file's hash and the candidate directory's tree hash are recomputed
    and compared against the record's sealed header, answering "does
    this record describe *this* spec and *this* candidate?".

Input may be a single evidence record or a repair report, in which
case every attempt's embedded record is checked. For a repair report
the spec binding applies to every attempt (the loop never changes the
spec), while the candidate binding applies to attempt 0 only: later
attempts verify patched copies that no longer exist on disk, so their
candidate hash is reported as not bound rather than mismatched.

A repair report also carries *unchained* summary fields — its
``final_verdict``, ``spec_name`` and ``mode``, and each attempt
wrapper's ``verdict`` / ``record_hash`` duplicates. Those are
cross-checked against the sealed records they summarise
(``report_match``): editing a summary to disagree with the sealed
attempts fails verification, exactly like editing a record itself.

Since v1.8.0 the cross-check also covers the two summary figures a
reader is most likely to quote: ``llm_usage_total`` (recomputed from
the sealed ``llm_usage`` entries of every attempt plus the report's
``unsealed_llm_usage`` list) and ``rounds_used`` (recomputed from the
attempts' round numbers and the rejected patch preserved in
``failed_patch``). Before this, either figure could be rewritten —
a 5,367-token bill restated as 1 token, 99 rounds claimed for a
1-round repair — and ``verify`` still reported the report INTACT,
while the web view printed the rewritten figures. A report that
*omits* one of these fields (pre-v1.8 legacy reports) is not failed
for the omission: nothing false is displayed for a missing figure.

Malformed figures (v1.11.0): a tampered record whose sealed ``usage``
was rewritten as a bare string made the recomputation itself raise
``AttributeError`` — ``verify`` crashed with a traceback instead of
reporting the record as failed, and the web view crashed the same
way. Structurally unusable usage now totals as "malformed" (None),
which makes the cross-check mismatch and the document fail cleanly.

Repair narrative (v1.13.0): a repair report also tells a *story* in
unchained fields — why the loop stopped (``stopped_reason``), how
many rounds it was allowed (``max_rounds``), which patch produced
each attempt (``patch_from_previous``), and which patch was rejected
last (``failed_patch`` / ``last_patch_error``). None of it was
cross-checked: a PASS report could claim it stopped at max rounds,
a clean report could grow an invented rejection, attempt rounds
could be renumbered past ``max_rounds``, and the adapter credited
with the repair could be rewritten — all while ``verify`` reported
the report INTACT. The loop's narrative is deterministic given the
sealed attempts, so ``narrative_problems`` re-derives it: attempt
rounds start at 0 and strictly increase, each later attempt's patch
linkage names that attempt's round and an applied patch, a rejected
patch's round is a round no attempt claims, ``last_patch_error``
is exactly the preserved rejection's error, ``stopped_reason`` is
the one the loop's exit conditions produce, rounds never exceed
``max_rounds``, and the report's ``llm_adapter`` is the adapter the
sealed diagnoses, sealed usage calls, and patch linkages all name.

Cross-attempt spec binding (v1.14.0): the report cross-checks above
compare every attempt's header ``spec_name`` and ``mode`` against the
report's stated summary, but never compared the attempts' sealed
``spec_hash`` values *with each other*. The repair loop verifies
every attempt against the one spec it was given — it never changes
the spec — so all attempts of a genuine report are sealed under a
single spec hash. Before this, an attempt record sealed under a
*different* spec (same name and mode, different brief or checks —
re-sealed with the labelled public dev key, the forgery route the
v1.12.0 consistency checks already assume) could be spliced into a
report in place of the real attempt, and ``verify`` reported the
report INTACT, as long as the wrapper verdict/record-hash, the
figures, and the narrative were patched up to match. ``verify``
without a ``--spec`` argument had no way to notice the task itself
had been swapped between attempts. ``attempt_binding_problems``
now requires every attempt to carry the same sealed ``spec_hash``;
binding a report to an externally supplied spec (``--spec`` in
``verify``, and ``replay``) is unchanged and complementary.

Round-budget bound (v1.22.0): the narrative check also bounds
``max_rounds`` itself by the loop's ``MAX_ROUNDS``. Before this, the
field was judged only for shape (a non-negative int) and for the
rounds-used comparison — so a report claiming a billion-round budget
verified INTACT even though the loop (which now validates its
budget) can never have run under one; the CLI accepted such a budget
verbatim. A stated budget above the loop maximum is a narrative
problem like any other rewritten field, and the web view flags it
through the same SUMMARY MISMATCH banner (it renders
``narrative_problems``).

Patch-report bounds (v1.30.0): the narrative check also bounds
the patch linkage's text. The loop caps what it stores at the
source — the preserved ``patch_text`` at
``repair.MAX_PATCH_TEXT_CHARS``, a stored patch error at
``repair.MAX_PATCH_ERROR_CHARS`` — but verification judged those
unchained fields only for type, and the ``changed_files`` lists
and each linkage's ``llm_usage`` snapshot not at all: inflating
a committed report's preserved patch, rejection error,
changed-file list, or usage snapshot with 500,000-character
strings still verified INTACT (all four reproduced). The
narrative check now mirrors the source caps, plus generous
ceilings over the rest: at most ``MAX_CHANGED_FILES`` changed
files (the candidate file ceiling — a patch's result must fit
inside it), each name at most ``MAX_CHANGED_FILE_CHARS``, and a
usage snapshot shaped like a sealed entry's calls (at most
``MAX_CALLS_PER_USAGE_ENTRY`` calls, per-field string caps —
the same ceilings ``evidence`` enforces inside a record). The
committed reports sit far below every cap (patch texts peak at
571 characters, errors at 92, one changed file, one call).

Report-level bounds (v1.31.0): two unchained report surfaces the
narrative check never judged at all. The report-level
``unsealed_llm_usage`` list — calls never sealed into a record —
was totalled by ``derived_usage_total`` but never bounded: a
coordinated edit (inflating the list and the stated total
together) carrying 5,000 calls with 500,000-character model
strings still verified INTACT, as did each attempt's
``candidate_dir`` inflated to 500,000 characters (all reproduced).
The narrative check now bounds both: the unsealed list must pass
the same sealed-call ceilings as a patch linkage's usage snapshot
(``_patch_usage_ok`` — at most ``MAX_CALLS_PER_USAGE_ENTRY``
calls, per-field string caps), and every attempt's
``candidate_dir``, when present, must be a string of at most
``MAX_CANDIDATE_DIR_CHARS`` characters. The report's own
``spec_name`` / ``llm_adapter`` are likewise capped at the sealed
ceilings (``MAX_SPEC_NAME_CHARS`` / ``MAX_ADAPTER_NAME_CHARS``)
as defence in depth — they are already cross-checked against the
sealed attempts, but a bound is cheaper than a comparison the
reader must trust. Committed reports sit far below every cap
(unsealed usage peaks at 2 calls; candidate dirs are temp paths).

Report shape bounds (v1.32.0): a repair report's *shape* itself
was never judged. A single evidence record's top-level fields are
held to an exact field set (chain v3 seals them; legacy records get
metadata consistency rules), but a repair report — which is not
hash-chained at the top level at all — could carry any extra field
at any level: a 500,000-character string injected as a new
top-level field, as a new field on an attempt wrapper, or as a new
field on a patch linkage (applied or preserved) all verified
INTACT, as did the report's ``kind`` rewritten to anything at all,
its ``version`` rewritten to a 500,000-character string, and an
attempt list far longer than the loop's round budget can produce
(all reproduced). ``narrative_problems`` now judges the shape:
the top-level keys must be a subset of the fields the loop emits
(a legacy report that *omits* the fields added later — the
v0.2.0-era shape carries no ``failed_patch`` /
``llm_usage_total`` / ``unsealed_llm_usage`` — is a subset, not
an offence), every attempt wrapper's keys a subset of its six
emitted fields, every patch linkage's keys a subset of its eight
emitted fields (the seven base fields plus the v1.20.0
``error_kind``), ``kind`` when present must be ``repair-report``,
``version`` when present must be a semver string like the one a
record carries, and the attempt count must fit the loop's budget
(at most ``MAX_ROUNDS + 1`` attempts: the submitted candidate
plus one per round). Every committed report satisfies all of it.

Candidate-directory validation (v1.41.0): ``verify_document``'s
``candidate_dir`` binding argument is validated up front, through
``sandbox.validate_candidate_dir``. Before this, the binding
hashed whatever it was handed: a non-path value crashed
``tree_hash`` with an unhandled ``TypeError``, an empty string
hashed the *current* directory and reported a binding mismatch
for a tree nobody asked about, and a regular file raised the tree
hash's ``NotADirectoryError`` where the run path reports the same
file as ``FileNotFoundError``. ``None`` remains the "no binding
requested" sentinel and is not judged; any other malformed value
is a usage error before any record is checked. The attempt
``candidate_dir`` ceiling above now imports its constant from
``sandbox`` — the same 1,000 characters the directory validation
enforces at run time, one definition for both sides.

Generation resilience (v1.20.0): the repair loop's preserved
``failed_patch`` gained an ``error_kind`` — ``patch_rejected`` for a
diff the applier refused, ``generation_failed`` when the adapter
produced no proposal at all (the failure that previously destroyed
the whole report instead of ending it). ``derived_stopped_reason``
returns ``generation_failed`` for the latter, so a report claiming
that stop while its preserved failure says the applier rejected a
diff — or vice versa — fails the narrative check like any other
rewritten story. Reports sealed before v1.20.0 carry no
``error_kind`` and derive exactly as before.
"""

from __future__ import annotations

import re
from pathlib import Path

from .evidence import (
    MAX_ADAPTER_NAME_CHARS,
    MAX_CALLS_PER_USAGE_ENTRY,
    MAX_CALL_FIELD_CHARS,
    MAX_CALL_KEYS,
    MAX_FINISH_REASON_CHARS,
    MAX_MODEL_CHARS,
    MAX_PURPOSE_CHARS,
    tree_hash,
    verify_chain,
)
from .repair import MAX_PATCH_ERROR_CHARS, MAX_PATCH_TEXT_CHARS, MAX_ROUNDS
from .sandbox import (
    MAX_CANDIDATE_DIR_CHARS,
    MAX_CANDIDATE_FILES,
    validate_candidate_dir,
)
from .spec import MAX_SPEC_NAME_CHARS

# Ceilings over a patch linkage's changed-file list (v1.30.0): a
# patch's staged result must fit inside the candidate file
# ceiling, so no genuine linkage names more changed files than
# that; each name is a relative path inside the candidate, capped
# generously at 1,000 characters.
MAX_CHANGED_FILES = MAX_CANDIDATE_FILES
MAX_CHANGED_FILE_CHARS = 1000

# Ceiling over an attempt's stated candidate directory (v1.31.0):
# a filesystem path (a temp workspace or the caller's directory),
# capped generously at the same 1,000 characters as a changed-file
# name. The constant itself lives in ``sandbox`` since v1.41.0
# (imported above): the directory validation enforces it at run
# time, this check mirrors it at verification time, and the two
# must never drift apart.

# Report shape bounds (v1.32.0, see the module docstring): the exact
# field vocabularies ``repair.run_repair`` emits. A report or
# wrapper carrying any other field is not a shape the loop produces.
# Judgement is by subset, not equality: fields the loop added later
# (the report's ``failed_patch`` / ``llm_usage_total`` /
# ``unsealed_llm_usage``; a patch linkage's ``error_kind``) are
# omitted by legacy reports, and omission is never an offence.
REPORT_KEYS = {
    "tool", "version", "kind", "spec_name", "mode", "llm_adapter",
    "max_rounds", "rounds_used", "final_verdict", "stopped_reason",
    "last_patch_error", "failed_patch", "llm_usage_total",
    "unsealed_llm_usage", "attempts",
}
ATTEMPT_KEYS = {
    "round", "candidate_dir", "verdict", "record_hash",
    "patch_from_previous", "evidence",
}
PATCH_KEYS = {
    "round", "llm_adapter", "applied", "changed_files", "error",
    "error_kind", "llm_usage", "patch_text",
}
# The most attempts a genuine report can carry: the submitted
# candidate (round 0) plus one attempt per round in the budget.
MAX_ATTEMPTS = MAX_ROUNDS + 1
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def collect_records(data) -> list[tuple[str, dict]]:
    """Extract (label, record) pairs from a record or repair report."""
    if not isinstance(data, dict) or data.get("tool") != "verifygate":
        raise ValueError("not a VerifyGate record")
    if "attempts" in data:
        attempts = data["attempts"]
        if not isinstance(attempts, list) or not attempts:
            raise ValueError("repair report has no attempts")
        pairs = []
        for attempt in attempts:
            record = attempt.get("evidence") if isinstance(attempt, dict) else None
            if not isinstance(record, dict) or "entries" not in record:
                raise ValueError("repair report attempt is missing its evidence record")
            pairs.append((f"attempt {attempt.get('round', len(pairs))}", record))
        return pairs
    if "entries" in data:
        return [("record", data)]
    raise ValueError("not a VerifyGate record (no entries or attempts)")


def header_of(record: dict) -> dict:
    entries = record.get("entries") or []
    if entries and entries[0].get("kind") == "header":
        return entries[0]
    return {}


def _calls_total(calls) -> tuple[int, int] | None:
    """(call count, total tokens) for a call list, or None if malformed.

    Malformed means structurally unusable: a call that is not an
    object, a ``usage`` that is not an object, or a ``total_tokens``
    that is not a number. Such input previously crashed verification
    with an unhandled ``AttributeError``/``TypeError`` traceback (a
    tampered record whose sealed usage was rewritten as a string) —
    a tampered record must *fail* verification, never crash it.
    """
    if not isinstance(calls, list):
        return None
    total = 0
    for call in calls:
        if not isinstance(call, dict):
            return None
        usage = call.get("usage")
        if usage is None:
            usage = {}
        if not isinstance(usage, dict):
            return None
        tokens = usage.get("total_tokens")
        if tokens is None:
            tokens = 0
        if isinstance(tokens, bool) or not isinstance(tokens, (int, float)):
            return None
        total += tokens
    return len(calls), total


def derived_usage_total(data) -> dict | None:
    """Recompute a repair report's ``llm_usage_total`` from its parts.

    The authoritative figures live inside the attempts' sealed
    ``llm_usage`` entries; calls that were never sealed (a patch that
    did not apply ended the loop) are carried in the report's
    ``unsealed_llm_usage`` list, which is itself unchained — but any
    edit to it changes the derived total, so it is caught by the
    comparison against the stated ``llm_usage_total``. Returns None
    when the report's parts are too malformed to total.
    """
    if not isinstance(data, dict) or not isinstance(data.get("attempts"), list):
        return None
    calls = 0
    tokens = 0
    for attempt in data["attempts"]:
        if not isinstance(attempt, dict):
            return None
        record = attempt.get("evidence")
        if not isinstance(record, dict):
            return None
        for entry in record.get("entries", []):
            if isinstance(entry, dict) and entry.get("kind") == "llm_usage":
                part = _calls_total(entry.get("calls"))
                if part is None:
                    return None
                calls += part[0]
                tokens += part[1]
    unsealed = data.get("unsealed_llm_usage") or []
    part = _calls_total(unsealed)
    if part is None:
        return None
    return {"llm_calls": calls + part[0], "total_tokens": tokens + part[1]}


def derived_rounds_used(data) -> int | None:
    """Recompute a repair report's ``rounds_used`` from its parts.

    Every patch attempt consumes exactly one round. An applied patch
    produced an attempt whose ``round`` is that round number; a
    rejected patch that ended the loop is preserved as
    ``failed_patch`` with its round. Intermediate rejections are
    superseded by later attempts or by that final rejection, so the
    largest round number among the attempts and ``failed_patch`` is
    exactly the number of rounds used. Returns None when no usable
    round number exists.
    """
    if not isinstance(data, dict) or not isinstance(data.get("attempts"), list):
        return None
    rounds = [
        a.get("round") for a in data["attempts"]
        if isinstance(a, dict) and isinstance(a.get("round"), int)
        and not isinstance(a.get("round"), bool)
    ]
    failed = data.get("failed_patch")
    if isinstance(failed, dict) and isinstance(failed.get("round"), int) \
            and not isinstance(failed.get("round"), bool):
        rounds.append(failed["round"])
    return max(rounds) if rounds else None


def _is_round(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def derived_stopped_reason(data) -> str | None:
    """Recompute a repair report's ``stopped_reason`` from its parts.

    The loop's exit conditions leave exactly four outcomes, each
    determined by the sealed attempts plus the preserved rejection:

      * the final attempt PASSed -> ``passed``;
      * the loop ended on a rejected patch — the preserved
        ``failed_patch`` carries the last round used, a round no
        attempt claims -> ``patch_not_applicable``;
      * the loop ended because patch *generation* itself failed
        (v1.20.0) — the preserved ``failed_patch`` carries the last
        round used and ``error_kind: generation_failed`` ->
        ``generation_failed``;
      * anything else (the round budget ran out on applied patches,
        or no round was ever attempted) -> ``max_rounds``.

    A preserved rejection without an ``error_kind`` (every report
    sealed before v1.20.0) is an applier rejection, as it could
    only have been one then. Returns None when the report's parts
    are too malformed to tell.
    """
    if not isinstance(data, dict) or not isinstance(data.get("attempts"), list) \
            or not data["attempts"]:
        return None
    last = data["attempts"][-1]
    record = last.get("evidence") if isinstance(last, dict) else None
    final = record.get("verdict") if isinstance(record, dict) else None
    if final == "PASS":
        return "passed"
    if final != "FAIL":
        return None
    rounds = derived_rounds_used(data)
    if rounds is None:
        return None
    failed = data.get("failed_patch")
    if isinstance(failed, dict) and failed.get("round") == rounds:
        if failed.get("error_kind") == "generation_failed":
            return "generation_failed"
        return "patch_not_applicable"
    return "max_rounds"


def _patch_usage_ok(calls) -> bool:
    """True iff a patch linkage's ``llm_usage`` snapshot is within
    the ceilings ``evidence`` enforces over a sealed entry's calls
    (v1.30.0): a bounded list of call objects, each with a bounded
    key count and per-field string caps. The snapshot is a copy of
    calls the adapter recorded for one patch round — one generation
    call, at most doubled by the real adapter's empty-completion
    retry — so the sealed entry's generous ceilings apply as-is."""
    if not isinstance(calls, list) \
            or len(calls) > MAX_CALLS_PER_USAGE_ENTRY:
        return False
    text_caps = {
        "adapter": MAX_ADAPTER_NAME_CHARS,
        "model": MAX_MODEL_CHARS,
        "purpose": MAX_PURPOSE_CHARS,
        "finish_reason": MAX_FINISH_REASON_CHARS,
    }
    for call in calls:
        if not isinstance(call, dict) or len(call) > MAX_CALL_KEYS:
            return False
        for field, value in call.items():
            if field == "usage":
                continue
            cap = text_caps.get(field, MAX_CALL_FIELD_CHARS)
            if field == "finish_reason":
                if value is not None and not (
                        isinstance(value, str) and len(value) <= cap):
                    return False
            elif field in text_caps:
                if not (isinstance(value, str) and len(value) <= cap):
                    return False
            elif isinstance(value, str) and len(value) > cap:
                return False
    return True


def _patch_linkage_problems(patch: dict, problems: list[str]) -> None:
    """Bound one patch linkage's stored text (v1.30.0): the
    preserved proposal, the changed-file list, and the usage
    snapshot. Only fields that are present are judged, matching
    the narrative check's legacy policy."""
    if patch.get("patch_text") is not None \
            and isinstance(patch.get("patch_text"), str) \
            and len(patch["patch_text"]) > MAX_PATCH_TEXT_CHARS:
        problems.append("patch text exceeds the stored maximum")
    changed = patch.get("changed_files")
    if changed is not None and isinstance(changed, list) and (
            len(changed) > MAX_CHANGED_FILES
            or any(isinstance(f, str) and len(f) > MAX_CHANGED_FILE_CHARS
                   for f in changed)):
        problems.append("patch changed_files exceeds the stored bounds")
    if patch.get("llm_usage") is not None \
            and not _patch_usage_ok(patch.get("llm_usage")):
        problems.append("patch llm_usage exceeds the stored bounds")


def narrative_problems(data) -> list[str]:
    """Ways a repair report's unchained narrative disagrees with its
    sealed attempts (empty list = the narrative is the one the loop
    would have written). See the module docstring. Only fields that
    are present are judged — a legacy report that omits a field is
    not failed for the omission, matching the figures policy."""
    problems: list[str] = []
    if not isinstance(data, dict) or not isinstance(data.get("attempts"), list) \
            or not data["attempts"]:
        return ["attempts missing or malformed"]
    attempts = data["attempts"]
    adapter = data.get("llm_adapter")

    # Report shape bounds (v1.32.0): the field vocabularies the loop
    # emits, the report kind/version, and the attempt count — judged
    # by subset, so a legacy report omitting a later-added field is
    # not failed for the omission (see the module docstring).
    if set(data) - REPORT_KEYS:
        problems.append("repair report carries unknown top-level fields")
    if data.get("kind") is not None and data["kind"] != "repair-report":
        problems.append("report kind is not repair-report")
    if data.get("version") is not None and not (
            isinstance(data["version"], str)
            and _VERSION_RE.match(data["version"])):
        problems.append("report version is not a version string")
    if len(attempts) > MAX_ATTEMPTS:
        problems.append("attempt count exceeds the loop maximum")
    for attempt in attempts:
        if isinstance(attempt, dict) and set(attempt) - ATTEMPT_KEYS:
            problems.append("attempt carries unknown fields")
            break
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        patch = attempt.get("patch_from_previous")
        if isinstance(patch, dict) and set(patch) - PATCH_KEYS:
            problems.append("patch linkage carries unknown fields")
            break

    # Report-level bounds (v1.31.0): the report's own name/adapter
    # fields and its unsealed usage list, plus each attempt's stated
    # candidate directory — unchained surfaces judged only when
    # present, matching the legacy policy below.
    if isinstance(data.get("spec_name"), str) \
            and len(data["spec_name"]) > MAX_SPEC_NAME_CHARS:
        problems.append("report spec_name exceeds the sealed maximum")
    if isinstance(adapter, str) and len(adapter) > MAX_ADAPTER_NAME_CHARS:
        problems.append("report llm_adapter exceeds the sealed maximum")
    unsealed = data.get("unsealed_llm_usage")
    if unsealed is not None and not _patch_usage_ok(unsealed):
        problems.append("unsealed_llm_usage exceeds the stored bounds")
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        cdir = attempt.get("candidate_dir")
        if cdir is not None and not (
                isinstance(cdir, str)
                and len(cdir) <= MAX_CANDIDATE_DIR_CHARS):
            problems.append(
                "attempt candidate_dir exceeds the stored bounds")
            break

    # Attempt rounds: the loop numbers the submitted candidate 0 and
    # each applied patch with the round that produced it, in order.
    rounds: list[int] = []
    for attempt in attempts:
        if not isinstance(attempt, dict) or not _is_round(attempt.get("round")):
            problems.append("attempt round missing or malformed")
            rounds = []
            break
        rounds.append(attempt["round"])
    if rounds:
        if rounds[0] != 0:
            problems.append("first attempt is not round 0")
        if any(b <= a for a, b in zip(rounds, rounds[1:])):
            problems.append("attempt rounds do not strictly increase")

    # Patch linkage: attempt 0 is the submitted candidate (no patch);
    # every later attempt was produced by the patch its linkage
    # records — that round, applied, no error, by the report's adapter.
    for idx, attempt in enumerate(attempts):
        if not isinstance(attempt, dict):
            continue
        patch = attempt.get("patch_from_previous")
        if idx == 0:
            if patch is not None:
                problems.append("attempt 0 carries a patch_from_previous")
            continue
        if not isinstance(patch, dict):
            problems.append(f"attempt {attempt.get('round')} has no patch linkage")
            continue
        if patch.get("round") != attempt.get("round"):
            problems.append("patch round does not match its attempt round")
        if patch.get("applied") is not True or patch.get("error") is not None:
            problems.append("an attempt's patch is not an applied patch")
        if patch.get("error_kind") is not None:
            problems.append("an attempt's patch carries an error kind")
        if adapter is not None and patch.get("llm_adapter") is not None \
                and patch.get("llm_adapter") != adapter:
            problems.append("patch adapter does not match the report adapter")
        changed = patch.get("changed_files")
        if changed is not None and (
                not isinstance(changed, list)
                or not all(isinstance(f, str) for f in changed)):
            problems.append("patch changed_files is not a list of file names")
        if patch.get("patch_text") is not None \
                and not isinstance(patch.get("patch_text"), str):
            problems.append("patch text is not a string")
        _patch_linkage_problems(patch, problems)

    # The preserved rejection: a patch the applier refused, on a
    # round no attempt claims (each round is either an applied patch
    # that produced an attempt or a rejection, never both), within
    # the rounds used, by the report's adapter.
    failed = data.get("failed_patch")
    derived_rounds = derived_rounds_used(data)
    if failed is not None:
        if not isinstance(failed, dict) or failed.get("applied") is not False \
                or not isinstance(failed.get("error"), str) \
                or not failed.get("error"):
            problems.append("failed_patch is not a rejected patch")
        elif not _is_round(failed.get("round")) or failed["round"] < 1 \
                or failed["round"] in rounds \
                or (derived_rounds is not None
                    and failed["round"] > derived_rounds):
            problems.append("failed_patch round is not a rejected round")
        if isinstance(failed, dict) and adapter is not None \
                and failed.get("llm_adapter") is not None \
                and failed.get("llm_adapter") != adapter:
            problems.append("failed_patch adapter does not match the report adapter")
        # The failure kind, when recorded (v1.20.0+), names how the
        # preserved patch failed: rejected by the applier, or never
        # generated at all. Absent (legacy) means an applier
        # rejection — the only kind the loop could preserve then.
        if isinstance(failed, dict) \
                and failed.get("error_kind") not in (
                    None, "patch_rejected", "generation_failed"):
            problems.append("failed_patch error_kind is not a known failure kind")
        if isinstance(failed, dict):
            if set(failed) - PATCH_KEYS:
                problems.append("failed_patch carries unknown fields")
            if isinstance(failed.get("error"), str) \
                    and len(failed["error"]) > MAX_PATCH_ERROR_CHARS:
                problems.append("failed_patch error exceeds the stored maximum")
            if failed.get("patch_text") is not None \
                    and not isinstance(failed.get("patch_text"), str):
                problems.append("failed_patch patch text is not a string")
            _patch_linkage_problems(failed, problems)
    if isinstance(data.get("last_patch_error"), str) \
            and len(data["last_patch_error"]) > MAX_PATCH_ERROR_CHARS:
        problems.append("last_patch_error exceeds the stored maximum")
    # last_patch_error is exactly the preserved rejection's error:
    # the loop sets it on a rejection and never clears it.
    if "last_patch_error" in data:
        expected_error = failed.get("error") if isinstance(failed, dict) else None
        if data.get("last_patch_error") != expected_error:
            problems.append("last_patch_error does not match failed_patch")

    # The round budget: it must be a budget the loop itself accepts
    # (a non-negative int no larger than MAX_ROUNDS — see the module
    # docstring), and rounds used can never exceed it.
    max_rounds = data.get("max_rounds")
    if max_rounds is not None:
        if not _is_round(max_rounds) or max_rounds < 0:
            problems.append("max_rounds is not a non-negative integer")
        elif max_rounds > MAX_ROUNDS:
            problems.append("max_rounds exceeds the loop maximum")
        elif derived_rounds is not None and derived_rounds > max_rounds:
            problems.append("rounds used exceed max_rounds")

    # Why the loop stopped is determined by how it ended.
    if data.get("stopped_reason") is not None:
        derived_stop = derived_stopped_reason(data)
        if derived_stop is not None and data["stopped_reason"] != derived_stop:
            problems.append("stopped_reason does not match the loop outcome")

    # The credited adapter must be the one the sealed parts name:
    # every FAIL attempt's sealed diagnosis and every sealed usage
    # call was recorded under the adapter that actually ran.
    if adapter is not None:
        sealed_adapters = set()
        for attempt in attempts:
            record = attempt.get("evidence") if isinstance(attempt, dict) else None
            if not isinstance(record, dict):
                continue
            for entry in record.get("entries", []):
                if not isinstance(entry, dict):
                    continue
                if entry.get("kind") == "diagnosis" \
                        and isinstance(entry.get("llm_adapter"), str):
                    sealed_adapters.add(entry["llm_adapter"])
                if entry.get("kind") == "llm_usage" \
                        and isinstance(entry.get("calls"), list):
                    for call in entry["calls"]:
                        if isinstance(call, dict) \
                                and isinstance(call.get("adapter"), str):
                            sealed_adapters.add(call["adapter"])
        if sealed_adapters and sealed_adapters != {adapter}:
            problems.append("report adapter does not match the sealed attempts")
    return problems


def attempt_binding_problems(data) -> list[str]:
    """Ways a repair report's attempts disagree about the task they
    were verified against (empty list = every attempt was sealed
    under one spec). See the module docstring. The repair loop never
    changes the spec, so a genuine report's attempts all carry the
    header ``spec_hash`` of the attempt-0 spec; mode and spec *name*
    equality are checked separately in ``verify_document`` (they are
    also compared against the report's stated summary), but two
    different specs can share a name and a mode while demanding
    different checks — only the hash distinguishes them."""
    problems: list[str] = []
    if not isinstance(data, dict) or not isinstance(data.get("attempts"), list) \
            or not data["attempts"]:
        return ["attempts missing or malformed"]
    hashes: list[str] = []
    for attempt in data["attempts"]:
        record = attempt.get("evidence") if isinstance(attempt, dict) else None
        if not isinstance(record, dict):
            problems.append("attempt evidence missing or malformed")
            continue
        spec_hash = header_of(record).get("spec_hash")
        if not isinstance(spec_hash, str) or not spec_hash:
            problems.append("attempt header carries no spec hash")
            continue
        hashes.append(spec_hash)
    if len(set(hashes)) > 1:
        problems.append("attempts were not all sealed under the same spec")
    return problems


def check_record(
    record: dict,
    *,
    candidate_hash: str | None = None,
    spec_hash: str | None = None,
    bind_candidate: bool = True,
) -> dict:
    """Verify one record's chain and (optionally) its header bindings."""
    header = header_of(record)
    result = {
        "verdict": record.get("verdict"),
        "mode": header.get("mode"),
        "spec_name": header.get("spec_name"),
        "chain_ok": verify_chain(record),
        "candidate_match": None,
        "spec_match": None,
        "report_match": None,
    }
    if candidate_hash is not None and bind_candidate:
        result["candidate_match"] = (
            header.get("candidate_tree_hash") == candidate_hash)
    if spec_hash is not None:
        result["spec_match"] = header.get("spec_hash") == spec_hash
    return result


def verify_document(
    data,
    *,
    candidate_dir: str | Path | None = None,
    spec_hash: str | None = None,
) -> list[tuple[str, dict]]:
    """Verify a record / repair report document; returns (label, result).

    Raises ValueError for non-VerifyGate input and for a malformed
    ``candidate_dir`` (v1.41.0 — validated before any record is
    checked); a missing / not-a-directory candidate raises
    FileNotFoundError, the run path's one message for that fault.
    ``candidate_dir=None`` requests no candidate binding and is
    not judged.
    """
    pairs = collect_records(data)
    if candidate_dir is not None:
        validate_candidate_dir(candidate_dir)
    candidate_hash = tree_hash(candidate_dir) if candidate_dir is not None else None
    is_report = len(pairs) > 1 or (pairs and pairs[0][0] != "record")
    results = []
    for idx, (label, record) in enumerate(pairs):
        # In a repair report only attempt 0 is the submitted candidate;
        # later attempts are patched copies (see module docstring).
        bind_candidate = not is_report or idx == 0
        results.append((label, check_record(
            record,
            candidate_hash=candidate_hash,
            spec_hash=spec_hash,
            bind_candidate=bind_candidate,
        )))
    if is_report:
        # The report's summary fields are not inside any hash chain;
        # cross-check them against the sealed records they summarise
        # so a summary edited to disagree with the attempts fails.
        attempts = data["attempts"]
        report_match = (
            data.get("final_verdict") == pairs[-1][1].get("verdict")
            and all(
                isinstance(a, dict)
                and a.get("verdict") == a["evidence"].get("verdict")
                and a.get("record_hash") == a["evidence"].get("record_hash")
                for a in attempts
            )
            and all(
                header_of(record).get("spec_name") == data.get("spec_name")
                and header_of(record).get("mode") == data.get("mode")
                for _, record in pairs
            )
            # The quoted figures are recomputed from the sealed
            # attempts (see module docstring). A report that omits a
            # figure entirely (legacy) is not failed for the omission.
            and ("llm_usage_total" not in data
                 or data.get("llm_usage_total") == derived_usage_total(data))
            and ("rounds_used" not in data
                 or data.get("rounds_used") == derived_rounds_used(data))
            # The narrative fields (stopped_reason, max_rounds,
            # patch linkage, preserved rejection, credited adapter)
            # must be the ones the sealed attempts imply.
            and not narrative_problems(data)
            # Every attempt must have been sealed under the same
            # spec — the loop never changes the task mid-repair.
            and not attempt_binding_problems(data)
        )
        for _, result in results:
            result["report_match"] = report_match
    return results


def document_ok(results: list[tuple[str, dict]]) -> bool:
    """True iff every chain verifies and every requested binding matches."""
    return all(
        r["chain_ok"] and r["candidate_match"] is not False
        and r["spec_match"] is not False
        and r.get("report_match") is not False
        for _, r in results
    )
