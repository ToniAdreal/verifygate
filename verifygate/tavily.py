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

Reference bounds (v1.26.0): references are sealed into the evidence
record, so their count and field sizes are bounded like every other
dimension of a run. Before this, the real adapter never sliced its
results to ``max_results`` (a response carrying 500 results returned
all 500, titles and URLs unbounded), and the runner sealed whatever
shape any lookup implementation returned — malformed references from
a third-party lookup produced a record that failed VerifyGate's own
chain verification the moment it was written. Now one normalisation
helper (``normalise_references``) is shared by the real adapter and
the runner: the adapter slices its response to the requested count
(hard ceiling ``MAX_RESULTS_CEILING``), the runner seals at most
``MAX_REFERENCES`` references per diagnosis — each exactly
{title, url, snippet, source} with per-field length caps — and
``evidence``'s structure check mirrors the sealing bounds.
"""

from __future__ import annotations

import json
import os
import urllib.request
from abc import ABC, abstractmethod

TAVILY_URL = "https://api.tavily.com/search"
MAX_REFERENCES = 3
MAX_RESULTS_CEILING = 10
TITLE_LIMIT = 300
URL_LIMIT = 2048
SNIPPET_LIMIT = 300
SOURCE_LIMIT = 100
_FIELD_LIMITS = {
    "title": TITLE_LIMIT,
    "url": URL_LIMIT,
    "snippet": SNIPPET_LIMIT,
    "source": SOURCE_LIMIT,
}
_ERROR_LIMIT = 200


def normalise_reference(item) -> dict | None:
    """Coerce one raw lookup result into the sealed reference shape.

    Returns exactly {title, url, snippet, source} with string fields
    truncated to the per-field caps, or None when the item cannot be
    a reference (not an object, or a field carrying a container —
    a dict/list has no honest string form here). Scalar field values
    (numbers, booleans) are coerced with ``str``; missing/None fields
    become "". Extra keys are dropped: the evidence record seals
    exactly this shape and nothing else.
    """
    if not isinstance(item, dict):
        return None
    ref: dict = {}
    for field, limit in _FIELD_LIMITS.items():
        raw_key = "content" if field == "snippet" and "snippet" not in item \
            else field
        value = item.get(raw_key)
        if value is None:
            text = ""
        elif isinstance(value, str):
            text = value
        elif isinstance(value, (dict, list, tuple, set)):
            return None
        else:
            text = str(value)
        ref[field] = text[:limit]
    return ref


def normalise_references(raw, limit: int = MAX_REFERENCES) -> list[dict]:
    """Normalise a lookup result list: at most ``limit`` references.

    A non-list input normalises to no references (callers that need
    to *note* the malformation check ``isinstance`` themselves, as
    the runner does). Non-reference items are dropped, never sealed.
    """
    if not isinstance(raw, list):
        return []
    refs: list[dict] = []
    for item in raw:
        if len(refs) >= limit:
            break
        ref = normalise_reference(item)
        if ref is not None:
            refs.append(ref)
    return refs


def _effective_max_results(max_results) -> int:
    """Clamp a requested result count to 1..MAX_RESULTS_CEILING.

    A bool or non-int request is not worth raising over (lookups
    must never break a run); it falls back to the default. The
    per-diagnosis sealing limit is the stricter MAX_REFERENCES,
    enforced by the runner — the only path that seals references
    into a record — and mirrored in evidence's structure check.
    """
    if isinstance(max_results, bool) or not isinstance(max_results, int):
        return MAX_REFERENCES
    return max(1, min(max_results, MAX_RESULTS_CEILING))


def validate_references(references, name: str = "references"):
    """Validate a reference-lookup argument (v1.42.0).

    A lookup is duck-typed, exactly as the existing test stubs
    rely on: it need not subclass :class:`ReferenceLookup`, and
    ``name`` / ``last_error`` stay optional — the runner reads
    them through ``getattr`` / normalisation. What is *required*
    is a callable ``lookup``.

    Before this, ``None``, a string, or a bare object was accepted
    verbatim: a PASS run sealed its verdict with no lookup at all,
    and a FAIL run crashed with an unhandled ``AttributeError``
    on ``references.name`` after the guarded lookup call — losing
    the already-decided record. A malformed lookup is now a
    ``ValueError`` (the CLI's exit-2 usage error) raised before
    any workspace is prepared or any check runs.
    """
    if references is None:
        raise ValueError(
            f"{name} is required (a reference lookup, got None)")
    if not callable(getattr(references, "lookup", None)):
        raise ValueError(
            f"{name} must provide a callable lookup() "
            f"(got {type(references).__name__})")
    return references


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
        effective = _effective_max_results(max_results)
        body = {
            "query": query,
            "max_results": effective,
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
        # The API is asked for `effective` results, but the response
        # is not trusted to honour it: slice and normalise regardless,
        # and pin the source to this adapter.
        refs = normalise_references(results, effective)
        for ref in refs:
            ref["source"] = "tavily"
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
