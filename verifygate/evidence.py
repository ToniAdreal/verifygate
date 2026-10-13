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

Check-count bound (v1.24.0): the spec parser caps a spec at
``MAX_CHECKS`` checks (each check runs sequentially with its own
timeout and is sealed into the record, so the count is the one
spec dimension that was still unbounded — a 5,000-check probe spec
carried an aggregate timeout budget of ~208 days, and a record
sealed over 5,000 checks verified INTACT). ``_structure_ok`` mirrors
the cap: a record carrying more check entries than any valid spec
can produce is not a shape ``build_record`` can emit from a real
run, for any chain version. Every committed record carries at most
2 checks, so they all still verify.

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

Explanation bound (v1.27.0): the diagnosis *references* were capped
in v1.26.0, but the diagnosis *text itself* was not — the runner
sealed whatever ``explain_failure`` returned, verbatim and at any
length. A stub adapter returning a 500,000-character explanation
sealed a ~500 KB record that verified INTACT, and even first-party
text could exceed any sane bound: the bundled mock renders ~670
characters per failing check, so a maximum-size spec (100 checks,
all failing) produced a ~67,000-character explanation, and the
runner's own deterministic fallback rendered ~33,000 characters on
the same shape. The runner now truncates the explanation on the one
sealing path (see ``runner.normalise_explanation``) to
:data:`MAX_EXPLANATION_CHARS`, with the truncation marked in the
text itself, and ``_structure_ok`` mirrors the cap: a record
carrying a longer explanation than any real run can seal fails
verification however it was sealed. The cap is generous — roughly
ten times the longest committed real-mode diagnosis (2,022
characters) and comfortably above what the real adapter's
4,096-token explanation budget can produce — so no honest diagnosis
is ever cut; it exists so one unbounded model reply cannot bloat
every copy of a record (and every rendering of it) forever.

Spec text bounds (v1.28.0): the spec's own strings were the mirror
image of the explanation gap — the parser capped a spec's check
count, timeouts, and (via the sandbox) its candidate's size, but
not the length of the text the spec carries. A probe spec with a
100,000-character name, a 500,000-character brief, and
100,000-character check names/commands was accepted verbatim; the
name and check names/commands are sealed into every record the spec
produces (and the brief is embedded verbatim in every real-mode
patch prompt, a paid call). The parser now caps those fields (see
``spec.py``) and ``_structure_ok`` / ``_check_entry_ok`` mirror the
sealed ones: header ``spec_name``, and each check's ``name`` and
``command``, must be within the parser's ceilings. The same mirror
covers the check entries' output tails: the sandbox already caps a
sealed tail at ``MAX_OUTPUT_CHARS`` plus its truncation marker
(:data:`MAX_TAIL_CHARS`), so a longer tail is not a shape a real
run emits either. Every committed record is far below every cap
(longest spec name 23 characters, longest command 20, longest tail
1,863), so they all still verify.

Diagnosis/usage metadata bounds (v1.29.0): two entry kinds still
carried unbounded text. A diagnosis entry's ``llm_adapter`` /
``reference_source`` names and its ``reference_error`` /
``diagnosis_error`` notes were checked only for being strings —
and an ``llm_usage`` entry's calls were checked only for their
``usage`` totals: the calls' *count* and their
``adapter`` / ``model`` / ``purpose`` / ``finish_reason`` strings
were unbounded. A record honestly sealed by ``build_record`` over
500,000-character adapter and error strings, plus 50 calls carrying
500,000-character model names, verified INTACT — a ~100 MB record.
Honest values are tiny and bounded by construction: adapter and
lookup names are code-defined identifiers (the longest committed
one is 20 characters), the runner truncates the exception text it
formats into an error note to 200 characters, and the calls sealed
into one record are bounded by the repair loop's round budget
times the real adapter's one empty-completion retry (at most 22;
the committed maximum is 2). ``_diagnosis_entry_ok`` and
``_usage_entry_ok`` now mirror generous caps over all of it, and
the two construction paths that could exceed them are capped at
the source: the runner truncates a lookup's ``last_error`` when it
copies it into the record (``runner._cap_error``), and the Token
Factory adapter falls back to its configured model when the API
returns an over-long model string, exactly as it already did for a
non-string one. Every committed fixture sits far below every cap,
so they all still verify.

Check-result numeric bounds (v1.33.0): a check entry's numbers were
the last unbounded values in a record. ``duration_ms`` was checked
only for being a non-negative int, so a record sealed over a
duration of 10**12 ms verified INTACT; ``exit_code`` was checked
only for being an int or None, so a timeout entry could carry an
exit code (the sandbox kills a timed-out check before it can exit
and seals None), a completed check could carry None, and an exit
code of 999,999 — no POSIX exit status or signal number — all
verified INTACT. ``_check_entry_ok`` now mirrors the sandbox's
emission contract: the duration is at most
:data:`MAX_DURATION_MS` (twice the longest legal timeout, in
milliseconds — a generous overhead allowance over a value that is
bounded by construction, since the sandbox kills a check at its
timeout), the exit code is None exactly when the check timed out,
and otherwise a real int in
:data:`MIN_EXIT_CODE`..:data:`MAX_EXIT_CODE` (POSIX exit statuses
are 0..255 and signal deaths are negative signal numbers). Every
committed fixture sits far below the duration cap (longest
185,237 ms) and uses exit codes 0/1, with the timeout and
signal shapes pinned by tests the other way, so they all still
verify.

Usage sealing normalisation (v1.35.0): the v1.29.0 ceilings were
mirrored in the structure check and capped on the two first-party
construction paths, but the runner — the one sealing path — still
sealed adapter-supplied metadata *verbatim*: a third-party (or
stub) adapter whose ``name`` was empty or 500,000 characters long,
or which recorded a usage call carrying a 500,000-character model
string, an int ``finish_reason``, or a ``usage`` that was not an
object at all, produced a record that failed its own verification
the moment it was written — or, for the non-object ``usage``,
crashed ``build_record`` with an ``AttributeError`` and lost the
record entirely. The same hole existed one level up in the Token
Factory adapter: a *configured* model id (``--model`` /
``NEBIUS_MODEL``) of any length was recorded whenever the API
response carried no usable model name. The sealing paths now
normalise through :func:`normalise_adapter_name` and
:func:`normalise_usage_calls` here: adapter/lookup names are
truncated to their ceiling (a non-string or empty name falls back
to a caller-supplied placeholder), usage calls are capped in
count and keys, known text fields are truncated to their ceilings
(a non-string ``finish_reason`` becomes ``None``, mirroring the
Token Factory adapter's own normalisation), and a malformed
``usage`` is rebuilt as an object whose token counts are valid
ints or ``None``. The Token Factory adapter additionally rejects
an over-long or empty configured model id at construction
(``ValueError``, which the CLI maps to its exit-2 adapter error).
Every first-party value is far below every cap, so honest records
are sealed byte-identical.

Mode validation (v1.38.0): the run ``mode`` was the mirror image
of all of the above — the structure check has always required the
header mode to be ``judge`` or ``real``, but nothing on the
emitting side enforced it except the CLI's argparse choices. The
runner, the repair loop, replay, and the CLI's adapter factory
(which silently treated any non-``judge`` value as real) now all
validate through :func:`validate_mode` before any work happens,
so an invalid mode — including a shell fragment interpolated into
the replay string — is a usage error, never a sealed record. See
:func:`validate_mode`.

Label validation (v1.40.0): the spec/candidate *labels* were the
last caller-supplied run parameters with no validation at all.
They are interpolated through :func:`quote_label` into the
record's sealed ``replay`` string, and ``quote_label`` assumed a
string: a library caller passing an ``int``, ``None``, or a list
label crashed with an unhandled ``TypeError`` from the regex
engine — in the runner and the repair loop *after* every check
had already run (no record delivered), and in replay mid-replay,
where the per-record path catches only ``OSError`` /
``RuntimeError``. An empty label sealed the quoted empty string
as if it were a path. And a label's length was unbounded: a
500,000-character label sealed a 500,000-character ``replay``
field that verified INTACT, because ``_metadata_ok`` judged the
replay string only against its templates, which cap no length.
Every entry point now validates both labels through
:func:`validate_label` before any workspace is prepared, any
check runs, or any record is replayed — a non-string, empty, or
over-:data:`MAX_LABEL_CHARS` label is a ``ValueError`` (CLI exit
2), never a crash and never a run — :func:`quote_label` applies
the same validation as a backstop, and ``_metadata_ok`` mirrors
the bound at verification time with :data:`MAX_REPLAY_CHARS`, a
ceiling derived from the largest replay string validated labels
can produce (two, or three for a repair attempt, worst-case
``shlex``-quoted labels plus the templates' fixed text), so a
record carrying a longer replay string fails verification
however it was sealed. Every committed replay string is at most
220 characters, far below both ceilings.

Binding-hash validation (v1.43.0): the hashes a caller supplies
to bind a record to its artifacts (``verify``'s ``spec_hash``
and ``candidate_hash``) are validated through
:func:`validate_binding_hash` — see that helper. The sealed
header side has always been held to the SHA-256 hex form; the
supplied side was compared verbatim, so a malformed value was
reported as a binding mismatch instead of the usage error it
is. A well-formed hash that differs remains a genuine
mismatch; ``None`` remains the unjudged no-binding sentinel.

Signing-key validation (v1.45.0): the signing key itself was
resolved with a bare ``os.environ.get(..., DEFAULT_DEV_KEY)``,
which falls back to the labelled dev key only when the variable
is *unset*. A variable set but empty — the classic CI/deployment
mistake — or set to whitespace, a single character, or any other
trivially guessable value therefore became the HMAC key, while
the record's ``signing_key_is_default_dev_key`` disclosure read
``False``: the record claimed a custom key had signed it when
in fact an empty (or near-empty) key anyone could reproduce
had. A 500,000-character value was likewise accepted verbatim.
:func:`signing_key` now validates the resolved value: an unset
variable still yields the labelled dev key (and an explicit
value equal to it still discloses ``True``), but a set value
must be 8..512 characters, not whitespace-only, and free of
control characters — anything else is a ``ValueError`` (the
CLI's exit-2 usage error) at sealing and at verification,
never a silently downgraded signature. The error names only
the rule and the length; it never echoes the key itself.

Replay-command update validation (v1.54.0):
:func:`update_replay_command` is the one sanctioned post-seal
edit — the repair loop rewrites a patched attempt's ``replay``
field (the runner's string points at the ephemeral patched
copy) and the record is re-sealed. It took both arguments on
trust. A ``replay_command`` that was not a string, was empty,
carried a shell fragment, named the wrong mode, or ran to
500,000 characters was written and (under chain v3) dutifully
re-sealed — producing a record that fails its own
verification the moment it is written, because
``_metadata_ok`` holds the field to the two emitted
templates, the header's mode, and
:data:`MAX_REPLAY_CHARS`. And the ``record`` side crashed:
a non-object raised ``TypeError`` on assignment, an empty
``entries`` list an ``IndexError``, a non-list one a
``TypeError``, a string ``chain_version`` a ``TypeError``
from the ``>= 3`` comparison, and a v3-shaped record missing
a sealed-payload field a ``KeyError`` — each *after* the
``replay`` field had already been overwritten, leaving a
half-edited record behind. Both arguments are now validated
before anything is mutated, through
:func:`validate_replay_command` and the record-shape checks
in :func:`update_replay_command` itself: a malformed value
is one ``ValueError`` and the record is left byte-for-byte
unchanged. ``build_record`` stays a permissive assembler,
as in earlier rounds; this is the one *mutation* of a
sealed record, so it is judged at its own entry.

Sealing-argument validation (v1.65.0): the permissive-assembler
note above is superseded for the shapes ``build_record`` itself
computes over. The runner normalises everything it seals
(v1.35.0), but that protects the runner's callers, not this
function's (the v1.51.0 doctrine): a direct caller handing the
assembler a malformed ``llm_usage`` crashed inside its own sum
— a non-object call or a non-object ``usage`` with an
``AttributeError``, a string token count with a ``TypeError`` —
and a negative token count sealed a usage entry whose total
the consistency mirror then rejects, so the record failed its
own verification the moment it was written. A non-object check
entry or diagnosis crashed the same way, on the mapping spread.
``build_record`` now validates exactly the shapes it touches:
``check_results`` a list of objects, ``diagnosis`` an object
or ``None``, and ``llm_usage`` a list of objects whose
``usage`` is an object or ``None`` carrying only non-negative
real-int (never bool) token counts or ``None`` — anything else
is one ``ValueError`` before anything is sealed. Field-*value*
consistency (a verdict against its checks, the caps, the
header's mode and hashes) deliberately stays the verification
mirror's job, not the assembler's: the mirror's tests seal
value-level nonsense through this function to prove the mirror
catches it, and the runner derives those values from validated
inputs long before it seals.

Unencodable sealed text (v1.97.0): the chain hashes UTF-8
text, so a **lone surrogate** anywhere in a record's strings —
reachable through a JSON ``\\ud800`` escape in the record file —
made the hash recomputation itself raise
``UnicodeEncodeError``: ``verify_chain`` crashed on exactly the
forged records it exists to judge, and the CLI's except-backstop
reported a usage error (exit 2) instead of CHAIN INVALID
(exit 1). The same character in a spec name / check name /
check command crashed ``build_record`` after the checks had
run, and in a check *command* additionally made the spawn
itself impossible, as does a NUL byte (which the structure
check now also mirrors). ``verify_chain`` therefore scans the
record — iteratively, keys included — for lone surrogates
before hashing anything and returns ``False`` for one, and
``build_record`` refuses to seal one with a ``ValueError``
before the first hash is computed. Adapter-supplied commentary
is sanitised on the sealing paths instead of rejected (see
``runner.normalise_explanation``, :func:`normalise_usage_calls`,
:func:`normalise_adapter_name`, ``tavily.normalise_references``):
surrogates become U+FFFD, the same replacement check output
already gets from ``errors="replace"`` decoding, because
commentary must never cost a decided verdict its record. No
genuine record is affected: everything VerifyGate seals is
produced from validated specs and decoded output, neither of
which can carry a lone surrogate.
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
from .jsonio import has_lone_surrogates, replace_lone_surrogates
from .sandbox import MAX_OUTPUT_CHARS
from .spec import (
    MAX_CHECK_COMMAND_CHARS,
    MAX_CHECK_NAME_CHARS,
    MAX_CHECKS,
    MAX_SPEC_NAME_CHARS,
    MAX_TIMEOUT_S,
)
from .tavily import (
    MAX_REFERENCES,
    SNIPPET_LIMIT,
    SOURCE_LIMIT,
    TITLE_LIMIT,
    URL_LIMIT,
)

DEFAULT_DEV_KEY = "verifygate-dev-key-NOT-FOR-PRODUCTION"
# Signing-key bounds (v1.45.0, see the module docstring): a custom
# key must be long enough not to be trivially guessable and short
# enough to be a key, not a document. The dev key (37 characters)
# and ordinary passphrases / generated keys sit comfortably inside.
MIN_SIGNING_KEY_CHARS = 8
MAX_SIGNING_KEY_CHARS = 512
GENESIS = "verifygate/genesis/v1"
CHAIN_VERSION = 3
# The diagnosis text sealed beside a FAIL verdict is capped at this
# many characters on the sealing path (runner.normalise_explanation)
# and mirrored here: generous over every real diagnosis (the longest
# committed one is 2,022 characters), finite against a runaway one.
MAX_EXPLANATION_CHARS = 20_000
# A check's sealed stdout/stderr tail: the sandbox's _tail returns at
# most MAX_OUTPUT_CHARS of output, prefixed (only when it truncated)
# by the 14-character marker "…[truncated]…\n". A longer tail in a
# record is therefore not a shape a real run can seal (v1.28.0).
MAX_TAIL_CHARS = MAX_OUTPUT_CHARS + len("…[truncated]…\n")
# Diagnosis/usage metadata ceilings (v1.29.0, see the module
# docstring): generous over every honest value — the longest
# committed adapter/lookup name is 20 characters, the longest model
# id 37, the longest sealed error note 79, and the committed
# maximum is 2 calls per usage entry against a construction-bound
# worst case of 22.
MAX_ADAPTER_NAME_CHARS = 100
MAX_MODEL_CHARS = 200
MAX_PURPOSE_CHARS = 100
MAX_FINISH_REASON_CHARS = 100
MAX_ERROR_CHARS = 500
MAX_CALLS_PER_USAGE_ENTRY = 50
MAX_CALL_KEYS = 20
MAX_CALL_FIELD_CHARS = 500
# Check-result numeric bounds (v1.33.0, see the module docstring):
# a check is killed at its timeout, whose ceiling is MAX_TIMEOUT_S,
# so its measured duration cannot exceed that ceiling plus startup
# and teardown overhead — the cap allows twice the ceiling outright.
# An exit code is a POSIX exit status (0..255) or a negative signal
# number; the range allows both with generous headroom. The longest
# committed duration is 185,237 ms and every committed exit code is
# 0 or 1.
MAX_DURATION_MS = MAX_TIMEOUT_S * 2 * 1000
MIN_EXIT_CODE = -255
MAX_EXIT_CODE = 255
# Label / replay ceilings (v1.40.0, see the module docstring): a
# spec/candidate label is a filesystem path as the caller names it
# — the longest committed one is a few dozen characters, and the
# repair report's candidate directories are already capped at
# 1,000 in verify.py — so 1,000 is a generous ceiling, not a
# target. MAX_REPLAY_CHARS is derived from it: a replay string
# carries at most three labels (record + spec + candidate for a
# repaired attempt), each at most MAX_LABEL_CHARS raw, and a raw
# label of single quotes expands the most under shlex quoting
# (each ' becomes the 5-character '"'"' sequence, plus the two
# surrounding quotes) — 3 * (5 * 1000 + 2) = 15,006, plus the
# templates' fixed text, rounded up to 16,384. A record whose
# replay string is longer than that is not a shape validated
# labels can produce.
MAX_LABEL_CHARS = 1000
MAX_REPLAY_CHARS = 16_384

_USAGE_TEXT_CAPS = {
    "adapter": MAX_ADAPTER_NAME_CHARS,
    "model": MAX_MODEL_CHARS,
    "purpose": MAX_PURPOSE_CHARS,
    "finish_reason": MAX_FINISH_REASON_CHARS,
}
_USAGE_TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


def normalise_adapter_name(value, fallback: str) -> str:
    """Bound an adapter/lookup name to the sealed ceiling (v1.35.0).

    A non-empty string is truncated to ``MAX_ADAPTER_NAME_CHARS``;
    anything else (non-string, empty) becomes ``fallback``, which
    the caller supplies as a valid placeholder. Applied on the
    sealing paths (the runner's diagnosis entry, the repair loop's
    report) so a record VerifyGate emits always satisfies the
    structure check's name rule, whatever an adapter calls itself.
    Lone surrogates become U+FFFD (v1.97.0): a self-reported name
    is commentary, sanitised like every other adapter text, never
    a reason to lose a record.
    """
    if isinstance(value, str) and value:
        return replace_lone_surrogates(value[:MAX_ADAPTER_NAME_CHARS])
    return fallback


def _normalise_usage_value(value):
    """Rebuild one call's ``usage`` as a valid token-count object.

    ``None`` stays ``None`` (a count the API did not report). A
    dict keeps only the three token fields, each a non-negative
    real int or ``None`` (an invalid count becomes ``None`` — not
    reported — rather than a value the sealed record would
    reject). Anything else becomes an empty object.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        return {}
    usage: dict = {}
    for field in _USAGE_TOKEN_FIELDS:
        count = value.get(field)
        if count is not None and (
                isinstance(count, bool) or not isinstance(count, int)
                or count < 0):
            count = None
        usage[field] = count
    return usage


def normalise_usage_calls(raw) -> list[dict]:
    """Bound recorded LLM calls to the sealed usage shape (v1.35.0).

    Mirrors ``_usage_entry_ok`` on the sealing path, the way
    ``tavily.normalise_references`` mirrors the reference caps:
    a non-list input yields no calls; non-object calls are dropped;
    at most ``MAX_CALLS_PER_USAGE_ENTRY`` calls and
    ``MAX_CALL_KEYS`` keys per call are kept; the known text fields
    are truncated to their ceilings (a non-string value drops the
    field, except ``finish_reason``, which becomes ``None`` — the
    neutral value the Token Factory adapter already records for an
    unusable reason); any other string value is truncated to
    ``MAX_CALL_FIELD_CHARS``; and each call's ``usage`` is rebuilt
    by :func:`_normalise_usage_value`. Lone surrogates in keys and
    string values become U+FFFD (v1.97.0) — adapter text is
    commentary and is sanitised, never a reason to lose a record.
    First-party calls are far below every cap and pass through
    unchanged in content.
    """
    if not isinstance(raw, list):
        return []
    calls: list[dict] = []
    for item in raw:
        if len(calls) >= MAX_CALLS_PER_USAGE_ENTRY:
            break
        if not isinstance(item, dict):
            continue
        call: dict = {}
        for field, value in item.items():
            if len(call) >= MAX_CALL_KEYS:
                break
            if isinstance(field, str):
                # Key text is adapter text too (v1.97.0): a lone
                # surrogate in a key would crash sealing exactly
                # like one in a value.
                field = replace_lone_surrogates(field)
            if field == "usage":
                call[field] = _normalise_usage_value(value)
            elif field == "finish_reason":
                # Mirrors the Token Factory adapter's own rule: a
                # string within the cap is kept, anything else —
                # non-string or over-long — becomes None.
                call[field] = replace_lone_surrogates(value) \
                    if isinstance(value, str) \
                    and len(value) <= MAX_FINISH_REASON_CHARS else None
            elif field in _USAGE_TEXT_CAPS:
                if isinstance(value, str):
                    call[field] = replace_lone_surrogates(
                        value[:_USAGE_TEXT_CAPS[field]])
            elif isinstance(value, str):
                call[field] = replace_lone_surrogates(
                    value[:MAX_CALL_FIELD_CHARS])
            elif isinstance(value, (dict, list, tuple, set)):
                continue  # a container has no honest scalar form here
            else:
                call[field] = value
        calls.append(call)
    return calls


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _contains_lone_surrogate(obj) -> bool:
    """True when any string in ``obj`` holds a lone surrogate.

    Iterative deep scan (v1.97.0), dict keys included: the chain
    hashes the canonical encoding of every key and value, so a
    surrogate in any of them is equally unhashable. Records are
    depth-bounded by the loader, and the scan is iterative in
    any case — a forged record cannot take the judge near the
    recursion cliff either.
    """
    stack = [obj]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if has_lone_surrogates(item):
                return True
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return False


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _escape_tree_rel(rel: str) -> str:
    """Escape a relative path for one tree-hash line (v1.71.0).

    Backslash is escaped first, then newline and carriage return, so
    the escaped form never contains a literal line break and a literal
    backslash sequence in a filename can never be confused with an
    escaped control character. A path carrying none of those three
    characters escapes to itself, byte for byte — which is what keeps
    every tree hash computed before v1.71.0 unchanged for ordinary
    filenames.
    """
    return rel.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")


def tree_hash(root: str | Path) -> str:
    """Content hash of a directory tree: sorted (relpath, file-hash) lines.

    Uses the sandbox's containment traversal: symlinks pointing outside
    the candidate (and directory symlinks) are excluded, so the hash
    describes exactly the tree that was copied into the workspace and
    verified — never a host file reached through a link.

    Each line is ``<relpath>:<file-hash>`` and the lines are joined
    with newlines before hashing. Until v1.71.0 the relpath was
    interpolated raw, so a filename carrying a newline (and a colon
    plus a plausible-looking hash) could forge an extra line: two
    different trees — one file named ``a:<hash-of-X>\\nc`` holding Y,
    versus the two files ``a`` holding X and ``c`` holding Y — hashed
    identically, and a candidate binding (``verify --candidate``,
    replay's tree comparison, a sealed header) could not tell them
    apart. Relpaths are now escaped (see ``_escape_tree_rel``) before
    they enter a line, which makes the encoding injective: each line
    decodes uniquely (the file hash is the fixed-width suffix) and no
    filename can manufacture an extra line. Filenames without a
    backslash, newline, or carriage return hash exactly as before, so
    every committed record's candidate binding is unchanged.

    The sandbox's candidate size bounds are enforced first (v1.23.0):
    binding a record to an oversized candidate directory (``verify
    --candidate``, replay) is a usage error, not an invitation to
    read an unbounded tree into the hash.

    The directory itself is validated on the same path (v1.41.0,
    through ``check_candidate_bounds``' own validation): a non-path
    value is a ``ValueError`` rather than a ``TypeError`` from
    ``Path(...)``, an empty string does not silently hash the
    current directory, and a regular file is the run path's
    ``FileNotFoundError``, not a ``NotADirectoryError``.
    """
    from .sandbox import check_candidate_bounds, iter_candidate_files

    check_candidate_bounds(root)

    lines = [
        f"{_escape_tree_rel(rel)}:{_hash_file(source)}"
        for rel, source in iter_candidate_files(root)
    ]
    return _sha("\n".join(lines))


def _hash_file(source: Path) -> str:
    """SHA-256 of one file's bytes, streamed in chunks (v1.79.0).

    Before this, :func:`tree_hash` hashed ``source.read_bytes()``:
    the whole file materialised in memory first. The candidate
    bounds (v1.23.0) cap a candidate at 256 MiB in total, so a
    single verified file could legitimately be ~256 MiB — and its
    hash, a value that never needs more than one chunk of the
    file at a time, allocated all of it. The digest is fed chunk
    by chunk instead, so the hash of a tree costs the same memory
    however large its largest file is; the resulting line (and
    therefore every tree hash and candidate binding) is identical
    to the whole-file computation.
    """
    digest = hashlib.sha256()
    with open(source, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def signing_key() -> str:
    """Resolve and validate the HMAC signing key (v1.45.0).

    Unset ``VERIFYGATE_SIGNING_KEY`` yields the labelled public
    dev key. A *set* value is validated before it is used, at
    sealing and at verification alike: it must be a string of
    ``MIN_SIGNING_KEY_CHARS..MAX_SIGNING_KEY_CHARS`` characters,
    not whitespace-only, contain no control characters, and be
    encodable as UTF-8 (v1.98.0: a value carrying lone
    surrogates — which is how ``os.environ`` decodes non-UTF-8
    bytes set by another process — passed every check above and
    then crashed the HMAC ``.encode()`` at sealing and at
    verification alike, instead of the one ``ValueError``).
    Before this, a set-but-empty variable (the ``os.environ.get``
    default applies only when the variable is absent) silently
    became an empty HMAC key whose records nonetheless disclosed
    ``signing_key_is_default_dev_key: False`` — a custom-key
    claim backed by a key anyone could reproduce. A malformed
    value is a ``ValueError`` naming only the rule and the
    length; the key itself is never echoed into an error, a log,
    or a record.
    """
    raw = os.environ.get("VERIFYGATE_SIGNING_KEY")
    if raw is None:
        return DEFAULT_DEV_KEY
    if not isinstance(raw, str) \
            or not MIN_SIGNING_KEY_CHARS <= len(raw) <= MAX_SIGNING_KEY_CHARS \
            or not raw.strip() \
            or has_lone_surrogates(raw) \
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise ValueError(
            "VERIFYGATE_SIGNING_KEY must be "
            f"{MIN_SIGNING_KEY_CHARS}..{MAX_SIGNING_KEY_CHARS} characters, "
            "not whitespace-only, free of control characters, and "
            "encodable as UTF-8 "
            f"(got length {len(raw) if isinstance(raw, str) else 'n/a'})")
    return raw


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

    The shapes this function itself computes over are validated
    before anything is sealed (v1.65.0, see the module docstring):
    ``check_results`` must be a list of objects, ``diagnosis`` an
    object or ``None``, and ``llm_usage`` a list of objects whose
    ``usage`` is an object or ``None`` whose token counts are
    non-negative real ints (never bool) or ``None``. Anything else
    is a ``ValueError``, never an ``AttributeError`` / ``TypeError``
    from the mapping spread or the usage sum, and never a sealed
    usage total the consistency mirror would reject.
    """
    if not isinstance(check_results, (list, tuple)) or any(
            not isinstance(result, dict) for result in check_results):
        raise ValueError(
            "check_results must be a list of check-result objects")
    if diagnosis is not None and not isinstance(diagnosis, dict):
        raise ValueError("diagnosis must be an object or None")
    if llm_usage is not None:
        if not isinstance(llm_usage, (list, tuple)) or any(
                not isinstance(call, dict) for call in llm_usage):
            raise ValueError("llm_usage must be a list of call objects")
        for call in llm_usage:
            usage = call.get("usage")
            if usage is None:
                continue
            if not isinstance(usage, dict):
                raise ValueError(
                    "each llm_usage call's usage must be an object or None")
            for field in _USAGE_TOKEN_FIELDS:
                count = usage.get(field)
                if count is not None and (
                        isinstance(count, bool)
                        or not isinstance(count, int) or count < 0):
                    raise ValueError(
                        f"llm_usage token count {field!r} must be a "
                        "non-negative int or None")
    if _contains_lone_surrogate((
            spec_name, spec_hash, candidate_tree_hash, mode, verdict,
            replay_command, check_results, diagnosis, llm_usage)):
        # Unencodable text (v1.97.0): the chain hashes UTF-8, so
        # a lone surrogate in anything about to be sealed would
        # crash the first _sha below with a UnicodeEncodeError —
        # one ValueError here, before anything is sealed, instead.
        raise ValueError(
            "record content contains lone surrogates — characters "
            "that are not valid Unicode and cannot be encoded as "
            "UTF-8 — so it can never be sealed into an evidence "
            "record")
    # Tuples are accepted above but sealed as lists: the entries
    # carry the objects themselves, and the structure mirror
    # requires a list (notably the usage entry's ``calls``), so
    # sealing the tuple itself would fail its own verification.
    check_results = list(check_results)
    if llm_usage is not None:
        llm_usage = list(llm_usage)
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


def validate_replay_command(replay_command, header_mode=None) -> str:
    """Return ``replay_command`` if it is an emittable replay string.

    Replay-command validation (v1.54.0, see the module
    docstring): a replay string is one of the two commands the
    runner / repair loop emit — the exact templates
    ``_metadata_ok`` enforces at verification time — at most
    :data:`MAX_REPLAY_CHARS` characters, in the header's mode
    when ``header_mode`` is given. A non-string, an empty or
    over-long string, a string matching neither template (a
    shell fragment, a trailing newline, a hand-written
    command), or a template whose mode disagrees with the
    header is a ``ValueError``; anything else would seal a
    record that fails its own verification. The match is a
    ``fullmatch``: the templates' own ``$`` anchor would
    otherwise let a trailing newline slip through.
    """
    if not isinstance(replay_command, str) or not replay_command:
        raise ValueError(
            "replay_command must be a non-empty string "
            f"(got {type(replay_command).__name__})")
    if len(replay_command) > MAX_REPLAY_CHARS:
        raise ValueError(
            f"replay_command must be at most {MAX_REPLAY_CHARS} "
            f"characters (got {len(replay_command)})")
    for pattern in _REPLAY_RES:
        match = pattern.fullmatch(replay_command)
        if match is not None:
            if header_mode is not None and match.group(1) != header_mode:
                raise ValueError(
                    f"replay_command mode {match.group(1)!r} does not "
                    f"match the record header mode {header_mode!r}")
            return replay_command
    raise ValueError(
        "replay_command must be a VerifyGate run or replay command "
        "in one of the two emitted templates (see _REPLAY_RES)")


def update_replay_command(record: dict, replay_command: str) -> None:
    """Replace a record's ``replay`` field, re-sealing if needed.

    The repair loop rewrites a patched attempt's replay field after
    the runner seals its record (the runner's string points at the
    ephemeral patched copy). Under chain v3 the field is part of the
    sealed payload, so the record hash and signature are recomputed
    here — the only sanctioned post-seal edit. Under v1/v2 the field
    was unsealed metadata and is simply replaced.

    Both arguments are validated before anything is mutated
    (v1.54.0, see the module docstring): the command through
    :func:`validate_replay_command` against the record header's
    mode, and the record for the shape the re-seal reads — an
    object whose ``chain_version`` (when present) is a real
    ``int`` in (1, 2, 3), whose ``entries`` is a non-empty list
    of objects headed by a header carrying a real mode, and,
    for chain v3, whose sealed-payload fields are present and
    whose last entry carries its ``hash`` string. A malformed
    record or command is one ``ValueError`` and the record is
    left byte-for-byte unchanged — never a ``TypeError`` /
    ``KeyError`` / ``IndexError`` after the field was already
    overwritten, and never a re-sealed record that fails its
    own verification.
    """
    if not isinstance(record, dict):
        raise ValueError(
            f"record must be an evidence record object "
            f"(got {type(record).__name__})")
    version = record.get("chain_version", 1)
    if isinstance(version, bool) or not isinstance(version, int) \
            or version not in (1, 2, 3):
        raise ValueError(
            f"record chain_version must be 1, 2, or 3 "
            f"(got {type(version).__name__})")
    entries = record.get("entries")
    if not isinstance(entries, list) or not entries \
            or not isinstance(entries[0], dict) \
            or not isinstance(entries[-1], dict):
        raise ValueError(
            "record entries must be a non-empty list of entry "
            "objects (a header first, sealed entries after)")
    header_mode = entries[0].get("mode")
    if header_mode not in MODES:
        raise ValueError(
            "record header must carry a mode of 'judge' or 'real'")
    validate_replay_command(replay_command, header_mode)
    if version >= 3:
        missing = [field for field in (
            "verdict", "version", "created_at",
            "signing_key_is_default_dev_key") if field not in record]
        if missing:
            raise ValueError(
                "chain v3 record is missing sealed-payload "
                f"field(s): {', '.join(missing)}")
        if not isinstance(entries[-1].get("hash"), str) \
                or not entries[-1]["hash"]:
            raise ValueError(
                "record's last entry must carry its hash string "
                "for the re-seal")
    record["replay"] = replay_command
    if version >= 3:
        prev = record["entries"][-1]["hash"]
        record["record_hash"] = _sha(
            prev + _canonical(_sealed_payload(record, version)))
        record["signature"] = hmac.new(
            signing_key().encode("utf-8"),
            record["record_hash"].encode("utf-8"),
            hashlib.sha256).hexdigest()


MODES = ("judge", "real")
_MODES = MODES
_USAGE_TOKEN_KEYS = {"prompt_tokens", "completion_tokens", "total_tokens"}


def validate_mode(mode) -> str:
    """Return ``mode`` if it is a run mode, else raise ``ValueError``.

    Mode validation (v1.38.0): the run mode was the one run parameter
    validated only by the CLI's argparse ``choices`` — every library
    entry point (the runner, the repair loop, replay, and the CLI's
    own adapter factory, which treated any non-``judge`` value as
    real mode) accepted any value verbatim. An invalid mode was
    sealed into the record header and interpolated raw into the
    record's ``replay`` string, so a run with mode ``bogus`` — or
    ``judge; echo pwned``, a shell injection into the command a
    reader is told to copy and run — produced a PASS record that
    failed its own verification the moment it was written (the
    structure check requires the header mode to be a real mode and
    the replay string to match a template ending in a real mode),
    after every check had already run. The entry points now call
    this helper before any check runs or any adapter is chosen, so
    an invalid mode is a usage error (``ValueError``, CLI exit 2),
    never a run. ``build_record`` itself stays a permissive
    assembler, as in earlier rounds: the structure mirror in
    :func:`verify_chain` is what judges a record sealed over an
    invalid mode, however it was produced.
    """
    if mode not in MODES:
        desc = repr(mode)
        if len(desc) > 80:
            desc = desc[:77] + "..."
        raise ValueError(f"mode must be 'judge' or 'real', got {desc}")
    return mode


def validate_label(label, name: str = "label") -> str:
    """Return ``label`` if it is a usable replay label, else raise.

    Label validation (v1.40.0): the spec/candidate labels are
    interpolated into the sealed ``replay`` string through
    :func:`quote_label`, which assumed a string — a non-string
    label raised an unhandled ``TypeError`` from the regex engine
    after the checks had already run, an empty label sealed the
    quoted empty string as if it were a path, and an over-long
    label sealed a replay field of any length, which the metadata
    templates then accepted at verification time. A label must now
    be a non-empty string of at most :data:`MAX_LABEL_CHARS`
    characters; anything else is a ``ValueError`` (CLI exit 2)
    before any work happens, with the offending value's repr
    truncated exactly as :func:`validate_mode` truncates it, so a
    huge label cannot bloat the error line either. Control
    characters are *not* rejected here: :func:`quote_label`
    already degrades such a label to the ``<unprintable-label>``
    placeholder, the v1.21.0 contract.

    Encodability (v1.99.0): the label was judged only for type and
    length, but a label is *sealed* — it is interpolated into the
    record's ``replay`` string, which the chain hashes as UTF-8 —
    so a label carrying lone surrogates could never be sealed at
    all. Such a label is exactly how a non-UTF-8 path arrives on
    POSIX: ``sys.argv`` (the CLI passes ``args.spec`` /
    ``args.candidate`` verbatim as the labels) and ``os.environ``
    decode bytes with surrogateescape, so a path whose filename
    holds non-UTF-8 bytes is a string of legal length carrying
    unpaired surrogates. It passed this validation, was
    shlex-quoted by :func:`quote_label`, and the entry point ran
    every check — then :func:`build_record` refused to seal, the
    decided verdict's record was lost, and the error blamed the
    "record content", not the label. In replay the same
    ``ValueError`` escaped the per-record path (which catches only
    ``OSError`` / ``RuntimeError``) mid-replay. A surrogate-bearing
    label is now the one ``ValueError`` here, at entry, before any
    work — the message names the rule and never echoes the label.
    Control characters are still *not* rejected (the placeholder
    contract above); lone surrogates are not control characters.
    """
    if not isinstance(label, str) or not label \
            or len(label) > MAX_LABEL_CHARS:
        desc = repr(label)
        if len(desc) > 80:
            desc = desc[:77] + "..."
        raise ValueError(
            f"{name} must be a non-empty string of at most "
            f"{MAX_LABEL_CHARS} characters, got {desc}")
    if has_lone_surrogates(label):
        raise ValueError(
            f"{name} must be encodable as UTF-8 (it is sealed "
            "into the record's replay string), got a label "
            "containing lone surrogates — characters that are "
            "not valid Unicode and cannot be encoded as UTF-8")
    return label


def _is_int(value) -> bool:
    """A real JSON integer — ``bool`` is an ``int`` subclass, so a
    sealed ``true`` must not pass as the number 1 (cf. spec timeouts)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_sha256_hex(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        c in "0123456789abcdef" for c in value)


def validate_binding_hash(value, name: str = "spec_hash"):
    """Return a binding hash if it is well-formed, else raise.

    Binding-hash validation (v1.43.0): ``verify`` binds a record
    to the artifacts on disk by comparing hashes the caller
    supplies — the spec hash and the candidate tree hash —
    against the record's sealed header. Those header fields are
    held to the SHA-256 hex form (64 lowercase hex characters,
    the form ``hashlib`` hexdigests and this module's header
    check require), but the *supplied* side of the comparison
    was taken on trust: a library caller passing an ``int``, a
    ``bool``, an empty string, an upper-case or truncated hex
    string, or a 500,000-character string got a ``spec_match``
    / ``candidate_match`` of False — reported as a binding
    *mismatch*, as if a genuine hash had been compared and the
    record had failed it — instead of the usage error the
    malformed value actually is. (The candidate-*directory*
    side of the same binding was given exactly this fail-fast
    treatment in v1.41.0; the hash side was left.) A well-formed
    hash that simply differs is a genuine mismatch and still
    returns False, unchanged. ``None`` is the caller's "no
    binding requested" sentinel and passes through unjudged,
    exactly as ``verify``'s candidate directory sentinel does.
    Anything else malformed is a ``ValueError`` (CLI exit 2)
    before any record is checked, with the offending value's
    repr truncated exactly as :func:`validate_mode` truncates
    it, so a huge hash cannot bloat the error line either.
    """
    if value is None:
        return None
    if not _is_sha256_hex(value):
        desc = repr(value)
        if len(desc) > 80:
            desc = desc[:77] + "..."
        raise ValueError(
            f"{name} must be a SHA-256 hex digest (64 lowercase "
            f"hex characters) or None, got {desc}")
    return value


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
    # The spec parser's text ceilings (v1.28.0), mirrored: a check
    # name or command longer than any valid spec can carry is not a
    # shape build_record can emit from a real run.
    if len(entry["name"]) > MAX_CHECK_NAME_CHARS \
            or len(entry["command"]) > MAX_CHECK_COMMAND_CHARS:
        return False
    # The spawnability rule (v1.97.0), mirrored: a command carrying
    # a NUL byte can never have been spawned (see
    # spec.check_command_problem), so a record claiming it ran is
    # not a shape a real run emits. (Lone surrogates need no mirror
    # here: verify_chain's deep scan rejects them before the
    # structure check is reached.)
    if "\x00" in entry["command"]:
        return False
    if not _is_int(entry["duration_ms"]) or entry["duration_ms"] < 0 \
            or entry["duration_ms"] > MAX_DURATION_MS:
        return False
    # The sandbox's tail cap, mirrored the same way: a sealed tail
    # longer than MAX_OUTPUT_CHARS plus the truncation marker is not
    # a shape a real run emits.
    if not isinstance(entry["stdout_tail"], str) \
            or not isinstance(entry["stderr_tail"], str) \
            or len(entry["stdout_tail"]) > MAX_TAIL_CHARS \
            or len(entry["stderr_tail"]) > MAX_TAIL_CHARS:
        return False
    if not isinstance(entry["timed_out"], bool):
        return False
    exit_code = entry["exit_code"]
    # The sandbox's exit-code contract (v1.33.0): a check that timed
    # out was killed before it could exit, so its exit code is None;
    # a check that did not time out always exited, with a POSIX exit
    # status (0..255) or a negative signal number. The mirror image
    # shapes — a timeout carrying an exit code, a completed check
    # carrying none, an exit code outside that range — are not
    # shapes run_command can seal.
    if entry["timed_out"]:
        if exit_code is not None:
            return False
    elif not _is_int(exit_code) \
            or not MIN_EXIT_CODE <= exit_code <= MAX_EXIT_CODE:
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
    all strings. The explanation itself is bounded by
    ``MAX_EXPLANATION_CHARS`` — the runner truncates on sealing
    (v1.27.0), so a longer text is not a shape a real run emits.
    The adapter/lookup names and the two error notes are bounded
    too (v1.29.0): names are code-defined identifiers, and the
    runner's error notes are formatted from exception text it
    truncates (a lookup's own note is capped on the sealing path),
    so an over-long name or note is not a real-run shape either.
    """
    keys = set(entry)
    base = {"kind", "hash", "explanation", "references", "llm_adapter",
            "reference_source"}
    if not base <= keys <= base | {"reference_error", "diagnosis_error"}:
        return False
    # The sealing bound the runner enforces (v1.27.0), mirrored: the
    # explanation is truncated to MAX_EXPLANATION_CHARS on the one
    # sealing path, so a longer text is not a shape a real run seals.
    if not isinstance(entry["explanation"], str) \
            or len(entry["explanation"]) > MAX_EXPLANATION_CHARS:
        return False
    # The adapter / lookup names are code-defined identifiers and
    # the error notes are formatted by the runner from exception
    # text it truncates to 200 characters (a lookup's own note is
    # capped on the sealing path too) — the v1.29.0 ceilings mirror
    # those construction bounds, so a longer name or note is not a
    # shape a real run seals.
    if not isinstance(entry["llm_adapter"], str) \
            or not 0 < len(entry["llm_adapter"]) <= MAX_ADAPTER_NAME_CHARS \
            or not isinstance(entry["reference_source"], str) \
            or not 0 < len(entry["reference_source"]) \
            <= MAX_ADAPTER_NAME_CHARS:
        return False
    if "reference_error" in entry and not (
            entry["reference_error"] is None
            or (isinstance(entry["reference_error"], str)
                and len(entry["reference_error"]) <= MAX_ERROR_CHARS)):
        return False
    if "diagnosis_error" in entry and not (
            entry["diagnosis_error"] is None
            or (isinstance(entry["diagnosis_error"], str)
                and len(entry["diagnosis_error"]) <= MAX_ERROR_CHARS)):
        return False
    references = entry["references"]
    if not isinstance(references, list):
        return False
    # The sealing bounds the runner enforces (v1.26.0), mirrored:
    # the runner asks for at most MAX_REFERENCES references and
    # normalises every field to the adapters' caps, so a diagnosis
    # carrying more references — or a longer field — than any real
    # run can seal is not a shape build_record's callers produce.
    if len(references) > MAX_REFERENCES:
        return False
    limits = {"title": TITLE_LIMIT, "url": URL_LIMIT,
              "snippet": SNIPPET_LIMIT, "source": SOURCE_LIMIT}
    return all(
        isinstance(ref, dict)
        and set(ref) == {"title", "url", "snippet", "source"}
        and all(isinstance(ref[k], str) and len(ref[k]) <= limits[k]
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

    Since v1.29.0 the calls are also *bounded*: at most
    ``MAX_CALLS_PER_USAGE_ENTRY`` of them (one record seals at most
    the repair loop's round budget worth of patch calls, each at
    most doubled by the real adapter's empty-completion retry, plus
    one explanation), each carrying at most ``MAX_CALL_KEYS`` keys,
    the known text fields (``adapter`` / ``model`` / ``purpose`` /
    ``finish_reason``) strings within their ceilings when present,
    and every other string value within ``MAX_CALL_FIELD_CHARS`` —
    so an entry padded with calls or with enormous strings is not a
    shape a real run seals either.
    """
    if set(entry) != {"kind", "hash", "calls", "total_tokens"}:
        return False
    calls = entry["calls"]
    if not isinstance(calls, list) or not calls \
            or len(calls) > MAX_CALLS_PER_USAGE_ENTRY:
        return False
    if not _is_int(entry["total_tokens"]) or entry["total_tokens"] < 0:
        return False
    text_caps = {
        "adapter": MAX_ADAPTER_NAME_CHARS,
        "model": MAX_MODEL_CHARS,
        "purpose": MAX_PURPOSE_CHARS,
        "finish_reason": MAX_FINISH_REASON_CHARS,
    }
    total = 0
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

    The label is validated first (:func:`validate_label`, v1.40.0)
    as a backstop: the entry points validate before any work, so
    this guard only fires for a direct caller — as a clean
    ``ValueError``, not the regex engine's ``TypeError``.
    """
    validate_label(label)
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
    # The replay length bound (v1.40.0), mirrored: the templates
    # below cap no length, so before this a record sealed over a
    # 500,000-character label carried a replay field of that length
    # and verified. Validated labels cannot produce a replay string
    # longer than MAX_REPLAY_CHARS (see its derivation).
    if len(replay) > MAX_REPLAY_CHARS:
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
    # The spec parser's name ceiling (v1.28.0), mirrored: a header
    # naming a longer spec than any valid spec can carry is not a
    # shape build_record can emit from a real run.
    if len(header["spec_name"]) > MAX_SPEC_NAME_CHARS:
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
    # The spec parser caps a spec at MAX_CHECKS checks, so a record
    # carrying more check entries than that is not a shape a real
    # run can seal — whatever its chain recomputes to (v1.24.0).
    if len(check_verdicts) > MAX_CHECKS:
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
        # Unencodable text first (v1.97.0): a lone surrogate in
        # any sealed string would make the hash recomputation
        # below raise UnicodeEncodeError instead of judging the
        # record — and a record carrying one is not a shape
        # build_record can emit in any case.
        if _contains_lone_surrogate(record):
            return False
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
