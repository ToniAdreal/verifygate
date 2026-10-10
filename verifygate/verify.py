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
