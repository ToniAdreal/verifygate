"""Tavily reference lookup — used only on the diagnosis path.

When a check fails, the quality of the diagnosis depends on current
information (current error signatures, current library behaviour). The
diagnosis step therefore performs a real runtime Tavily search and
attaches the top references to the explanation stored in the evidence
record. References are commentary, never verdict inputs.

That "never a verdict input" rule is enforced structurally here: a
lookup must never break a verification run. A missing key, a network
failure, an HTTP error, or a malformed response all degrade gracefully
to *no references*, with the reason recorded on ``last_error`` so the
runner can note the degradation inside the sealed evidence record
(CONCEPT.md: "graceful degradation, noted in the record").

Judge Mode uses the deterministic mock below. The real adapter's
transport is a single method (``_post_search``) so tests can inject a
fake and exercise parsing and failure handling with zero network.
"""

from __future__ import annotations

import json
import os
import urllib.request
from abc import ABC, abstractmethod

TAVILY_URL = "https://api.tavily.com/search"
_SNIPPET_LIMIT = 300
_ERROR_LIMIT = 200


class ReferenceLookup(ABC):
    name: str = "base"

    def __init__(self) -> None:
        # Short human-readable reason the last lookup returned no (or
        # fewer) references, or None when it fully succeeded. The
        # runner copies this into the diagnosis entry so the sealed
        # record itself documents any degradation.
        self.last_error: str | None = None

    @abstractmethod
    def lookup(self, query: str, max_results: int = 3) -> list[dict]:
        """Return [{title, url, snippet, source}] references for a query.

        Implementations must not raise: references are commentary, so
        any failure degrades to an empty list plus ``last_error``.
        """
        raise NotImplementedError


class MockReferenceLookup(ReferenceLookup):
    name = "mock-deterministic"

    def lookup(self, query: str, max_results: int = 3) -> list[dict]:
        self.last_error = None
        return [
            {
                "title": "Mock reference (Judge Mode): Python documentation",
                "url": "https://docs.python.org/3/",
                "snippet": f"Canned reference for query: {query[:80]}",
                "source": "mock",
            }
        ]


class TavilyReferenceLookup(ReferenceLookup):
    name = "tavily"

    def __init__(
        self,
        api_key: str | None = None,
        timeout_s: int = 30,
        transport=None,
    ):
        super().__init__()
        self.api_key = api_key if api_key is not None else os.environ.get(
            "TAVILY_API_KEY", "")
        self.timeout_s = timeout_s
        # transport(body: dict) -> decoded JSON response dict. Tests
        # inject a fake; the default performs the real API call.
        self._transport = transport or self._post_search

    # -- real transport -------------------------------------------------

    def _post_search(self, body: dict) -> dict:
        req = urllib.request.Request(
            TAVILY_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # -- lookup -----------------------------------------------------------

    def lookup(self, query: str, max_results: int = 3) -> list[dict]:
        self.last_error = None
        if not self.api_key:
            self.last_error = (
                "TAVILY_API_KEY is not set; references skipped "
                "(diagnosis proceeds without them)")
            return []
        body = {
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
        }
        try:
            data = self._transport(body)
        except Exception as exc:  # network / HTTP / JSON failures
            self.last_error = (
                f"Tavily lookup failed ({type(exc).__name__}): "
                f"{str(exc)[:_ERROR_LIMIT]}")[:_ERROR_LIMIT + 40]
            return []
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            self.last_error = (
                "Tavily lookup returned a malformed response "
                "(no results list); references skipped")
            return []
        refs: list[dict] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            refs.append({
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "snippet": str(item.get("content") or "")[:_SNIPPET_LIMIT],
                "source": "tavily",
            })
        return refs


def diagnosis_query(check_results: list[dict]) -> str:
    """Build a search query from the first failing check's key output line."""
    for c in check_results:
        if c.get("verdict") == "FAIL":
            tail = (c.get("stderr_tail") or c.get("stdout_tail") or "").strip()
            lines = tail.splitlines()
            last = lines[-1] if lines else (c.get("command") or "")
            return f"{last[:120]} — error reference and current documentation"
    return ""
