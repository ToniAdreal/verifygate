"""Adapter constructor validation (v1.44.0).

The two real adapters' constructors were the last caller-supplied
surface taken on trust — the run entry points had their arguments
validated round by round (mode, spec, labels, candidate, adapters
themselves, binding hashes), and the Token Factory constructor
already validated its configured model id (v1.35.0), but neither
constructor judged its remaining arguments:

* ``timeout_s`` accepted a string, ``True`` (one second — ``bool``
  is an ``int`` subclass), zero / negatives, ``None`` (the socket
  global default, potentially forever), ``inf`` / ``nan``, and
  1,000,000,000 (effectively no timeout on a paid call);
* ``transport`` was selected with ``or``, so a non-callable truthy
  value built an adapter whose first call raised a bare
  ``TypeError`` (Token Factory) or whose every lookup degraded
  with a ``TypeError`` ``last_error`` indistinguishable from an
  outage (Tavily), and a falsy non-``None`` value was silently
  replaced by the real transport;
* Tavily's ``api_key`` accepted any type — an ``int`` was
  interpolated into the Authorization header, a list produced a
  nonsense header.

All three arguments are now validated at construction in both
adapters: a malformed value is a ``ValueError`` (the CLI's exit-2
usage error), ``None`` keeps its sentinel meanings (env fallback
for the key, the real transport for ``transport``), and valid
values are stored verbatim.
"""

import math

import pytest

from verifygate.tavily import (
    MAX_API_KEY_CHARS as TAVILY_MAX_API_KEY_CHARS,
    MAX_TIMEOUT_S as TAVILY_MAX_TIMEOUT_S,
    TavilyReferenceLookup,
    validate_timeout_s as tavily_validate_timeout_s,
)
from verifygate.tokenfactory import (
    MAX_TIMEOUT_S as TF_MAX_TIMEOUT_S,
    TokenFactoryAdapter,
    validate_timeout_s as tf_validate_timeout_s,
)

BAD_TIMEOUTS = [
    "soon", "30", True, False, None, 0, 0.0, -5, -0.5,
    TF_MAX_TIMEOUT_S + 1, 10**9, math.inf, -math.inf, math.nan,
    [30], {"t": 30}, b"30", (30,),
]
GOOD_TIMEOUTS = [1, 30, 180, 3600, 0.5, 30.5, 0.001]
BAD_TRANSPORTS = ["not-callable", "", 0, 123, 4.5, [], {}, object()]
BAD_API_KEYS = [12345, 4.5, True, ["k"], {"k": "v"}, b"tvly-key",
                "k" * (TAVILY_MAX_API_KEY_CHARS + 1)]


def _tf_response(body):
    return {
        "model": "m",
        "choices": [{
            "message": {"content": "text"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                  "total_tokens": 2},
    }


class _CallableTransport:
    def __call__(self, body):
        return _tf_response(body)


class _FalsyCallableTransport:
    """Callable whose truthiness is False: the old ``or`` selection
    discarded it for the real transport."""

    def __bool__(self):
        return False

    def __call__(self, body):
        return _tf_response(body)


# -- the shared timeout rule --------------------------------------------


def test_timeout_ceilings_agree():
    assert TF_MAX_TIMEOUT_S == TAVILY_MAX_TIMEOUT_S == 3600


@pytest.mark.parametrize("helper", [tf_validate_timeout_s,
                                    tavily_validate_timeout_s])
@pytest.mark.parametrize("bad", BAD_TIMEOUTS)
def test_validate_timeout_s_rejects_malformed(helper, bad):
    with pytest.raises(ValueError):
        helper(bad)


@pytest.mark.parametrize("helper", [tf_validate_timeout_s,
                                    tavily_validate_timeout_s])
@pytest.mark.parametrize("good", GOOD_TIMEOUTS)
def test_validate_timeout_s_accepts_and_returns(helper, good):
    assert helper(good) == good


# -- Token Factory constructor ------------------------------------------


@pytest.mark.parametrize("bad", BAD_TIMEOUTS)
def test_tokenfactory_rejects_bad_timeout(bad):
    with pytest.raises(ValueError):
        TokenFactoryAdapter(timeout_s=bad, transport=_tf_response)


@pytest.mark.parametrize("good", GOOD_TIMEOUTS)
def test_tokenfactory_accepts_good_timeout_verbatim(good):
    adapter = TokenFactoryAdapter(timeout_s=good,
                                  transport=_tf_response)
    assert adapter.timeout_s == good


@pytest.mark.parametrize("bad", BAD_TRANSPORTS)
def test_tokenfactory_rejects_non_callable_transport(bad):
    with pytest.raises(ValueError):
        TokenFactoryAdapter(transport=bad)


def test_tokenfactory_none_transport_selects_real():
    adapter = TokenFactoryAdapter()
    assert adapter._transport == adapter._post_chat


def test_tokenfactory_callable_transports_preserved():
    fn = _CallableTransport()
    assert TokenFactoryAdapter(transport=fn)._transport is fn
    falsy = _FalsyCallableTransport()
    assert TokenFactoryAdapter(transport=falsy)._transport is falsy
    assert TokenFactoryAdapter(transport=_tf_response)._transport \
        is _tf_response


def test_tokenfactory_valid_adapter_still_calls():
    adapter = TokenFactoryAdapter(timeout_s=30, transport=_tf_response)
    out = adapter.explain_failure([{
        "name": "c", "command": "x", "verdict": "FAIL", "exit_code": 1,
    }])
    assert out == "text"
    assert adapter.drain_calls()[0]["usage"]["total_tokens"] == 2


# -- Tavily constructor ---------------------------------------------------


@pytest.mark.parametrize("bad", BAD_TIMEOUTS)
def test_tavily_rejects_bad_timeout(bad):
    with pytest.raises(ValueError):
        TavilyReferenceLookup(timeout_s=bad, transport=lambda b: {})


@pytest.mark.parametrize("good", GOOD_TIMEOUTS)
def test_tavily_accepts_good_timeout_verbatim(good):
    adapter = TavilyReferenceLookup(
        timeout_s=good, transport=lambda b: {})
    assert adapter.timeout_s == good


@pytest.mark.parametrize("bad", BAD_TRANSPORTS)
def test_tavily_rejects_non_callable_transport(bad):
    with pytest.raises(ValueError):
        TavilyReferenceLookup(transport=bad)


def test_tavily_none_transport_selects_real():
    adapter = TavilyReferenceLookup(api_key="k")
    assert adapter._transport == adapter._post_search


def test_tavily_falsy_callable_transport_preserved():
    falsy = _FalsyCallableTransport()
    adapter = TavilyReferenceLookup(api_key="k", transport=falsy)
    assert adapter._transport is falsy


@pytest.mark.parametrize("bad", BAD_API_KEYS)
def test_tavily_rejects_bad_api_key(bad):
    with pytest.raises(ValueError):
        TavilyReferenceLookup(api_key=bad, transport=lambda b: {})


def test_tavily_api_key_boundaries(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert TavilyReferenceLookup(transport=lambda b: {}).api_key == ""
    assert TavilyReferenceLookup(
        api_key="", transport=lambda b: {}).api_key == ""
    assert TavilyReferenceLookup(
        api_key="k" * TAVILY_MAX_API_KEY_CHARS,
        transport=lambda b: {}).api_key == "k" * TAVILY_MAX_API_KEY_CHARS
    monkeypatch.setenv("TAVILY_API_KEY", "env-key")
    assert TavilyReferenceLookup(transport=lambda b: {}).api_key \
        == "env-key"


def test_tavily_valid_adapter_still_looks_up():
    adapter = TavilyReferenceLookup(
        api_key="k", timeout_s=30,
        transport=lambda body: {"results": [{
            "title": "T", "url": "https://example.com",
            "content": "S"}]})
    refs = adapter.lookup("query")
    assert refs == [{
        "title": "T", "url": "https://example.com",
        "snippet": "S", "source": "tavily",
    }]
    assert adapter.last_error is None
