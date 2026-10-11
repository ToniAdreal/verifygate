"""Bound-override validation (v1.53.0).

Every ceiling in the harness can be tightened per call through an
override argument — ``check_candidate_bounds`` / ``apply_patch``
(``max_files`` / ``max_bytes``), ``load_json_file`` (``max_bytes``),
and ``normalise_references`` (``limit``). Before v1.53.0 those
overrides were the last numeric limits taken on trust:

* ``True`` silently ran as a bound of 1 (``bool`` is an ``int``
  subclass) — including in the JSON loader's own error message
  ("exceeds the maximum of True bytes");
* a float compared as-is and a string crashed with an unhandled
  ``TypeError`` — inside ``apply_patch`` that ``TypeError`` is not
  a ``PatchError``, so it escaped the repair loop's rejection
  handling; in ``load_json_file`` a string crashed in
  ``max_bytes + 1`` and a float / ``None`` in ``read()``;
* a negative bound produced a nonsense rejection naming a
  negative maximum (candidate bounds) or silently no references
  (``normalise_references``);
* an enormous override (``10**9`` files, ``10**12`` bytes, a huge
  reference limit) silently *loosened* the very ceiling the
  override exists under, disabling the protection for that call.

An override may only tighten a bound now: a real ``int`` (never
``bool``) within the ceiling in force, else one ``ValueError``
before any file is counted, staged, written, opened, or
normalised.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verifygate import sandbox, tavily
from verifygate.jsonio import JsonFileError, load_json_file, validate_max_bytes
from verifygate.patch import PatchError, apply_patch
from verifygate.sandbox import (
    CandidateTooLargeError,
    check_candidate_bounds,
    validate_candidate_bound,
)
from verifygate.tavily import normalise_references, validate_reference_limit

APP = Path(__file__).resolve().parents[1]

BAD_BOUNDS = [True, False, 2.5, "3", None, -1, [3], {"n": 3}]


def _candidate(tmp_path: Path) -> Path:
    root = tmp_path / "cand"
    root.mkdir()
    (root / "a.txt").write_text("hello\n")
    return root


DIFF = "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-hello\n+hello world\n"


# -- the shared candidate-bound helper ------------------------------------


def test_candidate_bound_helper_accepts_boundaries():
    assert validate_candidate_bound(0, "max_files", 10) == 0
    assert validate_candidate_bound(10, "max_files", 10) == 10


@pytest.mark.parametrize("bad", BAD_BOUNDS + [11, 10**9])
def test_candidate_bound_helper_rejects(bad):
    with pytest.raises(ValueError, match="max_files"):
        validate_candidate_bound(bad, "max_files", 10)


# -- check_candidate_bounds overrides --------------------------------------


def test_candidate_bounds_genuine_overrides_still_work(tmp_path):
    root = _candidate(tmp_path)
    assert check_candidate_bounds(root, max_files=1, max_bytes=6) == (1, 6)
    with pytest.raises(CandidateTooLargeError):
        check_candidate_bounds(root, max_files=0)
    with pytest.raises(CandidateTooLargeError):
        check_candidate_bounds(root, max_bytes=5)


@pytest.mark.parametrize("kwargs", [
    {"max_files": True}, {"max_files": 2.5}, {"max_files": "3"},
    {"max_files": -1}, {"max_files": 10**9},
    {"max_bytes": True}, {"max_bytes": 2.5}, {"max_bytes": "3"},
    {"max_bytes": -1}, {"max_bytes": 10**12},
])
def test_candidate_bounds_reject_bad_overrides(tmp_path, kwargs):
    root = _candidate(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        check_candidate_bounds(root, **kwargs)
    # A malformed override is a usage error, not a size rejection.
    assert not isinstance(excinfo.value, CandidateTooLargeError)


def test_candidate_bounds_override_cannot_loosen_a_patched_ceiling(
        tmp_path, monkeypatch):
    root = _candidate(tmp_path)
    monkeypatch.setattr(sandbox, "MAX_CANDIDATE_FILES", 10)
    with pytest.raises(ValueError, match="max_files"):
        check_candidate_bounds(root, max_files=11)
    # ... while the patched ceiling itself still applies by default.
    assert check_candidate_bounds(root) == (1, 6)


def test_candidate_bounds_bad_override_wins_over_missing_dir(tmp_path):
    with pytest.raises(ValueError, match="max_bytes"):
        check_candidate_bounds(tmp_path / "missing", max_bytes="x")


# -- apply_patch overrides ---------------------------------------------------


def test_patch_genuine_override_still_applies(tmp_path):
    root = _candidate(tmp_path)
    assert apply_patch(root, DIFF, max_files=1, max_bytes=100) == ["a.txt"]
    assert (root / "a.txt").read_text() == "hello world\n"


@pytest.mark.parametrize("kwargs", [
    {"max_files": True}, {"max_files": 2.5}, {"max_files": "3"},
    {"max_files": -1}, {"max_files": 10**9},
    {"max_bytes": True}, {"max_bytes": 2.5}, {"max_bytes": "3"},
    {"max_bytes": -1}, {"max_bytes": 10**12},
])
def test_patch_rejects_bad_overrides_before_writing(tmp_path, kwargs):
    root = _candidate(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        apply_patch(root, DIFF, **kwargs)
    # Deliberately not a PatchError: a malformed override is a
    # usage error about the call, not a rejection of the patch —
    # and nothing was staged or written either way.
    assert not isinstance(excinfo.value, PatchError)
    assert (root / "a.txt").read_text() == "hello\n"


# -- load_json_file's max_bytes ----------------------------------------------


def test_json_max_bytes_helper_boundaries():
    assert validate_max_bytes(1) == 1
    from verifygate.jsonio import MAX_RECORD_FILE_BYTES
    assert validate_max_bytes(MAX_RECORD_FILE_BYTES) == MAX_RECORD_FILE_BYTES


@pytest.mark.parametrize("bad", BAD_BOUNDS + [0, 10**12])
def test_json_max_bytes_helper_rejects(bad):
    with pytest.raises(JsonFileError, match="max_bytes"):
        validate_max_bytes(bad)


def test_json_loader_genuine_bound_still_works(tmp_path):
    path = tmp_path / "doc.json"
    path.write_text(json.dumps({"a": 1}))
    assert load_json_file(path, max_bytes=8) == {"a": 1}
    with pytest.raises(JsonFileError, match="exceeds the maximum of 7"):
        load_json_file(path, max_bytes=7)


@pytest.mark.parametrize("bad", [True, 2.5, "1024", None, -1, 0, 10**12])
def test_json_loader_rejects_bad_max_bytes(tmp_path, bad):
    path = tmp_path / "doc.json"
    path.write_text("{}")
    with pytest.raises(JsonFileError, match="max_bytes"):
        load_json_file(path, max_bytes=bad)


def test_json_bad_max_bytes_wins_over_missing_file(tmp_path):
    with pytest.raises(JsonFileError, match="max_bytes"):
        load_json_file(tmp_path / "missing.json", max_bytes="x")


# -- normalise_references' limit ----------------------------------------------


def _ref(i: int) -> dict:
    return {"title": f"t{i}", "url": f"https://example.com/{i}",
            "snippet": f"s{i}", "source": "test"}


def test_reference_limit_helper_boundaries():
    assert validate_reference_limit(0) == 0
    assert validate_reference_limit(tavily.MAX_RESULTS_CEILING) == \
        tavily.MAX_RESULTS_CEILING


@pytest.mark.parametrize("bad", BAD_BOUNDS + [11, 10**9])
def test_reference_limit_helper_rejects(bad):
    with pytest.raises(ValueError, match="limit"):
        validate_reference_limit(bad)


def test_normalise_references_genuine_limits_still_work():
    raw = [_ref(i) for i in range(5)]
    assert len(normalise_references(raw)) == tavily.MAX_REFERENCES
    assert len(normalise_references(raw, limit=2)) == 2
    assert normalise_references(raw, limit=0) == []
    assert len(normalise_references(raw, limit=5)) == 5


@pytest.mark.parametrize("bad", [True, 2.5, "3", None, -1, 10**9])
def test_normalise_references_rejects_bad_limits(bad):
    with pytest.raises(ValueError, match="limit"):
        normalise_references([_ref(0)], limit=bad)


# -- the committed fixtures are untouched by the new validation --------------


def test_committed_records_still_load_under_genuine_bounds():
    from verifygate.jsonio import MAX_RECORD_FILE_BYTES
    for name in ("evidence_passing.json", "evidence_failing.json",
                 "evidence_real_repair.json"):
        data = load_json_file(
            APP / "samples" / name, max_bytes=MAX_RECORD_FILE_BYTES,
            kind="record")
        assert data["tool"] == "verifygate"
