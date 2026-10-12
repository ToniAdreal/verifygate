"""Environment-scrubber validation (v1.64.0).

``sanitized_env`` builds the environment every check command runs
with. Before v1.64.0 it iterated ``base.items()`` and tested each
key with ``str.startswith`` on trust: a non-mapping base crashed
with a raw ``AttributeError``, a non-string key crashed the same
way (``bytes`` keys with a ``TypeError``), and an allowlisted entry
with a non-string value passed through verbatim, so the scrubbed
environment itself made ``subprocess.Popen`` raise a raw
``TypeError``. A non-mapping base is now one ``ValueError``;
entries whose key or value is not a string are dropped exactly
the way non-allowlisted names already are, so the result is
always a ``dict[str, str]``.
"""

import os
import subprocess
from types import MappingProxyType

import pytest

from verifygate.sandbox import run_command, prepare_workspace, sanitized_env


def test_genuine_mapping_unchanged():
    env = sanitized_env({"PATH": "/bin", "AWS_SECRET_ACCESS_KEY": "x",
                         "LC_ALL": "C", "HOME": "/home/x"})
    assert env == {"PATH": "/bin", "LC_ALL": "C", "HOME": "/home/x",
                   "VERIFYGATE_SANDBOX": "1"}


def test_none_base_uses_os_environ(monkeypatch):
    monkeypatch.setenv("PATH", "/from-environ")
    monkeypatch.setenv("NEBIUS_API_KEY", "must-not-leak")
    env = sanitized_env()
    assert env["PATH"] == "/from-environ"
    assert "NEBIUS_API_KEY" not in env
    assert env["VERIFYGATE_SANDBOX"] == "1"


def test_mapping_types_accepted():
    assert sanitized_env(MappingProxyType({"PATH": "/bin"}))["PATH"] == "/bin"
    assert sanitized_env(os.environ)["VERIFYGATE_SANDBOX"] == "1"


@pytest.mark.parametrize("base", [123, 3.14, True, "PATH=/bin",
                                  [("PATH", "/bin")], b"PATH", object()])
def test_non_mapping_base_is_value_error(base):
    with pytest.raises(ValueError, match="mapping"):
        sanitized_env(base)


def test_non_mapping_error_names_type_only():
    huge = "x" * 500_000
    with pytest.raises(ValueError) as excinfo:
        sanitized_env(huge)
    assert len(str(excinfo.value)) < 200
    assert "str" in str(excinfo.value)


@pytest.mark.parametrize("key", [1, None, True, b"PATH", ("PATH",)])
def test_non_string_keys_are_dropped(key):
    env = sanitized_env({key: "x", "PATH": "/bin"})
    assert env == {"PATH": "/bin", "VERIFYGATE_SANDBOX": "1"}


@pytest.mark.parametrize("value", [123, None, True, 3.14, ["/bin"],
                                   b"/bin", {"p": 1}])
def test_non_string_values_are_dropped(value):
    env = sanitized_env({"PATH": value, "HOME": "/home/x",
                         "LC_ALL": value})
    assert env == {"HOME": "/home/x", "VERIFYGATE_SANDBOX": "1"}


def test_marker_cannot_be_overridden_by_base():
    env = sanitized_env({"VERIFYGATE_SANDBOX": "0", "PATH": "/bin"})
    assert env["VERIFYGATE_SANDBOX"] == "1"


def test_result_is_always_str_to_str():
    env = sanitized_env({"PATH": "/bin", 7: "x", "HOME": 9,
                         "LC_X": "y", b"TERM": "z"})
    assert all(isinstance(k, str) and isinstance(v, str)
               for k, v in env.items())
    assert env == {"PATH": "/bin", "LC_X": "y", "VERIFYGATE_SANDBOX": "1"}


def test_sanitized_result_can_spawn_a_subprocess():
    env = sanitized_env({"PATH": 123, "HOME": None, 5: "x"})
    # The pre-fix shape raised TypeError inside Popen; the scrubbed
    # result must always be directly spawnable.
    proc = subprocess.run(["true"], env=env)
    assert proc.returncode == 0


def test_run_command_still_scrubs_end_to_end(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "f.txt").write_text("x")
    ws = prepare_workspace(cand)
    r = run_command("python3 -c \"import os; print(os.environ.get("
                    "'VERIFYGATE_SANDBOX'))\"", cwd=ws, timeout_s=10)
    assert r.exit_code == 0
    assert "1" in r.stdout_tail
