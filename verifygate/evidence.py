"""Evidence records: tamper-evident, replayable proof of a verdict.

An evidence record binds together the spec hash, the candidate tree hash,
and every check result. Entries are hash-chained (each entry's hash covers
the previous hash plus the entry's canonical content), and the final
record hash is signed with HMAC-SHA256. Anyone holding the record can:

  * recompute the chain and detect any edit (verify_chain), and
  * replay the checks with the printed replay command and expect the
    same verdict on the same candidate.

The signing key comes from VERIFYGATE_SIGNING_KEY; the default development
key is deliberately public and labelled as such — it demonstrates the
mechanism, it does not pretend to be production key management.

Chain versions: records sealed by VerifyGate >= 1.5 carry
``chain_version: 2`` — every entry's hash covers its ``kind`` as well as
its content. Earlier records (chain v1, no ``chain_version`` field) hashed
the content only, which left ``kind`` unauthenticated: a check entry
could be relabelled (say, as a diagnosis) without breaking the chain,
and consumers filter entries by kind. Verification therefore *also*
enforces the record's structure (see ``_structure_ok``) for both
versions — kinds, their order, and the fields each kind must carry —
so a relabelled legacy record fails verification too, while every
untampered legacy record still verifies exactly as before.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

DEFAULT_DEV_KEY = "verifygate-dev-key-NOT-FOR-PRODUCTION"
GENESIS = "verifygate/genesis/v1"
CHAIN_VERSION = 2


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tree_hash(root: str | Path) -> str:
    """Content hash of a directory tree: sorted (relpath, file-hash) lines.

    Uses the sandbox's containment traversal: symlinks pointing outside
    the candidate (and directory symlinks) are excluded, so the hash
    describes exactly the tree that was copied into the workspace and
    verified — never a host file reached through a link.
    """
    from .sandbox import iter_candidate_files

    lines = [
        f"{rel}:{_sha_bytes(source.read_bytes())}"
        for rel, source in iter_candidate_files(root)
    ]
    return _sha("\n".join(lines))


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def signing_key() -> str:
    return os.environ.get("VERIFYGATE_SIGNING_KEY", DEFAULT_DEV_KEY)


def build_record(
    *,
    spec_name: str,
    spec_hash: str,
    candidate_tree_hash: str,
    mode: str,
    check_results: list[dict],
    verdict: str,
    diagnosis: dict | None,
    replay_command: str,
    llm_usage: list[dict] | None = None,
) -> dict:
    """Assemble an evidence record with a hash chain over its entries.

    ``llm_usage`` carries the real model calls (model, purpose, token
    usage) made since the previous record was sealed. It is chained
    like every other entry, so editing a usage figure after the fact
    breaks verification exactly the way editing a verdict does. The
    deterministic mock records no calls, so Judge Mode never emits
    this entry.
    """
    entries: list[dict] = []
    prev = _sha(GENESIS)
    header = {
        "spec_name": spec_name,
        "spec_hash": spec_hash,
        "candidate_tree_hash": candidate_tree_hash,
        "mode": mode,
    }
    # Chain v2: the entry kind is part of the hashed payload, so an
    # entry's type is as tamper-evident as its content.
    prev = _sha(prev + _canonical({"kind": "header", **header}))
    entries.append({"kind": "header", "hash": prev, **header})
    for result in check_results:
        prev = _sha(prev + _canonical({"kind": "check", **result}))
        entries.append({"kind": "check", "hash": prev, **result})
    if diagnosis is not None:
        prev = _sha(prev + _canonical({"kind": "diagnosis", **diagnosis}))
        entries.append({"kind": "diagnosis", "hash": prev, **diagnosis})
    if llm_usage:
        usage_entry = {
            "calls": llm_usage,
            "total_tokens": sum(
                (c.get("usage") or {}).get("total_tokens") or 0
                for c in llm_usage
            ),
        }
        prev = _sha(prev + _canonical({"kind": "llm_usage", **usage_entry}))
        entries.append({"kind": "llm_usage", "hash": prev, **usage_entry})
    record_hash = _sha(prev + _canonical({"verdict": verdict}))
    signature = hmac.new(
        signing_key().encode("utf-8"), record_hash.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return {
        "tool": "verifygate",
        "version": __version__,
        "chain_version": CHAIN_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "replay": replay_command,
        "entries": entries,
        "record_hash": record_hash,
        "signature": signature,
        "signing_key_is_default_dev_key": signing_key() == DEFAULT_DEV_KEY,
    }


def _structure_ok(entries) -> bool:
    """Validate a record's entry structure: kinds, order, required fields.

    ``build_record`` only ever emits one shape — a header, one or more
    checks, at most one diagnosis, at most one llm_usage entry, in that
    order, each carrying the fields its kind requires. Chain v1 did not
    hash the ``kind`` field, so without this check an attacker could
    relabel an entry (a FAIL check becomes a "diagnosis", which
    consumers render as commentary instead of a check) and the chain
    would still recompute. A relabelled entry cannot satisfy the target
    kind's required fields, order, and uniqueness at once, so structure
    validation closes the hole for legacy records without invalidating
    any untampered one.
    """
    if not isinstance(entries, list) or len(entries) < 2:
        return False
    if any(not isinstance(e, dict) or not isinstance(e.get("hash"), str)
           for e in entries):
        return False
    header = entries[0]
    if header.get("kind") != "header":
        return False
    if any(not isinstance(header.get(k), str) or not header[k]
           for k in ("spec_name", "spec_hash", "candidate_tree_hash", "mode")):
        return False
    phase = 0  # 0 = checks, 1 = diagnosis seen, 2 = llm_usage seen
    checks = 0
    for entry in entries[1:]:
        kind = entry.get("kind")
        if kind == "check":
            if phase != 0:
                return False
            checks += 1
            if not entry.get("name") or not entry.get("command"):
                return False
            if entry.get("verdict") not in ("PASS", "FAIL"):
                return False
            exit_code = entry.get("exit_code")
            if "exit_code" not in entry or not (
                    exit_code is None or isinstance(exit_code, int)):
                return False
            if "duration_ms" not in entry:
                return False
        elif kind == "diagnosis":
            if phase != 0:
                return False  # out of order, or a second diagnosis
            phase = 1
            if "explanation" not in entry:
                return False
        elif kind == "llm_usage":
            if phase == 2:
                return False  # a second usage entry
            phase = 2
            if not isinstance(entry.get("calls"), list) \
                    or "total_tokens" not in entry:
                return False
        else:
            return False
    return checks >= 1


def verify_chain(record: dict) -> bool:
    """Recompute the hash chain + signature; True iff the record is intact."""
    try:
        entries = record["entries"]
        if not _structure_ok(entries):
            return False
        version = record.get("chain_version", 1)
        if version not in (1, 2):
            return False
        prev = _sha(GENESIS)
        header = {k: entries[0][k] for k in
                  ("spec_name", "spec_hash", "candidate_tree_hash", "mode")}
        if version == 2:
            header = {"kind": "header", **header}
        prev = _sha(prev + _canonical(header))
        if entries[0]["hash"] != prev:
            return False
        for entry in entries[1:]:
            if version == 2:
                payload = {k: v for k, v in entry.items() if k != "hash"}
            else:  # chain v1 hashed the content only, never the kind
                payload = {k: v for k, v in entry.items()
                           if k not in ("kind", "hash")}
            prev = _sha(prev + _canonical(payload))
            if entry["hash"] != prev:
                return False
        record_hash = _sha(prev + _canonical({"verdict": record["verdict"]}))
        if record_hash != record["record_hash"]:
            return False
        expected_sig = hmac.new(
            signing_key().encode("utf-8"), record_hash.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected_sig, record["signature"])
    except (KeyError, IndexError, TypeError):
        return False
