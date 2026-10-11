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
"""

from __future__ import annotations

from pathlib import Path

from .evidence import tree_hash, verify_chain


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

    Raises ValueError for non-VerifyGate input; OSError from hashing a
    missing candidate directory propagates to the caller.
    """
    pairs = collect_records(data)
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
