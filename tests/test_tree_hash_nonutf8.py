"""Tree-hash byte fidelity for non-UTF-8 filenames (v1.102.0).

A filename on Linux is a byte string, not text: a candidate may
legally contain a file whose name is not valid UTF-8 (a Latin-1
name committed on another system, a name produced by an archive
extraction — ``git`` tracks such names routinely). The filesystem
layer round-trips those bytes through ``surrogateescape``, so the
workspace copy, the patch applier (whose quoted-path decoding is
bytes-level, v1.85.0), and the check execution all handle such a
tree — but ``evidence.tree_hash`` joined its lines as text and
hashed ``text.encode("utf-8")``, which raises
``UnicodeEncodeError`` on the surrogate a non-UTF-8 name decodes
to. A run over such a candidate therefore lost the record its
checks had already decided (at the CLI, a cryptic codec message
and exit 2 after every check had run), and ``verify --candidate``
/ replay could not bind any record to the tree at all.

Contract pinned here:
* ``tree_hash`` hashes each line's *bytes*: the escaped relpath
  encoded as UTF-8 with ``surrogateescape``, so a filename enters
  the hash as exactly the bytes it has on disk;
* the encoding stays injective across encodings: the byte 0xFF,
  the byte 0xFE, the UTF-8 encoding of U+00FF, and the UTF-8
  encoding of U+FFFD (what an ``errors="replace"`` hash would
  collapse the first two into) all hash differently;
* a surrogate-free tree hashes byte-identically to the pre-fix
  computation, so every committed record's candidate binding is
  unchanged;
* such a candidate runs end-to-end in Judge Mode, seals a record
  that verifies and binds to itself (and not to a twin whose
  non-UTF-8 byte differs), and replays.
"""

import hashlib
import json
import os
from pathlib import Path

from verifygate import cli
from verifygate.evidence import _escape_tree_rel, _sha_bytes, tree_hash, verify_chain
from verifygate.mock_llm import MockLLMAdapter
from verifygate.replay import replay_document, replay_ok
from verifygate.runner import run_verification
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup
from verifygate.verify import document_ok, verify_document

APP = Path(__file__).resolve().parent.parent

CONTENT = b"non-utf8 name, ordinary content\n"


def _raw_file(directory: Path, raw_name: bytes, content: bytes = CONTENT) -> None:
    """Create a file whose *name* is exactly ``raw_name`` bytes."""
    fd = os.open(os.fsencode(directory) + b"/" + raw_name,
                 os.O_CREAT | os.O_WRONLY)
    try:
        os.write(fd, content)
    finally:
        os.close(fd)


def _tree(tmp_path: Path, label: str, raw_name: bytes,
          content: bytes = CONTENT) -> Path:
    tree = tmp_path / label
    tree.mkdir()
    _raw_file(tree, raw_name, content)
    return tree


def _spec(tmp_path: Path):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps({
        "name": "nonutf8-probe",
        "brief": "probe task",
        "checks": [{"name": "trivial", "command": "true", "timeout_s": 10}],
    }), encoding="utf-8")
    return load_spec(path), str(path)


# -- the hash itself ------------------------------------------------------------


def test_nonutf8_name_hashes_its_disk_bytes(tmp_path):
    tree = _tree(tmp_path, "cand", b"bad\xffname.txt")
    rel = os.listdir(tree)[0]
    line = f"{_escape_tree_rel(rel)}:{hashlib.sha256(CONTENT).hexdigest()}"
    expected = _sha_bytes(line.encode("utf-8", "surrogateescape"))
    assert tree_hash(tree) == expected


def test_nonutf8_names_are_distinguished(tmp_path):
    # Distinct raw bytes must stay distinct — including against the
    # U+FFFD replacement an errors="replace" encoding would produce.
    trees = [
        _tree(tmp_path, "ff", b"bad\xffname.txt"),
        _tree(tmp_path, "fe", b"bad\xfename.txt"),
        _tree(tmp_path, "utf8", b"bad\xc3\xbfname.txt"),
        _tree(tmp_path, "fffd", b"bad\xef\xbf\xbdname.txt"),
    ]
    hashes = [tree_hash(t) for t in trees]
    assert len(set(hashes)) == len(hashes)


def test_nonutf8_directory_name_hashes(tmp_path):
    tree = tmp_path / "cand"
    tree.mkdir()
    sub = os.fsencode(tree) + b"/dir\xffsub"
    os.mkdir(sub)
    fd = os.open(sub + b"/inner.txt", os.O_CREAT | os.O_WRONLY)
    try:
        os.write(fd, CONTENT)
    finally:
        os.close(fd)
    assert tree_hash(tree) == tree_hash(tree)  # stable, and no crash
    other = _tree(tmp_path, "other", b"inner.txt")
    assert tree_hash(tree) != tree_hash(other)


def test_nonutf8_content_change_changes_hash(tmp_path):
    tree = _tree(tmp_path, "cand", b"bad\xffname.txt")
    before = tree_hash(tree)
    # Rewrite the same raw-named file with new bytes.
    fd = os.open(os.fsencode(tree) + b"/bad\xffname.txt", os.O_WRONLY)
    try:
        os.write(fd, b"different content\n")
    finally:
        os.close(fd)
    assert tree_hash(tree) != before


def test_committed_sample_bindings_unchanged():
    pairs = [
        (APP / "samples" / "evidence_passing.json", APP / "samples" / "passing"),
        (APP / "samples" / "evidence_failing.json", APP / "samples" / "failing"),
        (APP / "samples-js" / "evidence_passing.json", APP / "samples-js" / "passing"),
        (APP / "samples-js" / "evidence_failing.json", APP / "samples-js" / "failing"),
    ]
    for record_path, candidate in pairs:
        data = json.loads(record_path.read_text(encoding="utf-8"))
        header = data["entries"][0]
        assert header["candidate_tree_hash"] == tree_hash(candidate), record_path


# -- end to end -------------------------------------------------------------------


def test_run_seals_verifying_record_that_binds_to_itself(tmp_path):
    tree = _tree(tmp_path, "cand", b"bad\xffname.txt")
    twin = _tree(tmp_path, "twin", b"bad\xfename.txt")
    spec, _ = _spec(tmp_path)
    record = run_verification(
        spec=spec, candidate_dir=tree, llm=MockLLMAdapter(),
        references=MockReferenceLookup(),
        mode="judge", spec_label="spec.json", candidate_label="cand")
    assert record["verdict"] == "PASS"
    assert verify_chain(record)
    assert document_ok(verify_document(record, candidate_dir=tree))
    results = verify_document(record, candidate_dir=twin)
    assert not document_ok(results)
    assert results[0][1]["candidate_match"] is False


def test_record_replays(tmp_path):
    tree = _tree(tmp_path, "cand", b"bad\xffname.txt")
    spec, _ = _spec(tmp_path)
    record = run_verification(
        spec=spec, candidate_dir=tree, llm=MockLLMAdapter(),
        references=MockReferenceLookup(),
        mode="judge", spec_label="spec.json", candidate_label="cand")
    results = replay_document(
        record, spec=spec, candidate_dir=tree, mode="judge",
        llm=MockLLMAdapter(), references=MockReferenceLookup(),
        spec_label="spec.json")
    assert replay_ok(results)


def test_cli_run_and_verify_with_candidate(tmp_path):
    tree = _tree(tmp_path, "cand", b"bad\xffname.txt")
    _, spec_label = _spec(tmp_path)
    out = tmp_path / "record.json"
    assert cli.main(["run", "--spec", spec_label, "--candidate", str(tree),
                     "--mode", "judge", "--out", str(out)]) == 0
    assert cli.main(["verify", "--record", str(out),
                     "--spec", spec_label, "--candidate", str(tree)]) == 0
