"""Configured-model resolution (v1.49.0).

The Token Factory adapter resolved its configured model id with
``model or os.environ.get("NEBIUS_MODEL", DEFAULT_MODEL)`` — a
truthiness chain. A supplied-but-falsy id ("", 0, False) was
silently swapped for the environment/default value, so the
adapter made paid calls under a model the caller never named,
while the same "" arriving *via* NEBIUS_MODEL was rejected by
the v1.35.0 validation: the identical value judged two ways
depending on which side of the ``or`` it arrived from. This is
the falsy-swap class v1.44.0 closed for the transport argument.

Resolution is now by identity with None (the Tavily api_key
pattern): only None means "not supplied" and falls back to
NEBIUS_MODEL / the default; every supplied value faces the
non-empty-string validation.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from verifygate import cli
from verifygate.tokenfactory import DEFAULT_MODEL, TokenFactoryAdapter

APP = Path(__file__).resolve().parents[1]
FAILING = APP / "samples" / "failing"


def _transport(body):
    return {"choices": [{"message": {"content": "diag"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                      "total_tokens": 2}}


@pytest.fixture(autouse=True)
def _no_env_model(monkeypatch):
    monkeypatch.delenv("NEBIUS_MODEL", raising=False)


# -- supplied-but-falsy ids are caller bugs, not fallback requests -----


@pytest.mark.parametrize("bad", ["", 0, 0.0, False, [], {}])
def test_supplied_falsy_model_is_rejected_not_swapped(bad):
    with pytest.raises(ValueError, match="model id"):
        TokenFactoryAdapter(model=bad, transport=_transport)


def test_supplied_empty_model_is_rejected_even_with_env_set(monkeypatch):
    monkeypatch.setenv("NEBIUS_MODEL", "env/model")
    with pytest.raises(ValueError, match="model id"):
        TokenFactoryAdapter(model="", transport=_transport)


def test_supplied_falsy_model_no_longer_resolves_to_the_default():
    # The pre-fix probe: both of these silently came back with
    # DEFAULT_MODEL installed.
    for bad in ("", 0, False):
        with pytest.raises(ValueError):
            TokenFactoryAdapter(model=bad, transport=_transport)


# -- None is the one "not supplied" sentinel ---------------------------


def test_none_model_falls_back_to_the_default():
    adapter = TokenFactoryAdapter(transport=_transport)
    assert adapter.model == DEFAULT_MODEL


def test_none_model_falls_back_to_the_env_value(monkeypatch):
    monkeypatch.setenv("NEBIUS_MODEL", "env/model")
    adapter = TokenFactoryAdapter(transport=_transport)
    assert adapter.model == "env/model"


def test_explicit_model_wins_over_the_env_value(monkeypatch):
    monkeypatch.setenv("NEBIUS_MODEL", "env/model")
    adapter = TokenFactoryAdapter(model="arg/model", transport=_transport)
    assert adapter.model == "arg/model"


def test_env_supplied_empty_model_is_rejected(monkeypatch):
    # The other side of the pre-fix asymmetry: "" via the
    # environment was already rejected — pin that both routes
    # now judge the identical value the identical way.
    monkeypatch.setenv("NEBIUS_MODEL", "")
    with pytest.raises(ValueError, match="model id"):
        TokenFactoryAdapter(transport=_transport)


def test_adapter_still_completes_a_call_after_resolution():
    adapter = TokenFactoryAdapter(model="arg/model", transport=_transport)
    assert adapter.explain_failure(
        [{"name": "c", "command": "false", "verdict": "FAIL",
          "exit_code": 1, "timed_out": False,
          "stdout_tail": "", "stderr_tail": ""}]) == "diag"
    assert adapter.usage_log[-1]["model"] == "arg/model"


# -- CLI surface ---------------------------------------------------------


def test_cli_rejects_an_empty_model_with_exit_2(capsys, tmp_path):
    out = tmp_path / "record.json"
    rc = cli.main(["run", "--spec", str(APP / "samples" / "spec.json"),
                   "--candidate", str(FAILING), "--mode", "real",
                   "--model", "", "--out", str(out)])
    assert rc == 2
    assert "adapter error" in capsys.readouterr().err
    assert not out.exists()
