"""Environment-resolved values must be encodable (v1.98.0).

v1.97.0 rejected lone surrogates in text that crosses a boundary
from JSON, from adapters, or into a patch — but the three values
VerifyGate resolves from the *environment* were judged only for
type and length. ``os.environ`` decodes a variable's bytes with
surrogateescape, so a variable set by another process from
non-UTF-8 bytes arrives holding lone surrogates while still being
a ``str`` of legal length:

* ``VERIFYGATE_SIGNING_KEY`` passed ``signing_key()`` and then
  crashed the HMAC ``.encode("utf-8")`` at sealing and at
  verification — an unencodable key can never sign anything.
* ``TAVILY_API_KEY`` was interpolated into an Authorization
  header that cannot be encoded.
* ``NEBIUS_MODEL`` / a supplied model id went into the request
  body as ``\\udcXX`` escapes no API issued.

All three now face the encodability rule at the same validation
that already judged their type and length, whichever route they
arrived by (argument or environment fallback).
"""

import os
import subprocess
import sys

import pytest

from verifygate import evidence
from verifygate.tavily import TavilyReferenceLookup
from verifygate.tokenfactory import TokenFactoryAdapter

SURROGATE_KEY = "\udcff\udcfevalidkey123"
SURROGATE_MODEL = "\udcff\udcfemodel-x"


@pytest.fixture()
def bytes_env():
    """Set/restore raw environment bytes (the surrogateescape route)."""
    saved = {}

    def set_bytes(name, value):
        key = name.encode()
        if key not in saved:
            saved[key] = os.environb.get(key)
        if value is None:
            os.environb.pop(key, None)
        else:
            os.environb[key] = value

    yield set_bytes
    for key, value in saved.items():
        if value is None:
            os.environb.pop(key, None)
        else:
            os.environb[key] = value


# -- signing key ------------------------------------------------------


def test_signing_key_rejects_surrogate_argument_value(monkeypatch):
    monkeypatch.setenv("VERIFYGATE_SIGNING_KEY", SURROGATE_KEY)
    with pytest.raises(ValueError) as excinfo:
        evidence.signing_key()
    # The one clean ValueError naming the rule, never the codec's
    # UnicodeEncodeError (a ValueError subclass) leaking through.
    assert type(excinfo.value) is ValueError
    assert "encodable as UTF-8" in str(excinfo.value)
    assert "validkey123" not in str(excinfo.value)


def test_signing_key_rejects_non_utf8_env_bytes(bytes_env):
    bytes_env("VERIFYGATE_SIGNING_KEY", b"\xff\xfevalidkey123")
    with pytest.raises(ValueError) as excinfo:
        evidence.signing_key()
    assert type(excinfo.value) is ValueError


def test_signing_key_valid_values_unchanged(monkeypatch):
    monkeypatch.delenv("VERIFYGATE_SIGNING_KEY", raising=False)
    assert evidence.signing_key() == evidence.DEFAULT_DEV_KEY
    monkeypatch.setenv("VERIFYGATE_SIGNING_KEY", "a proper custom key")
    assert evidence.signing_key() == "a proper custom key"


def test_cli_run_with_surrogate_signing_key_exits_2(tmp_path):
    env = dict(os.environ)
    env["VERIFYGATE_SIGNING_KEY"] = SURROGATE_KEY
    out = tmp_path / "record.json"
    proc = subprocess.run(
        [sys.executable, "-m", "verifygate", "run",
         "--spec", "samples/spec.json", "--candidate", "samples/passing",
         "--mode", "judge", "--out", str(out)],
        capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 2
    assert "Traceback" not in proc.stderr
    assert "VERIFYGATE_SIGNING_KEY" in proc.stderr
    assert not out.exists()


# -- Tavily api key -----------------------------------------------------


def test_tavily_key_rejects_surrogate_argument():
    with pytest.raises(ValueError):
        TavilyReferenceLookup(api_key=SURROGATE_KEY,
                              transport=lambda body: {"results": []})


def test_tavily_key_rejects_non_utf8_env_bytes(bytes_env):
    bytes_env("TAVILY_API_KEY", b"\xff\xfekey123456")
    with pytest.raises(ValueError):
        TavilyReferenceLookup(transport=lambda body: {"results": []})


def test_tavily_key_valid_values_unchanged(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    lookup = TavilyReferenceLookup(
        api_key="tvly-test-key", transport=lambda body: {"results": []})
    assert lookup.api_key == "tvly-test-key"


# -- Token Factory model id ----------------------------------------------


def test_model_rejects_surrogate_argument():
    with pytest.raises(ValueError):
        TokenFactoryAdapter(model=SURROGATE_MODEL,
                            transport=lambda body: {})


def test_model_rejects_non_utf8_env_bytes(bytes_env):
    bytes_env("NEBIUS_MODEL", b"\xff\xfemodel-x")
    with pytest.raises(ValueError):
        TokenFactoryAdapter(transport=lambda body: {})


def test_model_valid_values_unchanged(monkeypatch):
    monkeypatch.delenv("NEBIUS_MODEL", raising=False)
    adapter = TokenFactoryAdapter(
        model="nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
        transport=lambda body: {})
    assert adapter.model == "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
