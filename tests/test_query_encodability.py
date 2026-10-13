"""The lookup query must be encodable: lone surrogates (v1.101.0).

The encodability passes judged the text sent to external APIs
field by field — the Tavily API *key* in v1.98.0, the spec
brief in v1.100.0 — and ``_query_problem`` (v1.52.0) judged the
lookup query's type and length. The query's encodability was
left: a query carrying lone surrogates, reachable when a direct
caller builds one with ``diagnosis_query`` from check results
whose output tail carries a ``\\ud800`` escape decoded from
JSON, was echoed verbatim by the mock lookup into its canned
reference's snippet — text no UTF-8 encoding can carry, handed
back as if it were a reference — and sent verbatim by the real
adapter as the search body's ``query``.

A surrogate query now degrades exactly like every other
unusable query (the lookup contract: implementations must not
raise): no references, the reason on ``last_error``, and for
the real adapter no transport call at all. A NUL byte in a
query stays legal, exactly as in a brief: it is valid UTF-8.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from verifygate.tavily import (
    MockReferenceLookup,
    TavilyReferenceLookup,
    _query_problem,
    diagnosis_query,
)

SURROGATE_QUERY = "bad \ud800 tail — error reference and current documentation"
CLEAN_QUERY = "slugify AssertionError — error reference and current documentation"


def _real_lookup():
    bodies: list[dict] = []

    def fake_transport(body):
        bodies.append(body)
        return {"results": [{
            "title": "Real docs",
            "url": "https://example.com/docs",
            "content": "Snippet text",
        }]}

    lookup = TavilyReferenceLookup(
        api_key="tvly-test-key", transport=fake_transport)
    return lookup, bodies


# -- the problem helper ---------------------------------------------------


def test_query_problem_flags_surrogates():
    problem = _query_problem(SURROGATE_QUERY)
    assert problem is not None
    assert "lone surrogates" in problem
    assert "references skipped" in problem


def test_query_problem_accepts_clean_and_nul_queries():
    assert _query_problem(CLEAN_QUERY) is None
    # A NUL byte is valid UTF-8 and stays legal, as in a brief.
    assert _query_problem("bad \x00 tail") is None
    assert _query_problem("") is None


# -- the mock lookup --------------------------------------------------------


def test_mock_lookup_degrades_on_surrogate_query():
    lookup = MockReferenceLookup()
    assert lookup.lookup(SURROGATE_QUERY) == []
    assert lookup.last_error is not None
    assert "lone surrogates" in lookup.last_error


def test_mock_lookup_clean_query_unchanged():
    lookup = MockReferenceLookup()
    refs = lookup.lookup(CLEAN_QUERY)
    assert len(refs) == 1
    assert lookup.last_error is None
    # The canned snippet echoes the query and encodes cleanly.
    refs[0]["snippet"].encode("utf-8")


def test_mock_lookup_nul_query_still_references():
    lookup = MockReferenceLookup()
    refs = lookup.lookup("bad \x00 tail")
    assert len(refs) == 1
    assert lookup.last_error is None


# -- the real lookup --------------------------------------------------------


def test_real_lookup_degrades_without_any_transport_call():
    lookup, bodies = _real_lookup()
    assert lookup.lookup(SURROGATE_QUERY) == []
    assert bodies == []
    assert lookup.last_error is not None
    assert "lone surrogates" in lookup.last_error


def test_real_lookup_clean_query_unchanged():
    lookup, bodies = _real_lookup()
    refs = lookup.lookup(CLEAN_QUERY)
    assert len(refs) == 1
    assert refs[0]["source"] == "tavily"
    assert bodies[0]["query"] == CLEAN_QUERY
    assert lookup.last_error is None


# -- the diagnosis_query composition ---------------------------------------


def test_diagnosis_query_from_surrogate_tail_degrades_in_both_lookups():
    query = diagnosis_query([{
        "verdict": "FAIL",
        "command": "pytest -q",
        "stderr_tail": "AssertionError: bad \ud800 tail",
    }])
    # diagnosis_query is tolerant plumbing and still builds a query;
    # the lookups are the judgement site, and both degrade on it.
    assert "\ud800" in query
    mock = MockReferenceLookup()
    assert mock.lookup(query) == []
    assert "lone surrogates" in (mock.last_error or "")
    real, bodies = _real_lookup()
    assert real.lookup(query) == []
    assert bodies == []
    assert "lone surrogates" in (real.last_error or "")


def test_diagnosis_query_from_clean_tail_still_references():
    query = diagnosis_query([{
        "verdict": "FAIL",
        "command": "pytest -q",
        "stderr_tail": "AssertionError: slugify kept punctuation",
    }])
    mock = MockReferenceLookup()
    assert len(mock.lookup(query)) == 1
    assert mock.last_error is None
