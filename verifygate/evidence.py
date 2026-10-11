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

Record metadata (v1.15.0): the record's *top-level* fields sat outside
the chain entirely — ``record_hash`` covered only the entries plus the
verdict — so the ``replay`` command (the field a reader is told to
copy, paste, and run) could be replaced with an arbitrary shell
command, ``signing_key_is_default_dev_key`` flipped to hide that the
labelled public dev key signed the record, and ``version`` /
``created_at`` rewritten at will, all while the record verified
INTACT and the web view printed the rewritten fields. Chain v3
(VerifyGate >= 1.15) seals those fields into ``record_hash`` itself.
For legacy v1/v2 records, verification instead enforces the metadata
*consistency* rules a genuine record always satisfies (see
``_metadata_ok``): exact top-level field set, semver ``version``,
timezone-aware ISO ``created_at``, the dev-key flag equal to the key
that actually verifies the signature, and a ``replay`` string that
matches one of the two command templates the runner emits, in the
header's mode, with no shell metacharacters in its labels. A legacy
rewrite that stays inside those rules (a different plausible date)
is not detectable — that residual is exactly what v3 removes for new
records.

Header sealing (v1.11.0): the header entry was the one exception to
full-entry hashing — verification recomputed its hash from a fixed
four-field subset, so an *extra* field injected into the header was
authenticated by nothing and a tampered record still verified, for
both chain versions (every other entry's extra fields already broke
the chain). Verification now hashes the header's full content
(minus ``hash``; minus ``kind`` for v1) and ``_structure_ok`` requires
the header to carry exactly the fields ``build_record`` emits, so an
injected header field fails two independent ways. Records emitted by
any earlier version have exactly those fields, so they still verify.

Record consistency (v1.12.0): structure validation previously checked
that fields *exist* (and, since v1.7.0, that a check's name/command
are strings), but never that a record is internally consistent — a
record sealed (or re-chained and re-signed with the public dev key)
with a PASS verdict over FAIL checks, a check claiming PASS with
exit code 1, a negative or non-numeric duration, a header naming a
mode that does not exist, or an ``llm_usage`` entry whose stated
total disagrees with its own calls verified INTACT, because the
hash chain faithfully authenticates whatever nonsense it was fed.
``_structure_ok`` now mirrors the *whole* emission contract of
``build_record`` + the runner: exact field sets and value types per
entry kind, header hashes in SHA-256 hex form and a real mode, each
check's verdict recomputed from its exit code / timeout exactly as
``validators`` computes it, the record's verdict recomputed from its
checks exactly as ``overall_verdict`` computes it, and a usage
entry's total recomputed from its calls exactly as ``build_record``
computes it. Every committed record satisfies these rules (they were
all runner-emitted), so they all still verify.

Diagnosis resilience (v1.19.0): since that version the runner seals
an optional ``diagnosis_error`` field in the diagnosis entry (see
``runner.py``): when the LLM explanation call fails, the record
carries a deterministic fallback explanation plus the failure's
reason, instead of the run producing no record at all. The field is
optional here for the same reason ``reference_error`` is — records
sealed before it existed do not carry it, and they still verify.

Replay-label quoting (v1.21.0): the metadata rules above require a
record's ``replay`` string to match the emitted templates with
path-like labels — no whitespace, no shell metacharacters — but the
runner interpolated the caller's spec/candidate labels *raw*. Any
perfectly ordinary path containing a space, a quote, or a non-ASCII
character (a checkout under ``My Projects``, a home directory with a
Chinese name) therefore produced a record that failed its own
verification the moment it was sealed: ``run`` exited 0 with a PASS,
and ``verify`` on the record it had just written reported CHAIN
INVALID. Labels are now interpolated through :func:`quote_label`:
a label that is already a bare path token is emitted unchanged (so
every ordinary run's replay string is byte-identical to before),
and anything else is single-quoted exactly as ``shlex.quote``
quotes it — inert to the shell, still copy-pasteable, and still
actually runnable. The metadata templates accept both forms; an
*unquoted* label carrying whitespace or metacharacters is still
rejected, exactly as before.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

DEFAULT_DEV_KEY = "verifygate-dev-key-NOT-FOR-PRODUCTION"
GENESIS = "verifygate/genesis/v1"
CHAIN_VERSION = 3


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

    The sandbox's candidate size bounds are enforced first (v1.23.0):
    binding a record to an oversized candidate directory (``verify
    --candidate``, replay) is a usage error, not an invitation to
    read an unbounded tree into the hash.
    """
    from .sandbox import check_candidate_bounds, iter_candidate_files

    check_candidate_bounds(root)

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
    created_at = datetime.now(timezone.utc).isoformat()
    dev_key = signing_key() == DEFAULT_DEV_KEY
    # Chain v3: the top-level metadata is sealed into the record hash
    # alongside the verdict, so the replay command, the sealing time,
    # the emitting version, and the dev-key disclosure cannot be
    # rewritten after the fact (see the module docstring).
    record_hash = _sha(prev + _canonical({
        "verdict": verdict,
        "version": __version__,
        "created_at": created_at,
        "replay": replay_command,
        "signing_key_is_default_dev_key": dev_key,
    }))
    signature = hmac.new(
        signing_key().encode("utf-8"), record_hash.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return {
        "tool": "verifygate",
        "version": __version__,
        "chain_version": CHAIN_VERSION,
        "created_at": created_at,
        "verdict": verdict,
        "replay": replay_command,
        "entries": entries,
        "record_hash": record_hash,
        "signature": signature,
        "signing_key_is_default_dev_key": dev_key,
    }


def _sealed_payload(record: dict, chain_version: int) -> dict:
    """The top-level payload ``record_hash`` is computed over.

    Chain v1/v2 sealed only the verdict; chain v3 also seals the
    record's metadata fields (see the module docstring).
    """
    if chain_version >= 3:
        return {
            "verdict": record["verdict"],
            "version": record["version"],
            "created_at": record["created_at"],
            "replay": record["replay"],
            "signing_key_is_default_dev_key":
                record["signing_key_is_default_dev_key"],
        }
    return {"verdict": record["verdict"]}


def update_replay_command(record: dict, replay_command: str) -> None:
    """Replace a record's ``replay`` field, re-sealing if needed.

    The repair loop rewrites a patched attempt's replay field after
    the runner seals its record (the runner's string points at the
    ephemeral patched copy). Under chain v3 the field is part of the
    sealed payload, so the record hash and signature are recomputed
    here — the only sanctioned post-seal edit. Under v1/v2 the field
    was unsealed metadata and is simply replaced.
    """
    record["replay"] = replay_command
    version = record.get("chain_version", 1)
    if version >= 3:
        prev = record["entries"][-1]["hash"]
        record["record_hash"] = _sha(
            prev + _canonical(_sealed_payload(record, version)))
        record["signature"] = hmac.new(
            signing_key().encode("utf-8"),
            record["record_hash"].encode("utf-8"),
            hashlib.sha256).hexdigest()


_MODES = ("judge", "real")
_USAGE_TOKEN_KEYS = {"prompt_tokens", "completion_tokens", "total_tokens"}


def _is_int(value) -> bool:
    """A real JSON integer — ``bool`` is an ``int`` subclass, so a
    sealed ``true`` must not pass as the number 1 (cf. spec timeouts)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_sha256_hex(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        c in "0123456789abcdef" for c in value)


def _check_entry_ok(entry: dict) -> bool:
    """One check entry must be exactly what ``validators`` emits."""
    if set(entry) != {"kind", "hash", "name", "command", "exit_code",
                      "duration_ms", "stdout_tail", "stderr_tail",
                      "timed_out", "verdict"}:
        return False
    # Since v1.7.0 the spec parser only seals string fields, and
    # structure verification mirrors that: a check whose name/command
    # is not a non-empty string (e.g. a JSON number, which the pre-1.7
    # parser happily sealed) is not a shape build_record can emit
    # from a valid spec.
    if not isinstance(entry["name"], str) or not entry["name"].strip() \
            or not isinstance(entry["command"], str) \
            or not entry["command"].strip():
        return False
    if not _is_int(entry["duration_ms"]) or entry["duration_ms"] < 0:
        return False
    if not isinstance(entry["stdout_tail"], str) \
            or not isinstance(entry["stderr_tail"], str):
        return False
    if not isinstance(entry["timed_out"], bool):
        return False
    exit_code = entry["exit_code"]
    if exit_code is not None and not _is_int(exit_code):
        return False
    # The verdict is not free text: validators decide it from the exit
    # code and the timeout alone, so recompute it the same way. A
    # check claiming PASS with exit code 1 (or on a timeout), or FAIL
    # with exit code 0, is a shape no real run can emit.
    expected = "PASS" if (not entry["timed_out"] and exit_code == 0) \
        else "FAIL"
    return entry["verdict"] == expected


def _diagnosis_entry_ok(entry: dict) -> bool:
    """One diagnosis entry must be exactly what the runner emits.

    The field set is the runner's diagnosis dict: the five base
    fields, plus ``reference_error`` — optional because records sealed
    before v0.7.0 do not carry it — and ``diagnosis_error`` — optional
    because records sealed before v1.19.0 do not carry it (that is
    when the runner began sealing a diagnosis failure instead of
    letting it destroy the record). References are the lookup
    adapters' normalised shape: {title, url, snippet, source},
    all strings.
    """
    keys = set(entry)
    base = {"kind", "hash", "explanation", "references", "llm_adapter",
            "reference_source"}
    if not base <= keys <= base | {"reference_error", "diagnosis_error"}:
        return False
    if not isinstance(entry["explanation"], str):
        return False
    if not isinstance(entry["llm_adapter"], str) \
            or not isinstance(entry["reference_source"], str):
        return False
    if "reference_error" in entry and not (
            entry["reference_error"] is None
            or isinstance(entry["reference_error"], str)):
        return False
    if "diagnosis_error" in entry and not (
            entry["diagnosis_error"] is None
            or isinstance(entry["diagnosis_error"], str)):
        return False
    references = entry["references"]
    if not isinstance(references, list):
        return False
    return all(
        isinstance(ref, dict)
        and set(ref) == {"title", "url", "snippet", "source"}
        and all(isinstance(ref[k], str)
                for k in ("title", "url", "snippet", "source"))
        for ref in references)


def _usage_entry_ok(entry: dict) -> bool:
    """One llm_usage entry: typed calls, and a total that adds up.

    ``build_record`` computes the entry's ``total_tokens`` as the sum
    of its calls' usage totals; a stated total that disagrees with
    the entry's own calls (a forged cheaper — or grander — bill,
    re-chained and re-signed) is not a shape it can emit. Call dicts
    themselves are adapter-defined, except for ``usage``: when
    present it must be a token-count object whose values are
    non-negative ints or None (the Token Factory adapter records
    None for a count the API did not return).
    """
    if set(entry) != {"kind", "hash", "calls", "total_tokens"}:
        return False
    calls = entry["calls"]
    if not isinstance(calls, list) or not calls:
        return False
    if not _is_int(entry["total_tokens"]) or entry["total_tokens"] < 0:
        return False
    total = 0
    for call in calls:
        if not isinstance(call, dict):
            return False
        usage = call.get("usage")
        if usage is None:
            continue
        if not isinstance(usage, dict) \
                or not set(usage) <= _USAGE_TOKEN_KEYS:
            return False
        if any(v is not None and not (_is_int(v) and v >= 0)
               for v in usage.values()):
            return False
        total += usage.get("total_tokens") or 0
    return entry["total_tokens"] == total


_RECORD_KEYS = {"tool", "version", "created_at", "verdict", "replay",
                "entries", "record_hash", "signature",
                "signing_key_is_default_dev_key"}
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
# A spec/candidate/record label as the runner interpolates it into a
# replay command: a path-like token with no whitespace and no shell
# metacharacters, so a sealed replay string can never smuggle a
# second command past a reader who copy-pastes it. ``<>`` are allowed
# for the ``<repair-report.json>`` placeholder the repair loop uses.
_LABEL = r"[A-Za-z0-9_./~@:+\-<>]+"
# The quoted form of a label (see quote_label): single-quoted, so
# every metacharacter inside is inert, with an embedded single quote
# carried as the '"'"' sequence ``shlex.quote`` produces (close the
# quote, a double-quoted quote, reopen the quote). Control characters
# are excluded: a one-line command cannot carry them, quoted or not.
_SQ = "'"
_QUOTED_LABEL = (
    _SQ + r"(?:[^'\r\n]|" + _SQ + '"' + _SQ + '"' + _SQ + r")*" + _SQ)
_ANY_LABEL = rf"(?:{_LABEL}|{_QUOTED_LABEL})"
_REPLAY_RES = (
    re.compile(
        rf"^python3 -m verifygate run --spec {_ANY_LABEL} "
        rf"--candidate {_ANY_LABEL} --mode (judge|real)$"),
    re.compile(
        rf"^python3 -m verifygate replay --record {_ANY_LABEL} "
        rf"--spec {_ANY_LABEL} --candidate {_ANY_LABEL} "
        rf"--mode (judge|real)  "
        rf"# attempt [1-9][0-9]*: patched copy, reconstructed from "
        rf"the report's recorded patch$"),
)


def quote_label(label: str) -> str:
    """Interpolate a spec/candidate label into a replay command.

    A label that is already a bare path token (every label in every
    committed record) is returned unchanged, so ordinary runs emit
    byte-identical replay strings. Anything else — a path with a
    space, a quote, a shell metacharacter, or a non-ASCII character —
    is single-quoted via ``shlex.quote``: the emitted command stays
    copy-pasteable and actually runs, and the record still verifies
    (before v1.21.0 the raw label broke the metadata template, so a
    record VerifyGate had just sealed reported CHAIN INVALID). A
    label containing control characters cannot be represented in a
    one-line command at all; it degrades to the placeholder form the
    repair loop already uses for its record argument, which a reader
    edits before running — never to a raw, template-breaking string.
    """
    if re.fullmatch(_LABEL, label):
        return label
    if any(ord(c) < 32 or ord(c) == 127 for c in label):
        return "<unprintable-label>"
    return shlex.quote(label)


def _metadata_ok(record: dict) -> bool:
    """A record's top-level metadata must be what ``build_record``
    emits (see the module docstring's v1.15.0 note).

    Under chain v3 the metadata is additionally sealed into
    ``record_hash``, which catches *every* rewrite; these consistency
    rules are what legacy v1/v2 records get, and they also keep a v3
    record's metadata well-formed before its hash is recomputed.
    """
    if not isinstance(record, dict):
        return False
    if not set(record) <= _RECORD_KEYS | {"chain_version"} \
            or not _RECORD_KEYS <= set(record):
        return False
    if record.get("tool") != "verifygate":
        return False
    if not isinstance(record.get("version"), str) \
            or not _VERSION_RE.match(record["version"]):
        return False
    chain_version = record.get("chain_version", 1)
    if not _is_int(chain_version) or chain_version not in (1, 2, 3):
        return False
    created_at = record.get("created_at")
    if not isinstance(created_at, str):
        return False
    try:
        parsed = datetime.fromisoformat(created_at)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        return False
    # The disclosure flag must tell the truth about the key that the
    # signature check below will use: it is derived from the same
    # environment key, exactly as build_record derived it at sealing.
    flag = record.get("signing_key_is_default_dev_key")
    if not isinstance(flag, bool) or flag != (signing_key() == DEFAULT_DEV_KEY):
        return False
    if not _is_sha256_hex(record.get("record_hash")) \
            or not _is_sha256_hex(record.get("signature")):
        return False
    # The replay instruction must be one of the two commands the
    # runner / repair loop emit, in the mode the header seals.
    replay = record.get("replay")
    if not isinstance(replay, str):
        return False
    entries = record.get("entries") or []
    header_mode = entries[0].get("mode") if entries \
        and isinstance(entries[0], dict) else None
    return any(
        (m := pattern.match(replay)) and m.group(1) == header_mode
        for pattern in _REPLAY_RES)


_UNSPECIFIED = object()  # sentinel: "no verdict supplied" != verdict None


def _structure_ok(entries, verdict=_UNSPECIFIED) -> bool:
    """Validate a record's structure: kinds, order, fields, consistency.

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

    Since v1.12.0 the check goes beyond shape to *consistency* (see
    the module docstring): per-entry field sets and value types, and
    the verdicts/totals recomputed from the record's own parts. When
    ``verdict`` (the record's top-level verdict) is given, it too must
    be the verdict ``overall_verdict`` computes from the checks.
    """
    if not isinstance(entries, list) or len(entries) < 2:
        return False
    if any(not isinstance(e, dict) or not isinstance(e.get("hash"), str)
           for e in entries):
        return False
    header = entries[0]
    if header.get("kind") != "header":
        return False
    # Exactly the fields build_record emits — no more, no fewer. An
    # extra field was previously authenticated by nothing (the header
    # hash covered a fixed subset), so it must fail structurally too.
    if set(header) != {"kind", "hash", "spec_name", "spec_hash",
                       "candidate_tree_hash", "mode"}:
        return False
    if not isinstance(header["spec_name"], str) \
            or not header["spec_name"]:
        return False
    # The header binds the record to two SHA-256 hashes and to one of
    # the two modes the CLI can run; anything else is not a header a
    # real run seals.
    if not _is_sha256_hex(header["spec_hash"]) \
            or not _is_sha256_hex(header["candidate_tree_hash"]):
        return False
    if header["mode"] not in _MODES:
        return False
    phase = 0  # 0 = checks, 1 = diagnosis seen, 2 = llm_usage seen
    check_verdicts: list[str] = []
    for entry in entries[1:]:
        kind = entry.get("kind")
        if kind == "check":
            if phase != 0:
                return False
            if not _check_entry_ok(entry):
                return False
            check_verdicts.append(entry["verdict"])
        elif kind == "diagnosis":
            if phase != 0:
                return False  # out of order, or a second diagnosis
            phase = 1
            if not _diagnosis_entry_ok(entry):
                return False
        elif kind == "llm_usage":
            if phase == 2:
                return False  # a second usage entry
            phase = 2
            if not _usage_entry_ok(entry):
                return False
        else:
            return False
    if not check_verdicts:
        return False
    if verdict is not _UNSPECIFIED:
        # overall_verdict: PASS iff there is at least one check and
        # every check passed. A top-level verdict that disagrees with
        # the record's own checks — PASS stamped over a FAIL — is the
        # one forgery every consumer of this record cares about.
        expected = "PASS" if all(v == "PASS" for v in check_verdicts) \
            else "FAIL"
        if verdict != expected:
            return False
    return True


def verify_chain(record: dict) -> bool:
    """Recompute the hash chain + signature; True iff the record is intact."""
    try:
        entries = record["entries"]
        if not _structure_ok(entries, record.get("verdict")):
            return False
        if not _metadata_ok(record):
            return False
        version = record.get("chain_version", 1)
        if version not in (1, 2, 3):
            return False
        prev = _sha(GENESIS)
        # The header is hashed over its *full* content, like every
        # other entry: a fixed-subset recomputation here previously
        # left any extra header field unauthenticated. Untampered
        # records (both versions) carry exactly the emitted fields,
        # so their header hashes recompute identically.
        if version >= 2:
            header = {k: v for k, v in entries[0].items() if k != "hash"}
        else:  # chain v1 hashed the content only, never the kind
            header = {k: v for k, v in entries[0].items()
                      if k not in ("kind", "hash")}
        prev = _sha(prev + _canonical(header))
        if entries[0]["hash"] != prev:
            return False
        for entry in entries[1:]:
            if version >= 2:
                payload = {k: v for k, v in entry.items() if k != "hash"}
            else:  # chain v1 hashed the content only, never the kind
                payload = {k: v for k, v in entry.items()
                           if k not in ("kind", "hash")}
            prev = _sha(prev + _canonical(payload))
            if entry["hash"] != prev:
                return False
        record_hash = _sha(prev + _canonical(_sealed_payload(record, version)))
        if record_hash != record["record_hash"]:
            return False
        expected_sig = hmac.new(
            signing_key().encode("utf-8"), record_hash.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected_sig, record["signature"])
    except (KeyError, IndexError, TypeError):
        return False
