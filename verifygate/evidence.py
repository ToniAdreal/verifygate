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
    """Content hash of a directory tree: sorted (relpath, file-hash) lines."""
    root = Path(root)
    lines: list[str] = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts:
            rel = p.relative_to(root).as_posix()
            lines.append(f"{rel}:{_sha_bytes(p.read_bytes())}")
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
) -> dict:
    """Assemble an evidence record with a hash chain over its entries."""
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
