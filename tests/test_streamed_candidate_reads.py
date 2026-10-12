"""Streamed / bounded candidate-file reads (v1.79.0).

Two candidate-path readers materialised whole files before judging
them. ``evidence.tree_hash`` hashed ``source.read_bytes()`` — the
whole file in memory first, though a digest never needs more than
one chunk at a time (the candidate bounds allow a single verified
file of ~256 MiB). ``repair._candidate_files`` ran ``read_text``
and only then applied its 4,000-character cap, so a snapshot-
eligible file allocated roughly twice its size (bytes + decoded
string) on its way to being skipped — a 20 MiB probe file peaked
at 40 MiB of traced allocations.

``tree_hash`` now feeds the digest chunk by chunk (the resulting
tree hashes are byte-identical), and the snapshot reads at most
``_MAX_FILE_CHARS + 1`` characters per file, which decides the
same skip and returns exactly the file the whole read would have.
"""

from __future__ import annotations

import hashlib
import json
import tracemalloc
from pathlib import Path

import pytest

from verifygate.evidence import _escape_tree_rel, tree_hash
from verifygate.repair import _MAX_FILE_CHARS, _candidate_files

APP_ROOT = Path(__file__).resolve().parents[1]
PEAK_LIMIT = 4 * 1024 * 1024  # far under the 20 MiB probe files


def _legacy_tree_hash(root: Path) -> str:
    """The pre-v1.79 computation: whole-file read_bytes per file."""
    from verifygate.sandbox import iter_candidate_files

    lines = [
        f"{_escape_tree_rel(rel)}:"
        f"{hashlib.sha256(source.read_bytes()).hexdigest()}"
        for rel, source in iter_candidate_files(root)
    ]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


@pytest.fixture()
def big_candidate(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    # 20 MiB of deterministic pattern: hashable, snapshot-eligible.
    (cand / "big.py").write_bytes(b"abcd" * (5 * 1024 * 1024))
    (cand / "small.py").write_text("x = 1\n", encoding="utf-8")
    return cand


def test_tree_hash_matches_whole_file_computation(big_candidate):
    assert tree_hash(big_candidate) == _legacy_tree_hash(big_candidate)


def test_tree_hash_never_reads_a_whole_file(big_candidate, monkeypatch):
    expected = _legacy_tree_hash(big_candidate)

    def boom(self):
        raise AssertionError("whole-file read on the tree-hash path")

    monkeypatch.setattr(Path, "read_bytes", boom)
    assert tree_hash(big_candidate) == expected


def test_tree_hash_memory_stays_bounded(big_candidate):
    tracemalloc.start()
    try:
        tree_hash(big_candidate)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < PEAK_LIMIT


def test_tree_hash_committed_sample_bindings_unchanged():
    for sample, record_name in (
        (APP_ROOT / "samples" / "passing", "evidence_passing.json"),
        (APP_ROOT / "samples-js" / "passing", "evidence_passing.json"),
    ):
        record = json.loads((sample.parent / record_name).read_text(
            encoding="utf-8"))
        sealed = record["entries"][0]["candidate_tree_hash"]
        assert tree_hash(sample) == sealed


def test_snapshot_small_file_included_verbatim(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "a.py").write_text("print('hi')\n", encoding="utf-8")
    assert _candidate_files(cand) == {"a.py": "print('hi')\n"}


def test_snapshot_cap_boundary_is_in_characters(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "at.py").write_text("a" * _MAX_FILE_CHARS, encoding="utf-8")
    (cand / "over.py").write_text(
        "a" * (_MAX_FILE_CHARS + 1), encoding="utf-8")
    files = _candidate_files(cand)
    assert files.get("at.py") == "a" * _MAX_FILE_CHARS
    assert "over.py" not in files


def test_snapshot_multibyte_file_judged_by_characters_not_bytes(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    text = "é" * 2500  # 5,000 bytes on disk, 2,500 characters
    (cand / "uni.py").write_text(text, encoding="utf-8")
    assert _candidate_files(cand) == {"uni.py": text}


def test_snapshot_undecodable_file_within_cap_skipped(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "bad.py").write_bytes(b"\xff\xfe not utf-8")
    (cand / "ok.py").write_text("y = 2\n", encoding="utf-8")
    assert _candidate_files(cand) == {"ok.py": "y = 2\n"}


def test_snapshot_undecodable_bytes_past_cap_still_skipped(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    payload = b"a" * (_MAX_FILE_CHARS + 100) + b"\xff\xfe"
    (cand / "late.py").write_bytes(payload)
    assert _candidate_files(cand) == {}


def test_snapshot_never_reads_a_whole_file(tmp_path, monkeypatch):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "small.py").write_text("x = 1\n", encoding="utf-8")
    (cand / "big.py").write_bytes(b"a" * (1024 * 1024))

    def boom(self, *args, **kwargs):
        raise AssertionError("whole-file read on the snapshot path")

    monkeypatch.setattr(Path, "read_text", boom)
    assert _candidate_files(cand) == {"small.py": "x = 1\n"}


def test_snapshot_memory_stays_bounded(big_candidate):
    tracemalloc.start()
    try:
        files = _candidate_files(big_candidate)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert files == {"small.py": "x = 1\n"}
    assert peak < PEAK_LIMIT
