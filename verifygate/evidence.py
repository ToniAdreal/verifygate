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
    prev = _sha(prev + _canonical(header))
    entries.append({"kind": "header", "hash": prev, **header})
    for result in check_results:
        prev = _sha(prev + _canonical(result))
        entries.append({"kind": "check", "hash": prev, **result})
    if diagnosis is not None:
        prev = _sha(prev + _canonical(diagnosis))
        entries.append({"kind": "diagnosis", "hash": prev, **diagnosis})
    if llm_usage:
        usage_entry = {
            "calls": llm_usage,
            "total_tokens": sum(
                (c.get("usage") or {}).get("total_tokens") or 0
                for c in llm_usage
            ),
        }
        prev = _sha(prev + _canonical(usage_entry))
        entries.append({"kind": "llm_usage", "hash": prev, **usage_entry})
    record_hash = _sha(prev + _canonical({"verdict": verdict}))
    signature = hmac.new(
        signing_key().encode("utf-8"), record_hash.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return {
        "tool": "verifygate",
        "version": __version__,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "replay": replay_command,
        "entries": entries,
        "record_hash": record_hash,
        "signature": signature,
        "signing_key_is_default_dev_key": signing_key() == DEFAULT_DEV_KEY,
    }


def verify_chain(record: dict) -> bool:
    """Recompute the hash chain + signature; True iff the record is intact."""
    try:
        entries = record["entries"]
        prev = _sha(GENESIS)
        header = {k: entries[0][k] for k in
                  ("spec_name", "spec_hash", "candidate_tree_hash", "mode")}
        prev = _sha(prev + _canonical(header))
        if entries[0]["hash"] != prev:
            return False
        for entry in entries[1:]:
            payload = {k: v for k, v in entry.items() if k not in ("kind", "hash")}
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
