"""Nebius Token Factory adapter (real mode).

Talks to the Token Factory inference API, which is OpenAI-compatible, and
runs an NVIDIA Nemotron instruct model. The model id is configuration
(NEBIUS_MODEL), not a hard-coded assumption — it gets pinned to whatever
Nemotron instruct model is live on the account during integration week.

Implemented over urllib from the standard library: no SDK dependency, and
the whole request/response path is visible in this one file. Nothing in
here can affect a verdict; it only produces advisory text.
"""

from __future__ import annotations

import json
import os
import urllib.request

from .llm import LLMAdapter

DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
# Placeholder default from the Nemotron 3 family; override with NEBIUS_MODEL.
DEFAULT_MODEL = "nvidia/nemotron-3-nano-30b-a3b"


class TokenFactoryAdapter(LLMAdapter):
    name = "nebius-token-factory"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout_s: int = 120,
    ):
        self.api_key = api_key or os.environ.get("NEBIUS_API_KEY", "")
        self.model = model or os.environ.get("NEBIUS_MODEL", DEFAULT_MODEL)
        self.base_url = (base_url or os.environ.get(
            "NEBIUS_BASE_URL", DEFAULT_BASE_URL)).rstrip("/") + "/"
        self.timeout_s = timeout_s
        if not self.api_key:
            raise ValueError(
                "NEBIUS_API_KEY is not set; use --mode judge for a zero-key run"
            )

    def _chat(self, system: str, user: str) -> str:
        body = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]

    def generate_patch(self, brief: str, failing_evidence: dict | None = None) -> str:
        user = f"Task brief:\n{brief}\n\nPropose a minimal code change that satisfies it."
        if failing_evidence:
            user += (
                "\n\nA previous candidate failed these deterministic checks; "
                "address the failures:\n"
                + json.dumps(failing_evidence, indent=2)[:6000]
            )
        return self._chat(
            "You are a coding agent. Output a concise, minimal patch proposal "
            "as a unified diff plus a one-line summary. Do not claim the change "
            "works — a deterministic verifier will decide that.",
            user,
        )

    def explain_failure(self, check_results: list[dict]) -> str:
        failed = [c for c in check_results if c["verdict"] == "FAIL"]
        return self._chat(
            "You explain failing software checks to the engineer who owns the "
            "code. Be brief, concrete, and cite the actual commands and output. "
            "Never suggest weakening a check to make it pass.",
            "These deterministic checks failed:\n"
            + json.dumps(failed, indent=2)[:6000]
            + "\n\nWrite a short markdown diagnosis: what failed, the most "
              "likely cause, and the smallest next fix to try.",
        )
