"""Nebius Token Factory adapter (real mode).

Talks to the Token Factory inference API (OpenAI-compatible) and runs an
NVIDIA Nemotron model. Authentication uses the stored connector
credential ``custom.nebius-token-factory``: the request carries an
``hsurr:*`` surrogate obtained via ``dynamic_credentials`` and exchanged
for the real key on egress to ``api.tokenfactory.nebius.com`` only. No
raw key is ever read, printed, or hard-coded here, and no
``NEBIUS_API_KEY`` environment variable is involved.

The transport is a single method (``_post_chat``) so tests can inject a
fake and exercise the whole adapter — prompts, usage accounting, diff
instructions — with zero network. Every completed call is recorded via
``record_call``; the runner seals those entries into the evidence
record's hash chain.

Response-shape hardening (v1.18.0): the decoded response is validated
field by field before anything is recorded. Before this, several
plausible malformed shapes — ``message`` as a bare string or ``None``,
``usage`` as a string or list, a non-string ``content`` — escaped the
shape guard as unhandled ``AttributeError`` tracebacks, crashing
``verifygate run`` / ``repair`` in real mode instead of failing as the
clean adapter error (CLI exit 2) the exit-code contract promises.
Worse, a malformed ``usage`` that *was* a dict (a string, negative, or
float token count) would have been recorded verbatim and sealed into
a record whose own consistency rules (token counts are non-negative
ints or ``None``) then fail that honestly-emitted record's chain.
Every malformed shape now raises one ``RuntimeError``; token counts
must be non-negative ints or absent, and a non-string ``model`` falls
back to the configured model instead of being recorded.

Model-name bound (v1.29.0): the same fallback now also covers an
over-long ``model`` string — the evidence structure check caps a
sealed call's model at ``MAX_MODEL_CHARS``, so a response naming a
500,000-character "model" would otherwise be recorded verbatim and
seal a record that fails its own verification.

Finish-reason normalisation (v1.34.0): one recorded field still
escaped both hardenings above — ``finish_reason`` was copied from
the decoded response into the usage log verbatim, with no type or
length check at all. The evidence structure check caps a sealed
call's ``finish_reason`` at ``MAX_FINISH_REASON_CHARS`` and
requires it to be a string or ``None``, so an API response carrying
``finish_reason: 123``, an object, a list, ``true``, or a
500,000-character string produced a usable completion whose call
was then sealed into a record that fails its own verification —
the exact failure the v1.18.0 / v1.29.0 work closed for the usage
counts and the model name. ``_response_parts`` now normalises it
the same way the model name is handled: a string within the cap is
kept verbatim (``"stop"``, ``"length"``, …), anything else is
recorded as ``None`` — the neutral value the structure check
already accepts for an unstated finish reason — instead of being
allowed to poison the sealed record. The completion itself, the
part that matters, is unaffected either way.

Configured-model validation (v1.35.0): the fallback above covers
model names the *API* returns, but the *configured* model id —
``--model`` / ``NEBIUS_MODEL`` / the constructor argument — was
accepted at any length and recorded verbatim whenever the API
returned no usable model name, so a 500,000-character configured
id sealed the same self-failing record through the fallback path.
The constructor now rejects a configured id that is not a
non-empty string within ``MAX_MODEL_CHARS`` (``ValueError``, which
the CLI already maps to its exit-2 adapter error): no real model
id comes close to the cap (the longest live one is 37 characters).

Constructor validation (v1.44.0): the constructor's other two
arguments were still taken on trust. ``timeout_s`` accepted
anything — a string, ``True`` (silently one second, ``bool``
being an ``int`` subclass), zero or a negative (socket semantics
nobody asked for), ``None`` (block on the global default,
potentially forever), ``inf`` / ``nan``, or 1,000,000,000
seconds (effectively no timeout at all, on a call the caller is
paying for) — and the malformed shapes surfaced only at call
time, inside ``urlopen``, as a confusing ``RuntimeError`` about
a failed call. ``transport`` was selected with ``or``, so a
non-callable truthy value (a string, a number) constructed an
adapter whose first call raised a bare ``TypeError`` — not the
one-``RuntimeError`` adapter-error contract (v1.18.0) — and a
falsy non-``None`` value (``0``, ``""``) was silently *replaced*
by the real transport, discarding the caller's injection and
making real, paid calls in its place. Both are now validated
at construction, beside the model id: ``timeout_s`` must be a
finite number (``int`` / ``float``, never ``bool``) with
``0 < timeout_s <= MAX_TIMEOUT_S`` (3600, mirroring the spec's
check-timeout ceiling), and ``transport`` must be ``None``
(the real transport) or callable. The Tavily adapter's
constructor applies the same two rules (see ``tavily.py``).

Configured-model resolution (v1.49.0): one falsy swap survived
the v1.44.0 constructor pass — the model id itself, resolved
with ``model or os.environ.get(...)``. A supplied-but-falsy id
(``""``, ``0``, ``False``) was silently replaced by the
``NEBIUS_MODEL`` / default value and the adapter then made
paid calls under a model the caller never named, while the
same ``""`` arriving *via* ``NEBIUS_MODEL`` was rejected by
the v1.35.0 validation — the identical value judged two ways
depending on which side of the ``or`` it arrived from.
Resolution is now by identity with ``None`` (the Tavily
api_key pattern): only ``None`` means "not supplied"; every
supplied value, falsy or not, faces the non-empty-string
validation below.

Bounded response read (v1.81.0): the real transport delegated
reading its response to the connector helper's
``read_json_response``, whose body reader chunks the response
(64 KiB at a time) and joins *all* of it in memory with no size
ceiling — the whole body materialised before parsing, however
large the endpoint (or anything able to answer for it) chose to
make it. The v1.80.0 Tavily fix called its own bare ``resp.read()``
"the last unbounded reader in the project"; this one escaped
that audit because the unbounded read hides behind the helper
instead of appearing in this file. ``_post_chat`` now applies
the credential surrogate itself and reads at most
:data:`MAX_RESPONSE_BYTES` + 1 bytes directly, raising past the
ceiling, which the transport's existing wrapper turns into the
one ``RuntimeError`` adapter error (CLI exit 2) — never a
silently truncated completion, since a body cut at the ceiling
is rejected whole, not parsed. The ceiling is generous over a
genuine reply (a completion is capped by ``max_tokens``; even
the 8,192-token patch call with reasoning is tens of kilobytes).

Prompt-embedding bounds (v1.67.0): two parts of the patch prompt
were embedded verbatim, at any size, from ``failing_evidence`` —
the ``candidate_files`` snapshot and the ``patch_error``
feedback. The repair loop caps both at the source (its snapshot
skips files over 4,000 characters and stops at 12,000 total; a
stored patch error is truncated to 2,000), but the adapter is
the method the loop's contract is documented on, and a direct
caller bypassed every cap: one probe call carrying a
500,000-character file and a 500,000-character patch error
produced a 1,001,449-character prompt — a paid call whose cost
the caller never chose. The embedding now mirrors the loop's
caps at the method: only (string path, string content) file
entries are embedded, a file over ``MAX_EMBED_FILE_CHARS`` is
skipped whole (never truncated — a truncated file would invite
a diff whose context lines do not exist, the snapshot's own
reasoning), embedding stops at ``MAX_EMBED_TOTAL_CHARS``
total, and the patch error is truncated to
``MAX_EMBED_PATCH_ERROR_CHARS``. Prompts the repair loop
produces are byte-identical: its snapshot and feedback already
satisfy every cap.

Nothing in here can affect a verdict; it only produces advisory text.
"""

from __future__ import annotations

import json
import math
import os
import sys
import urllib.request

from .evidence import MAX_FINISH_REASON_CHARS, MAX_MODEL_CHARS
from .llm import (
    LLMAdapter,
    validate_brief,
    validate_check_results,
    validate_failing_evidence,
)

BASE_URL = "https://api.tokenfactory.nebius.com/v1/chat/completions"
ALLOWED_HOSTS = ["api.tokenfactory.nebius.com"]
CREDENTIAL_NAME = "custom.nebius-token-factory"
# Pinned 2026-10-10 from the live account model list (25 models). The
# Nano is the fast/cheap reasoning model used for development and the
# default for the repair loop; override with NEBIUS_MODEL or --model.
DEFAULT_MODEL = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
_DYNAMIC_CREDENTIALS_DIR = "/opt/hatch/skills/skill-creator/bin"

# Nemotron 3 Nano is a reasoning model: a tight max_tokens budget can be
# consumed entirely by reasoning before any answer text appears, so the
# patch call gets a generous ceiling. The ceiling is not a target —
# billing follows actual usage, which is recorded per call.
PATCH_MAX_TOKENS = 8192
EXPLAIN_MAX_TOKENS = 4096

# Ceiling for the constructor's ``timeout_s`` (v1.44.0): one hour,
# mirroring the spec's ceiling for a single check (``spec.MAX_TIMEOUT_S``).
# A network call that has not answered in an hour is not going to;
# an unbounded timeout on a paid call is a hang, not patience.
MAX_TIMEOUT_S = 3600

# Prompt-embedding bounds (v1.67.0, see the module docstring):
# the per-file and total caps mirror the repair loop's candidate
# snapshot (``repair._MAX_FILE_CHARS`` / ``repair._MAX_TOTAL_CHARS``)
# and the patch-error cap mirrors its stored-error truncation
# (``repair.MAX_PATCH_ERROR_CHARS``), so a prompt the loop's own
# evidence produces is embedded exactly as before, and only a
# direct caller's oversized evidence is bounded.
MAX_EMBED_FILE_CHARS = 4000
MAX_EMBED_TOTAL_CHARS = 12000
MAX_EMBED_PATCH_ERROR_CHARS = 2000

# Ceiling for the real transport's response body (v1.81.0, see the
# module docstring): the same 4 MiB the Tavily transport enforces,
# generous over any genuine completion this adapter can request.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def validate_timeout_s(timeout_s, name: str = "timeout_s"):
    """Validate an adapter constructor's ``timeout_s`` (v1.44.0).

    A timeout is a plain duration: a finite ``int`` / ``float``
    (never ``bool`` — ``True`` is an ``int`` subclass and used to
    slip through as one second) with ``0 < timeout_s <=
    MAX_TIMEOUT_S``. Anything else — a string, ``None``, zero or
    a negative, ``inf`` / ``nan``, an over-ceiling value, a
    container — is a ``ValueError`` at construction, not a
    socket error or an unbounded wait at call time. The Tavily
    adapter applies the identical rule (``tavily.validate_timeout_s``
    mirrors this helper).
    """
    if isinstance(timeout_s, bool) \
            or not isinstance(timeout_s, (int, float)) \
            or not math.isfinite(timeout_s) \
            or not 0 < timeout_s <= MAX_TIMEOUT_S:
        raise ValueError(
            f"{name} must be a finite number of seconds with "
            f"0 < {name} <= {MAX_TIMEOUT_S} "
            f"(got {type(timeout_s).__name__})")
    return timeout_s


def _load_credential_helpers():
    """Import the dynamic-credential helper, or explain what's missing.

    Only the surrogate helper is imported (v1.81.0): the body is
    read bounded in ``_post_chat`` itself, so the helper's
    unbounded ``read_json_response`` is no longer used here.
    """
    if _DYNAMIC_CREDENTIALS_DIR not in sys.path:
        sys.path.insert(0, _DYNAMIC_CREDENTIALS_DIR)
    try:
        from dynamic_credentials import add_surrogate_to_request
    except ImportError as exc:
        raise ValueError(
            "Nebius connector helpers are unavailable on this machine; "
            "use --mode judge for a zero-key run"
        ) from exc
    return add_surrogate_to_request


class TokenFactoryAdapter(LLMAdapter):
    name = "nebius-token-factory"

    def __init__(
        self,
        model: str | None = None,
        timeout_s: int = 180,
        transport=None,
    ):
        super().__init__()
        # Resolution is by identity with None, never truthiness
        # (v1.49.0): only None means "not supplied" and falls back
        # to NEBIUS_MODEL / the default. Before this, the ``or``
        # chain silently swapped a supplied-but-falsy id ("", 0,
        # False) for the environment/default value — the exact
        # falsy-swap v1.44.0 closed for the transport argument —
        # while the same "" arriving via NEBIUS_MODEL was rejected
        # by the validation below. A supplied id now faces that
        # validation whatever its truthiness.
        self.model = model if model is not None else os.environ.get(
            "NEBIUS_MODEL", DEFAULT_MODEL)
        # The configured id is recorded as the fallback model name
        # for every call, so it must satisfy the sealed model cap
        # itself (v1.35.0): reject a non-string, empty, or over-long
        # id here instead of sealing a self-failing record later.
        if not isinstance(self.model, str) or not self.model \
                or len(self.model) > MAX_MODEL_CHARS:
            raise ValueError(
                "Token Factory model id must be a non-empty string "
                f"of at most {MAX_MODEL_CHARS} characters")
        # The remaining constructor arguments are validated the
        # same way (v1.44.0, see the module docstring): a malformed
        # timeout or transport is a usage error here, not a socket
        # surprise, a bare TypeError, or a silently swapped-in real
        # transport at call time.
        self.timeout_s = validate_timeout_s(timeout_s)
        # transport(body: dict) -> decoded JSON response dict. Tests
        # inject a fake; None selects the real surrogate call. The
        # selection is by identity with None, never by truthiness:
        # a falsy non-None transport is a caller bug, not a request
        # for the real (paid) transport.
        if transport is not None and not callable(transport):
            raise ValueError(
                "Token Factory transport must be callable or None "
                f"(got {type(transport).__name__})")
        self._transport = transport if transport is not None \
            else self._post_chat

    # -- real transport -------------------------------------------------

    def _post_chat(self, body: dict) -> dict:
        add_surrogate_to_request = _load_credential_helpers()
        req = urllib.request.Request(
            BASE_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        add_surrogate_to_request(
            req, CREDENTIAL_NAME, allowed_hosts=ALLOWED_HOSTS
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                # Bounded while reading (v1.81.0): at most the
                # ceiling + 1 bytes are ever materialised; an
                # oversize body is rejected whole below, never
                # parsed truncated. (The connector helper's own
                # reader joins every chunk with no ceiling.)
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError(
                    f"Token Factory response exceeds "
                    f"{MAX_RESPONSE_BYTES} bytes")
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:  # network / auth / connector failures
            raise RuntimeError(
                f"Nebius Token Factory call failed ({type(exc).__name__}): {exc}"
            ) from exc

    # -- chat plumbing ----------------------------------------------------

    @staticmethod
    def _response_parts(data) -> tuple[dict, dict, dict, str | None]:
        """Validate one decoded chat response; return its usable parts.

        Returns ``(choice, message, usage, finish_reason)`` where
        ``usage`` is a fresh dict carrying exactly the three token
        counts, each a non-negative int or ``None`` when the API did
        not return it — the only shape the evidence chain's consistency
        rules accept for a sealed call. Any other shape raises
        ``RuntimeError``: a malformed response is a clean adapter
        error, never an unhandled ``AttributeError`` traceback and
        never a nonsense usage entry sealed into a record (see module
        docstring).

        ``finish_reason`` is *normalised*, not validated (v1.34.0):
        it is descriptive metadata with a neutral fallback, exactly
        like the model name in :meth:`_chat`. A string within
        ``MAX_FINISH_REASON_CHARS`` is returned verbatim; a
        non-string or over-long value becomes ``None``, so a usable
        completion is never recorded with a finish reason the
        evidence chain would reject at sealing time.
        """
        def malformed() -> RuntimeError:
            return RuntimeError(
                f"unexpected Token Factory response shape: {str(data)[:300]}")

        if not isinstance(data, dict):
            raise malformed()
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise malformed()
        choice = choices[0]
        if not isinstance(choice, dict):
            raise malformed()
        message = choice.get("message")
        if not isinstance(message, dict):
            raise malformed()
        for field in ("content", "reasoning_content"):
            value = message.get(field)
            if value is not None and not isinstance(value, str):
                raise malformed()
        raw_usage = data.get("usage")
        if raw_usage is None:
            raw_usage = {}
        if not isinstance(raw_usage, dict):
            raise malformed()
        usage: dict = {}
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = raw_usage.get(field)
            if value is not None and (
                    isinstance(value, bool) or not isinstance(value, int)
                    or value < 0):
                raise malformed()
            usage[field] = value
        finish_reason = choice.get("finish_reason")
        if not (isinstance(finish_reason, str)
                and len(finish_reason) <= MAX_FINISH_REASON_CHARS):
            finish_reason = None
        return choice, message, usage, finish_reason

    def _chat(self, system: str, user: str, *, purpose: str, max_tokens: int) -> str:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        # Reasoning models occasionally burn the whole token budget
        # thinking and return an empty completion (finish_reason
        # "length"); one retry is the observed-practical remedy.
        last_finish = None
        for _attempt in range(2):
            data = self._transport(body)
            choice, message, usage, finish_reason = \
                self._response_parts(data)
            # Reasoning models may place the answer in
            # reasoning_content when content is empty; accept either.
            # Both were validated as strings (or absent) above.
            content = message.get("content") or message.get("reasoning_content") or ""
            model = data.get("model")
            self.record_call({
                "adapter": self.name,
                # An over-long model string falls back like a
                # non-string one (v1.29.0): the sealed call must
                # stay within the evidence cap for a model name.
                "model": model if isinstance(model, str) and model
                       and len(model) <= MAX_MODEL_CHARS
                       else self.model,
                "purpose": purpose,
                # Normalised in _response_parts (v1.34.0): a string
                # within the evidence cap, else None — never the
                # raw API value, which could be a non-string or an
                # over-long string the sealed record would reject.
                "finish_reason": finish_reason,
                "usage": usage,
            })
            if content.strip():
                return content
            last_finish = finish_reason
        raise RuntimeError(
            f"Token Factory returned an empty completion for {purpose} "
            f"(finish_reason={last_finish!r}) after one retry; "
            "the reasoning budget may be exhausted — raise max_tokens"
        )

    # -- the two advisory jobs -------------------------------------------

    def generate_patch(self, brief: str, failing_evidence: dict | None = None) -> str:
        # Method arguments are validated at the method (v1.52.0):
        # a non-string brief used to be stringified verbatim into
        # this paid prompt ("Task brief: 123"), a non-dict evidence
        # crashed on .items(), and a non-JSON-serializable evidence
        # value crashed in json.dumps — all before, or instead of,
        # the one clean ValueError a caller bug deserves, and the
        # brief itself was unbounded.
        brief = validate_brief(brief)
        failing_evidence = validate_failing_evidence(failing_evidence)
        user = (
            f"Task brief:\n{brief}\n\n"
            "Propose the minimal code change that satisfies the brief."
        )
        if failing_evidence:
            try:
                evidence_json = json.dumps(
                    {k: v for k, v in failing_evidence.items()
                     if k not in ("candidate_files", "patch_error")},
                    indent=2,
                )
            except (TypeError, ValueError):
                raise ValueError(
                    "failing_evidence must be JSON-serializable") from None
            user += (
                "\n\nA previous candidate failed these deterministic checks; "
                "address the failures:\n"
                + evidence_json[:6000]
            )
            patch_error = failing_evidence.get("patch_error")
            if patch_error:
                # Embedded truncated (v1.67.0): the loop's feedback
                # is already capped at its stored-error ceiling, but
                # a direct caller's error text is not — a prompt is a
                # paid call, so its size is bounded at the method.
                user += (
                    "\n\nYour previous diff was REJECTED by the patch "
                    f"applier: {str(patch_error)[:MAX_EMBED_PATCH_ERROR_CHARS]}\n"
                    "The usual cause is a context or removed line that "
                    "does not match the file character for character "
                    "(paraphrased, re-wrapped, or merged lines). Copy "
                    "every line from the file contents below verbatim."
                )
            files = failing_evidence.get("candidate_files") or {}
            if not isinstance(files, dict):
                files = {}
            # Embedded under the snapshot caps (v1.67.0): only
            # string path/content pairs, a file over the per-file
            # cap skipped whole (a truncated file would invite a
            # diff against contents that do not exist), and a
            # total cap across files. The repair loop's snapshot
            # already satisfies all three, so its prompts are
            # unchanged.
            embedded_total = 0
            for path, content in files.items():
                if not isinstance(path, str) \
                        or not isinstance(content, str) \
                        or len(content) > MAX_EMBED_FILE_CHARS \
                        or embedded_total + len(content) \
                        > MAX_EMBED_TOTAL_CHARS:
                    continue
                user += f"\n\nCurrent contents of {path}:\n```\n{content}\n```"
                embedded_total += len(content)
        user += (
            "\n\nReason silently: your reply must contain no deliberation, "
            "analysis, or draft diffs — earlier replies that reasoned in "
            "the open burned the whole token budget mid-thought "
            "(finish_reason=length) and never produced a usable diff. "
            "Output format: a single unified diff that applies "
            "cleanly to the current file contents shown above — copy "
            "context and removed lines exactly, character for character, "
            "and give every hunk header real line numbers, like:\n"
            "--- a/stringtools.py\n"
            "+++ b/stringtools.py\n"
            "@@ -7,3 +7,4 @@\n"
            " <one unchanged context line>\n"
            "-<the exact line being replaced>\n"
            "+<the replacement line>\n"
            " <one unchanged context line>\n"
            "Keep hunks small and anchored on the code being changed "
            "(start at its def line); do not pull module docstrings or "
            "headers into the hunk, and never merge two source lines "
            "into one. You may add a one-line summary before the diff, "
            "nothing after it. Do not claim the change works; a "
            "deterministic verifier will decide that."
        )
        return self._chat(
            "You are a coding agent working through VerifyGate, a "
            "deterministic verification harness. You propose patches; "
            "exit codes decide whether they pass.",
            user,
            purpose="generate_patch",
            max_tokens=PATCH_MAX_TOKENS,
        )

    def explain_failure(self, check_results: list[dict]) -> str:
        check_results = validate_check_results(check_results)
        failed = [c for c in check_results if c.get("verdict") == "FAIL"]
        try:
            failed_json = json.dumps(failed, indent=2)
        except (TypeError, ValueError):
            raise ValueError(
                "check_results must be JSON-serializable") from None
        return self._chat(
            "You explain failing software checks to the engineer who owns "
            "the code. Be brief, concrete, and cite the actual commands "
            "and output. Never suggest weakening a check to make it pass.",
            "These deterministic checks failed:\n"
            + failed_json[:6000]
            + "\n\nWrite a short markdown diagnosis: what failed, the most "
              "likely cause, and the smallest next fix to try.",
            purpose="explain_failure",
            max_tokens=EXPLAIN_MAX_TOKENS,
        )
