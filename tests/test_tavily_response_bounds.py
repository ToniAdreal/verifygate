"""Bounded Tavily response read (v1.80.0).

The real transport read its response with a bare ``resp.read()``:
the whole body materialised in memory before parsing, however large
the endpoint chose to make it. ``_post_search`` now reads at most
``MAX_RESPONSE_BYTES`` + 1 bytes and raises past the ceiling, which
``lookup`` degrades like any other transport failure — no references,
the reason on ``last_error``, the run untouched.
"""

import json
import urllib.request

import pytest

from verifygate import tavily
from verifygate.tavily import MAX_RESPONSE_BYTES, TavilyReferenceLookup


class FakeResponse:
    """Minimal stand-in for an HTTPResponse: honours bounded reads."""

    def __init__(self, payload: bytes):
        self._payload = payload
        self.read_sizes: list[int] = []

    def read(self, n: int = -1) -> bytes:
        self.read_sizes.append(n)
        if n is None or n < 0:
            return self._payload
        return self._payload[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install(monkeypatch, payload: bytes) -> FakeResponse:
    resp = FakeResponse(payload)
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout=None: resp)
    return resp


def _body(results) -> bytes:
    return json.dumps({"results": results}).encode("utf-8")


def test_post_search_parses_a_small_response(monkeypatch):
    payload = _body([{"title": "T", "url": "https://example.com",
                      "content": "snippet"}])
    _install(monkeypatch, payload)
    data = TavilyReferenceLookup(api_key="k")._post_search({"query": "q"})
    assert data["results"][0]["title"] == "T"


def test_post_search_never_reads_unbounded(monkeypatch):
    resp = _install(monkeypatch, _body([]))
    TavilyReferenceLookup(api_key="k")._post_search({"query": "q"})
    assert resp.read_sizes == [MAX_RESPONSE_BYTES + 1]


def test_post_search_rejects_a_response_at_ceiling_plus_one(monkeypatch):
    payload = b"x" * (MAX_RESPONSE_BYTES + 1)
    _install(monkeypatch, payload)
    with pytest.raises(ValueError, match="exceeds"):
        TavilyReferenceLookup(api_key="k")._post_search({"query": "q"})


def test_lookup_degrades_on_an_oversize_response(monkeypatch):
    payload = _body([{"title": "t", "url": "https://e.com",
                      "content": "c" * 1000}] * 5000)
    assert len(payload) > MAX_RESPONSE_BYTES
    _install(monkeypatch, payload)
    lookup = TavilyReferenceLookup(api_key="k")
    # Fresh adapter: the real transport is selected, not an injected one.
    assert lookup.lookup("some error — documentation") == []
    assert lookup.last_error is not None
    assert "exceeds" in lookup.last_error


def test_lookup_still_returns_references_under_the_ceiling(monkeypatch):
    payload = _body([{"title": "Docs", "url": "https://example.com/d",
                      "content": "how to fix it"}])
    _install(monkeypatch, payload)
    lookup = TavilyReferenceLookup(api_key="k")
    refs = lookup.lookup("some error — documentation")
    assert len(refs) == 1
    assert refs[0]["title"] == "Docs"
    assert refs[0]["source"] == "tavily"
    assert lookup.last_error is None
