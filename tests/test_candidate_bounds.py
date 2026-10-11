"""Candidate size bounds: the copy, the hash, and every round are bounded.

Before v1.23.0 nothing limited a candidate's size: ``prepare_workspace``
copied every verified file in full, ``tree_hash`` read every file in
full, and the repair loop repeated the copy per round — so a candidate
carrying a vendored ``node_modules`` tree, build output, or one huge
binary was copied again and again into the temp filesystem this
project has already filled in practice (the v1.0.0 Errno 28 incident).
A 12,000-file probe candidate was accepted and copied in full.

Contract pinned here:
* at most ``MAX_CANDIDATE_FILES`` verified files and
  ``MAX_CANDIDATE_BYTES`` total verified bytes; the count covers
  exactly the containment traversal's files (ignored names and
  escaping symlinks do not count);
* rejection is pre-flight — ``prepare_workspace`` raises
  ``CandidateTooLargeError`` before creating any workspace;
* ``tree_hash`` enforces the same bounds, so ``verify --candidate``
  and replay reject an oversized candidate the same way;
* at the CLI the rejection is a usage error (exit 2, no record /
  report written), never a traceback and never a FAIL verdict.
"""

import tempfile
from pathlib import Path

import pytest

from verifygate import sandbox
from verifygate.cli import main
from verifygate.evidence import tree_hash
from verifygate.mock_llm import MockLLMAdapter
from verifygate.runner import run_verification
from verifygate.sandbox import (
    CandidateTooLargeError,
    check_candidate_bounds,
    prepare_workspace,
)
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup

APP = Path(__file__).resolve().parent.parent
SPEC = load_spec(APP / "samples" / "spec.json")
PASSING = APP / "samples" / "passing"


def _candidate(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "cand"
    root.mkdir()
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def test_bounds_are_generous_ceilings():
    assert sandbox.MAX_CANDIDATE_FILES >= 10_000
    assert sandbox.MAX_CANDIDATE_BYTES >= 100 * 1024 * 1024
    # The bundled samples sit far below the ceilings.
    count, total = check_candidate_bounds(PASSING)
    assert 0 < count < sandbox.MAX_CANDIDATE_FILES // 100
    assert 0 < total < sandbox.MAX_CANDIDATE_BYTES // 100


def test_check_returns_counts_at_the_boundary(tmp_path):
    root = _candidate(tmp_path, {"a.txt": "12345", "b.txt": "678"})
    assert check_candidate_bounds(root, max_files=2, max_bytes=8) == (2, 8)
    with pytest.raises(CandidateTooLargeError):
        check_candidate_bounds(root, max_files=1, max_bytes=8)
    with pytest.raises(CandidateTooLargeError):
        check_candidate_bounds(root, max_files=2, max_bytes=7)


def test_file_count_bound_rejects_before_any_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    root = _candidate(tmp_path, {f"f{i}.txt": "x" for i in range(20)})
    monkeypatch.setattr(sandbox, "MAX_CANDIDATE_FILES", 10)
    with pytest.raises(CandidateTooLargeError, match="files"):
        prepare_workspace(root)
    # Pre-flight: the rejection created no workspace at all.
    assert [p for p in tmp_path.iterdir()
            if p.name.startswith("verifygate-")] == []


def test_byte_bound_rejects_a_large_sparse_file(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    root = tmp_path / "cand"
    root.mkdir()
    big = root / "big.bin"
    with open(big, "wb") as f:
        f.truncate(sandbox.MAX_CANDIDATE_BYTES + 1)  # sparse: no data written
    with pytest.raises(CandidateTooLargeError, match="bytes"):
        prepare_workspace(root)
    assert [p for p in tmp_path.iterdir()
            if p.name.startswith("verifygate-")] == []


def test_ignored_and_escaping_files_do_not_count(tmp_path):
    root = _candidate(tmp_path, {"real.txt": "ok"})
    cache = root / "__pycache__"
    cache.mkdir()
    (cache / "junk.pyc").write_bytes(b"\0" * 4096)
    git = root / ".git"
    git.mkdir()
    (git / "blob").write_bytes(b"\0" * 4096)
    outside = tmp_path / "outside.bin"
    with open(outside, "wb") as f:
        f.truncate(10 * 1024 * 1024)  # large, but only reached via a link
    (root / "escape.bin").symlink_to(outside)
    # The verified set is just real.txt: 2 bytes, 1 file.
    assert check_candidate_bounds(root, max_files=1, max_bytes=2) == (1, 2)


def test_tree_hash_enforces_the_bounds(tmp_path, monkeypatch):
    root = _candidate(tmp_path, {"a.txt": "12345"})
    monkeypatch.setattr(sandbox, "MAX_CANDIDATE_BYTES", 4)
    with pytest.raises(CandidateTooLargeError):
        tree_hash(root)


def test_run_verification_rejects_oversized_candidate(tmp_path, monkeypatch):
    root = _candidate(tmp_path, {"a.txt": "12345"})
    monkeypatch.setattr(sandbox, "MAX_CANDIDATE_BYTES", 4)
    with pytest.raises(CandidateTooLargeError):
        run_verification(
            spec=SPEC, candidate_dir=root, mode="judge",
            llm=MockLLMAdapter(), references=MockReferenceLookup(),
            spec_label="samples/spec.json", candidate_label=str(root))


def test_cli_run_oversized_candidate_is_usage_error(tmp_path, monkeypatch):
    root = _candidate(tmp_path, {"a.txt": "12345"})
    monkeypatch.setattr(sandbox, "MAX_CANDIDATE_BYTES", 4)
    out = tmp_path / "e.json"
    rc = main(["run", "--spec", str(APP / "samples" / "spec.json"),
               "--candidate", str(root), "--mode", "judge",
               "--out", str(out)])
    assert rc == 2
    assert not out.exists()


def test_cli_repair_oversized_candidate_is_usage_error(tmp_path, monkeypatch):
    root = _candidate(tmp_path, {"a.txt": "12345"})
    monkeypatch.setattr(sandbox, "MAX_CANDIDATE_BYTES", 4)
    out = tmp_path / "r.json"
    rc = main(["repair", "--spec", str(APP / "samples" / "spec.json"),
               "--candidate", str(root), "--mode", "judge",
               "--out", str(out)])
    assert rc == 2
    assert not out.exists()


def test_cli_verify_candidate_binding_rejects_oversized(tmp_path, monkeypatch):
    root = _candidate(tmp_path, {"a.txt": "12345"})
    monkeypatch.setattr(sandbox, "MAX_CANDIDATE_BYTES", 4)
    rc = main(["verify", "--record",
               str(APP / "samples" / "evidence_passing.json"),
               "--candidate", str(root)])
    assert rc == 2
