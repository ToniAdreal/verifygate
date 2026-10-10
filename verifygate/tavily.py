"""Tavily reference lookup — used only on the diagnosis path.

When a check fails, the quality of the diagnosis depends on current
information (current error signatures, current library behaviour). The
diagnosis step therefore performs a real runtime Tavily search and
attaches the top references to the explanation stored in the evidence
record. References are commentary, never verdict inputs.

Judge Mode uses the deterministic mock below. In real mode without a
TAVILY_API_KEY, lookup degrades gracefully to no references.
"""

from __future__ import annotations

import json
import os
import urllib.request
from abc import ABC, abstractmethod


class ReferenceLookup(ABC):
    name: str = "base"

    @abstractmethod
    def lookup(self, query: str, max_results: int = 3) -> list[dict]:
        """Return [{title, url, snippet}] references for a query."""
        raise NotImplementedError


class MockReferenceLookup(ReferenceLookup):
    name = "mock-deterministic"

    def lookup(self, query: str, max_results: int = 3) -> list[dict]:
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

    def __init__(self, api_key: str | None = None, timeout_s: int = 30):
        self.api_key = api_key or os.environ.get("TAVILY_API_KEY", "")
        self.timeout_s = timeout_s

    def lookup(self, query: str, max_results: int = 3) -> list[dict]:
        if not self.api_key:
            return []
        body = json.dumps({
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.tavily.com/search",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": (r.get("content") or "")[:300],
                "source": "tavily",
            }
            for r in data.get("results", [])
        ]


def diagnosis_query(check_results: list[dict]) -> str:
    """Build a search query from the first failing check's key output line."""
    for c in check_results:
        if c["verdict"] == "FAIL":
            tail = (c.get("stderr_tail") or c.get("stdout_tail") or "").strip()
            last = tail.splitlines()[-1] if tail.splitlines() else c["command"]
            return f"{last[:120]} — error reference and current documentation"
    return ""
