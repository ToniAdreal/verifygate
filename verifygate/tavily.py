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

Constructor validation (v1.44.0): the real adapter's constructor
arguments were taken on trust, in the same shapes the Token
Factory adapter's were. ``timeout_s`` accepted a string, ``True``
(silently one second), zero / negatives, ``None``, ``inf`` /
``nan``, or an effectively unbounded value. ``transport`` was
selected with ``or``: a non-callable truthy value constructed an
adapter whose every lookup degraded with a ``TypeError``
``last_error`` — indistinguishable in the sealed record from a
genuine Tavily outage — and a falsy non-``None`` value (``0``,
``""``) was silently replaced by the real transport. ``api_key``
accepted any type: an ``int`` was interpolated into the
``Authorization`` header, a list produced a nonsense header, and
neither was ever reported. All three are now validated at
construction: ``timeout_s`` follows the identical rule as the
Token Factory adapter's (a finite non-bool number, ``0 <
timeout_s <= MAX_TIMEOUT_S``); ``transport`` must be ``None``
(the real transport) or callable; ``api_key`` must be ``None``
(the environment fallback) or a string of at most
``MAX_API_KEY_CHARS`` characters — the empty string stays valid
and keeps its documented meaning (no key: references skipped,
noted on ``last_error``). A malformed argument is a
``ValueError`` (the CLI's exit-2 usage error) at construction,
never a degraded-looking lookup at run time.

Bound-override validation (v1.53.0): the ``limit`` argument of
:func:`normalise_references` — the one normalisation helper the
adapter and the runner's sealing path both call — was taken on
trust. A string or ``None`` crashed the ``len(refs) >= limit``
comparison with a ``TypeError``; ``True`` silently ran as a
one-reference limit; a float compared as-is; a negative limit
silently returned no references; and an enormous limit silently
lifted the reference ceiling this module exists to enforce.
``limit`` must now be a real ``int`` (never ``bool``) between 0
and :data:`MAX_RESULTS_CEILING` — an override may only tighten
the sealing limit, never loosen it past the adapter ceiling —
and a malformed one is a ``ValueError`` before any item is
normalised. (The lookup *request* count keeps its separate,
deliberately forgiving rule in ``_effective_max_results``:
lookups must never break a run, so a bad request falls back to
the default instead of raising. Normalisation is a pure
function on the sealing path; its callers pass constants.)

Bounded response read (v1.80.0): the real transport read its
response with a bare ``resp.read()`` — the whole body materialised
in memory before parsing, however large the endpoint (or anything
able to answer for it) chose to make it. Every other reader in
this project is bounded while reading (disk documents in
``jsonio``, candidate files in ``evidence`` / ``repair``, check
output in ``sandbox``); the network response was the last one
that was not. ``_post_search`` now reads at most
:data:`MAX_RESPONSE_BYTES` + 1 bytes and raises ``ValueError``
past the ceiling, which ``lookup`` degrades exactly like any
other transport failure: no references, reason on ``last_error``,
verdict untouched. The ceiling is generous over a genuine reply
(at most ``MAX_RESULTS_CEILING`` results with capped fields is
tens of kilobytes).
"""

from __future__ import annotations

import json
import math
import os
import urllib.request
from abc import ABC, abstractmethod

from .jsonio import replace_lone_surrogates

TAVILY_URL = "https://api.tavily.com/search"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024  # bounded response read (v1.80.0)
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

# Ceilings for the real adapter's constructor arguments (v1.44.0):
# the timeout ceiling mirrors the Token Factory adapter's (and,
# behind both, the spec's ceiling for a single check); the key
# ceiling is generous over any real Tavily key (they run to ~55
# characters) — the value becomes an HTTP header verbatim, so an
# unbounded one is a malformed request waiting to happen.
MAX_TIMEOUT_S = 3600
MAX_API_KEY_CHARS = 512

# Ceiling for a lookup query (v1.52.0). Queries are built by
# diagnosis_query from one failing check's last output line (at
# most ~160 characters); the ceiling is generous over that and far
# under anything a search API would accept as a query string.
MAX_QUERY_CHARS = 1000


def _query_problem(query) -> str | None:
    """Why ``query`` cannot be looked up, or None when it can.

    Lookup-argument validation (v1.52.0): ``lookup`` took its query
    on trust — the mock crashed on a non-string with a ``TypeError``
    (violating the lookup contract, *implementations must not
    raise*), and the real adapter sent a non-string verbatim as the
    JSON body's ``query``. An unusable query now degrades exactly
    like a failed lookup: no references, the reason on
    ``last_error``, never an exception.
    """
    if not isinstance(query, str):
        return (f"lookup query must be a string "
                f"(got {type(query).__name__}); references skipped")
    if len(query) > MAX_QUERY_CHARS:
        return (f"lookup query exceeds the maximum of "
                f"{MAX_QUERY_CHARS} characters; references skipped")
    return None


def validate_timeout_s(timeout_s, name: str = "timeout_s"):
    """Validate an adapter constructor's ``timeout_s`` (v1.44.0).

    Identical rule to the Token Factory adapter's helper of the
    same name (the two adapters mirror each other; see the module
    docstring): a finite ``int`` / ``float`` (never ``bool``) with
    ``0 < timeout_s <= MAX_TIMEOUT_S``, else a ``ValueError`` at
    construction.
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


def normalise_reference(item) -> dict | None:
    """Coerce one raw lookup result into the sealed reference shape.

    Returns exactly {title, url, snippet, source} with string fields
    truncated to the per-field caps, or None when the item cannot be
    a reference (not an object, or a field carrying a container —
    a dict/list has no honest string form here). Scalar field values
    (numbers, booleans) are coerced with ``str``; missing/None fields
    become "". Extra keys are dropped: the evidence record seals
    exactly this shape and nothing else. Lone surrogates — which a
    JSON ``\\ud800`` escape in a lookup response can carry — become
    U+FFFD (v1.97.0): reference text is sealed commentary, sanitised
    on this path like every other adapter text.
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
        ref[field] = replace_lone_surrogates(text[:limit])
    return ref


def validate_reference_limit(limit, name: str = "limit") -> int:
    """Return a ``normalise_references`` limit safe to enforce.

    Bound-override validation (v1.53.0, see the module docstring):
    a real ``int`` — never ``bool`` — between 0 and
    :data:`MAX_RESULTS_CEILING`, else a ``ValueError``.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) \
            or not 0 <= limit <= MAX_RESULTS_CEILING:
        raise ValueError(
            f"{name} must be an int with 0 <= {name} <= "
            f"{MAX_RESULTS_CEILING} (got {type(limit).__name__})")
    return limit


def normalise_references(raw, limit: int = MAX_REFERENCES) -> list[dict]:
    """Normalise a lookup result list: at most ``limit`` references.

    A non-list input normalises to no references (callers that need
    to *note* the malformation check ``isinstance`` themselves, as
    the runner does). Non-reference items are dropped, never sealed.
    ``limit`` is validated first (v1.53.0): a malformed limit is a
    ``ValueError``, not a ``TypeError`` from the comparison below
    or a silently lifted ceiling.
    """
    limit = validate_reference_limit(limit)
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
        problem = _query_problem(query)
        if problem is not None:
            self.last_error = problem
            return []
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
        # The constructor arguments are validated at construction
        # (v1.44.0, see the module docstring): a malformed key,
        # timeout, or transport is a usage error here, not a
        # nonsense Authorization header, a socket surprise, or a
        # permanently degraded-looking lookup at run time.
        resolved_key = api_key if api_key is not None else os.environ.get(
            "TAVILY_API_KEY", "")
        if not isinstance(resolved_key, str) \
                or len(resolved_key) > MAX_API_KEY_CHARS:
            raise ValueError(
                "Tavily api_key must be a string of at most "
                f"{MAX_API_KEY_CHARS} characters or None "
                f"(got {type(resolved_key).__name__})")
        self.api_key = resolved_key
        self.timeout_s = validate_timeout_s(timeout_s)
        # transport(body: dict) -> decoded JSON response dict. Tests
        # inject a fake; None selects the real API call. Selection
        # is by identity with None, never by truthiness (v1.44.0):
        # a falsy non-None transport is a caller bug, not a request
        # for the real transport.
        if transport is not None and not callable(transport):
            raise ValueError(
                "Tavily transport must be callable or None "
                f"(got {type(transport).__name__})")
        self._transport = transport if transport is not None \
            else self._post_search

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
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError(
                f"Tavily response exceeds {MAX_RESPONSE_BYTES} bytes")
        return json.loads(raw.decode("utf-8"))

    # -- lookup -----------------------------------------------------------

    def lookup(self, query: str, max_results: int = 3) -> list[dict]:
        self.last_error = None
        problem = _query_problem(query)
        if problem is not None:
            self.last_error = problem
            return []
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
    """Build a search query from the first failing check's key output line.

    Tolerant by design (and, since v1.52.0, tolerant of the whole
    input shape, not just missing fields): a non-list input, a
    non-object entry, or a non-string tail/command yields no query
    rather than an exception — the query is commentary plumbing,
    and a malformed one must never break the run asking for it.
    """
    if not isinstance(check_results, (list, tuple)):
        return ""
    for c in check_results:
        if not isinstance(c, dict):
            continue
        if c.get("verdict") == "FAIL":
            tail = (c.get("stderr_tail") or c.get("stdout_tail") or "")
            if not isinstance(tail, str):
                tail = ""
            lines = tail.strip().splitlines()
            last = lines[-1] if lines else (c.get("command") or "")
            if not isinstance(last, str):
                last = ""
            return f"{last[:120]} — error reference and current documentation"
    return ""
