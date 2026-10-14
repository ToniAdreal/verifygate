"""Credential-helper discovery (v1.106.0).

The real adapter locates ``dynamic_credentials`` — the stored
connector's surrogate helper — on disk. Up to v1.105.0 it pinned
one directory (``/opt/hatch/skills/skill-creator/bin``); when the
connector-setup skill moved the module, every real-mode call
failed with "connector helpers are unavailable" while direct CLI
smokes (run with an explicit ``PYTHONPATH``) kept passing and
masked the breakage. Discovery is now an ordered candidate list.

These tests use stub modules in temp dirs — zero network, zero
real credentials — and restore ``sys.path`` / ``sys.modules``
after themselves via ``monkeypatch``.
"""

import sys

import pytest

from verifygate import tokenfactory
from verifygate.tokenfactory import _load_credential_helpers

CANONICAL = "/opt/muse/skills/custom-connector-setup/bin"
LEGACY = "/opt/hatch/skills/skill-creator/bin"

STUB = "def add_surrogate_to_request(req, name, allowed_hosts=None):\n" \
       "    return %r\n"


def _write_stub(directory, marker):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "dynamic_credentials.py").write_text(
        STUB % marker, encoding="utf-8")


def _isolate(monkeypatch, tmp_path):
    """Point discovery at temp dirs only; hide any real module."""
    monkeypatch.setattr(sys, "path", [str(tmp_path)])
    monkeypatch.delitem(sys.modules, "dynamic_credentials",
                        raising=False)


def test_canonical_location_is_the_first_candidate():
    dirs = tokenfactory._DYNAMIC_CREDENTIALS_DIRS
    assert dirs[0] == CANONICAL
    # The original Oct 10 pin stays available, last, so an older
    # machine layout still works.
    assert dirs[-1] == LEGACY
    assert all(d.startswith("/") for d in dirs)


def test_discovers_helper_in_a_candidate_dir(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    cand = tmp_path / "cand"
    _write_stub(cand, "found")
    monkeypatch.setattr(tokenfactory, "_DYNAMIC_CREDENTIALS_DIRS",
                        (str(cand),))
    helper = _load_credential_helpers()
    assert helper(None, "x") == "found"


def test_first_candidate_wins(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    first, second = tmp_path / "a", tmp_path / "b"
    _write_stub(first, "first")
    _write_stub(second, "second")
    monkeypatch.setattr(tokenfactory, "_DYNAMIC_CREDENTIALS_DIRS",
                        (str(first), str(second)))
    assert _load_credential_helpers()(None, "x") == "first"


def test_module_without_the_helper_does_not_shadow_later_candidate(
        monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    bad, good = tmp_path / "bad", tmp_path / "good"
    bad.mkdir()
    (bad / "dynamic_credentials.py").write_text(
        "SOMETHING_ELSE = 1\n", encoding="utf-8")
    _write_stub(good, "good")
    monkeypatch.setattr(tokenfactory, "_DYNAMIC_CREDENTIALS_DIRS",
                        (str(bad), str(good)))
    assert _load_credential_helpers()(None, "x") == "good"


def test_already_importable_module_wins_over_candidates(
        monkeypatch, tmp_path):
    # A caller's PYTHONPATH (the direct-CLI smoke arrangement)
    # is respected before any candidate directory is consulted.
    _isolate(monkeypatch, tmp_path)
    on_path = tmp_path / "onpath"
    _write_stub(on_path, "onpath")
    monkeypatch.setattr(sys, "path", [str(on_path)])
    cand = tmp_path / "cand"
    _write_stub(cand, "cand")
    monkeypatch.setattr(tokenfactory, "_DYNAMIC_CREDENTIALS_DIRS",
                        (str(cand),))
    assert _load_credential_helpers()(None, "x") == "onpath"


def test_missing_everywhere_is_the_clean_value_error(
        monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(tokenfactory, "_DYNAMIC_CREDENTIALS_DIRS",
                        (str(tmp_path / "nope"),))
    with pytest.raises(ValueError, match="connector helpers are "
                                         "unavailable"):
        _load_credential_helpers()
