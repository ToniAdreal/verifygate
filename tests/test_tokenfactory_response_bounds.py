"""Bounded Token Factory response read (v1.81.0).

The real transport delegated reading its response to the
connector helper's ``read_json_response``, whose body reader
chunks the response and joins *all* of it in memory with no size
ceiling — the whole body materialised before parsing, however
large the endpoint chose to make it. ``_post_chat`` now applies
the surrogate itself and reads at most ``MAX_RESPONSE_BYTES`` + 1
bytes, rejecting an oversize body whole as the one ``RuntimeError``
adapter error (CLI exit 2), never parsing it truncated.
"""

import json
import urllib.request

import pytest

from verifygate import tokenfactory
from verifygate.tokenfactory import MAX_RESPONSE_BYTES, TokenFactoryAdapter


class FakeResponse:
    """Minimal stand-in for an HTTPResponse: honours bounded reads."""

    def __init__(self, payload: bytes):
        self._payload = payload
        self._pos = 0
        self.read_sizes: list[int] = []

    def read(self, n: int = -1) -> bytes:
        self.read_sizes.append(n)
        if n is None or n < 0:
            chunk = self._payload[self._pos:]
            self._pos = len(self._payload)
            return chunk
        chunk = self._payload[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install(monkeypatch, payload: bytes) -> FakeResponse:
    resp = FakeResponse(payload)
    monkeypatch.setattr(
        tokenfactory, "_load_credential_helpers",
        lambda: (lambda req, name, allowed_hosts=None: None))
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout=None: resp)
    return resp


def _chat_body(content: str = "the fix") -> bytes:
    return json.dumps({
        "model": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
        "choices": [{
            "message": {"content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2,
                  "total_tokens": 5},
    }).encode("utf-8")


def _adapter() -> TokenFactoryAdapter:
    # transport=None selects the real _post_chat; urlopen and the
    # credential helper are stubbed by _install, so no network and
    # no real credential is ever touched.
    return TokenFactoryAdapter()


def test_post_chat_parses_a_small_response(monkeypatch):
    _install(monkeypatch, _chat_body())
    data = _adapter()._post_chat({"model": "m", "messages": []})
    assert data["choices"][0]["message"]["content"] == "the fix"


def test_post_chat_never_reads_unbounded(monkeypatch):
    resp = _install(monkeypatch, _chat_body())
    _adapter()._post_chat({"model": "m", "messages": []})
    assert resp.read_sizes == [MAX_RESPONSE_BYTES + 1]


def test_post_chat_rejects_a_response_at_ceiling_plus_one(monkeypatch):
    _install(monkeypatch, b"x" * (MAX_RESPONSE_BYTES + 1))
    with pytest.raises(RuntimeError, match="exceeds"):
        _adapter()._post_chat({"model": "m", "messages": []})


def test_adapter_call_fails_cleanly_on_an_oversize_response(monkeypatch):
    payload = _chat_body("x" * (5 * 1024 * 1024))
    assert len(payload) > MAX_RESPONSE_BYTES
    _install(monkeypatch, payload)
    adapter = _adapter()
    with pytest.raises(RuntimeError, match="exceeds"):
        adapter.explain_failure(
            [{"name": "t", "command": "false", "verdict": "FAIL",
              "exit_code": 1, "stdout_tail": "", "stderr_tail": "boom"}])
    # Nothing was recorded: a rejected body seals no usage.
    assert adapter.drain_calls() == []


def test_adapter_call_still_works_under_the_ceiling(monkeypatch):
    _install(monkeypatch, _chat_body("root cause: the tax rate is ignored"))
    adapter = _adapter()
    text = adapter.explain_failure(
        [{"name": "t", "command": "false", "verdict": "FAIL",
          "exit_code": 1, "stdout_tail": "", "stderr_tail": "boom"}])
    assert text == "root cause: the tax rate is ignored"
    calls = adapter.drain_calls()
    assert len(calls) == 1
    assert calls[0]["usage"]["total_tokens"] == 5


def test_post_chat_invalid_json_is_the_adapter_error(monkeypatch):
    _install(monkeypatch, b"this is not json")
    with pytest.raises(RuntimeError, match="call failed"):
        _adapter()._post_chat({"model": "m", "messages": []})


def test_post_chat_non_utf8_body_is_the_adapter_error(monkeypatch):
    _install(monkeypatch, b"\xff\xfe\x00bad")
    with pytest.raises(RuntimeError, match="call failed"):
        _adapter()._post_chat({"model": "m", "messages": []})
