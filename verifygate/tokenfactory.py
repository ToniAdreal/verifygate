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

Nothing in here can affect a verdict; it only produces advisory text.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

from .llm import LLMAdapter

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


def _load_credential_helpers():
    """Import the dynamic-credential helpers, or explain what's missing."""
    if _DYNAMIC_CREDENTIALS_DIR not in sys.path:
        sys.path.insert(0, _DYNAMIC_CREDENTIALS_DIR)
    try:
        from dynamic_credentials import add_surrogate_to_request, read_json_response
    except ImportError as exc:
        raise ValueError(
            "Nebius connector helpers are unavailable on this machine; "
            "use --mode judge for a zero-key run"
        ) from exc
    return add_surrogate_to_request, read_json_response


class TokenFactoryAdapter(LLMAdapter):
    name = "nebius-token-factory"

    def __init__(
        self,
        model: str | None = None,
        timeout_s: int = 180,
        transport=None,
    ):
        super().__init__()
        self.model = model or os.environ.get("NEBIUS_MODEL", DEFAULT_MODEL)
        self.timeout_s = timeout_s
        # transport(body: dict) -> decoded JSON response dict. Tests
        # inject a fake; the default performs the real surrogate call.
        self._transport = transport or self._post_chat

    # -- real transport -------------------------------------------------

    def _post_chat(self, body: dict) -> dict:
        add_surrogate_to_request, read_json_response = _load_credential_helpers()
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
                return read_json_response(resp)
        except Exception as exc:  # network / auth / connector failures
            raise RuntimeError(
                f"Nebius Token Factory call failed ({type(exc).__name__}): {exc}"
            ) from exc

    # -- chat plumbing ----------------------------------------------------

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
            try:
                choice = data["choices"][0]
                message = choice["message"]
            except (KeyError, IndexError, TypeError) as exc:
                raise RuntimeError(
                    f"unexpected Token Factory response shape: {str(data)[:300]}"
                ) from exc
            # Reasoning models may place the answer in
            # reasoning_content when content is empty; accept either.
            content = message.get("content") or message.get("reasoning_content") or ""
            usage = data.get("usage") or {}
            self.record_call({
                "adapter": self.name,
                "model": data.get("model") or self.model,
                "purpose": purpose,
                "finish_reason": choice.get("finish_reason"),
                "usage": {
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "total_tokens": usage.get("total_tokens"),
                },
            })
            if content.strip():
                return content
            last_finish = choice.get("finish_reason")
        raise RuntimeError(
            f"Token Factory returned an empty completion for {purpose} "
            f"(finish_reason={last_finish!r}) after one retry; "
            "the reasoning budget may be exhausted — raise max_tokens"
        )

    # -- the two advisory jobs -------------------------------------------

    def generate_patch(self, brief: str, failing_evidence: dict | None = None) -> str:
        user = (
            f"Task brief:\n{brief}\n\n"
            "Propose the minimal code change that satisfies the brief."
        )
        if failing_evidence:
            user += (
                "\n\nA previous candidate failed these deterministic checks; "
                "address the failures:\n"
                + json.dumps(
                    {k: v for k, v in failing_evidence.items()
                     if k not in ("candidate_files", "patch_error")},
                    indent=2,
                )[:6000]
            )
            if failing_evidence.get("patch_error"):
                user += (
                    "\n\nYour previous diff was REJECTED by the patch "
                    f"applier: {failing_evidence['patch_error']}\n"
                    "The usual cause is a context or removed line that "
                    "does not match the file character for character "
                    "(paraphrased, re-wrapped, or merged lines). Copy "
                    "every line from the file contents below verbatim."
                )
            files = failing_evidence.get("candidate_files") or {}
            for path, content in files.items():
                user += f"\n\nCurrent contents of {path}:\n```\n{content}\n```"
        user += (
            "\n\nOutput format: a single unified diff that applies "
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
        failed = [c for c in check_results if c["verdict"] == "FAIL"]
        return self._chat(
            "You explain failing software checks to the engineer who owns "
            "the code. Be brief, concrete, and cite the actual commands "
            "and output. Never suggest weakening a check to make it pass.",
            "These deterministic checks failed:\n"
            + json.dumps(failed, indent=2)[:6000]
            + "\n\nWrite a short markdown diagnosis: what failed, the most "
              "likely cause, and the smallest next fix to try.",
            purpose="explain_failure",
            max_tokens=EXPLAIN_MAX_TOKENS,
        )
