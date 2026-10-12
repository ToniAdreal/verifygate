"""Tree-hash line escaping (v1.71.0).

``evidence.tree_hash`` hashed a tree as ``<relpath>:<file-hash>``
lines joined with newlines. The relpath was interpolated raw, so a
filename carrying a newline could forge an extra line: the one-file
tree holding Y in a file named ``a:<sha256(X)>\\nc`` (a real newline
in the name) produced exactly the joined string of the two-file tree
``a`` holding X plus ``c`` holding Y — two different candidates, one
tree hash. Every candidate binding (a sealed header, ``verify
--candidate``, replay's per-attempt tree comparison) therefore
could not tell the two trees apart.

Contract pinned here:
* relpaths are escaped before they enter a line (backslash first,
  then newline / carriage return), so the encoding is injective —
  the forged pair hashes differently, in both directions;
* a filename without a backslash, newline, or carriage return
  escapes to itself, so ordinary tree hashes are byte-identical to
  the pre-v1.71.0 values and every committed record's candidate
  binding still matches the tree on disk;
* a candidate whose name carries a newline still runs end-to-end
  in Judge Mode, seals a record that verifies, and binds to itself
  — and not to its forged twin.
"""

import json
from pathlib import Path

from verifygate.evidence import _escape_tree_rel, _sha_bytes, tree_hash, verify_chain
from verifygate.mock_llm import MockLLMAdapter
from verifygate.runner import run_verification
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup
from verifygate.verify import document_ok, verify_document

APP = Path(__file__).resolve().parent.parent
SPEC_PATH = APP / "samples" / "spec.json"

X = b"content-X"
Y = b"content-Y"


def _forged_pair(tmp_path):
    """The pre-fix collision: (one-file tree, two-file tree)."""
    two = tmp_path / "two"
    two.mkdir()
    (two / "a").write_bytes(X)
    (two / "c").write_bytes(Y)
    one = tmp_path / "one"
    one.mkdir()
    (one / f"a:{_sha_bytes(X)}\nc").write_bytes(Y)
    return one, two


# -- the escape itself ---------------------------------------------------------


def test_ordinary_paths_escape_to_themselves():
    for rel in ("a.txt", "src/deep/cart.js", "with space.md",
                "colon:name.py", "uni-\u4e2d\u6587.py"):
        assert _escape_tree_rel(rel) == rel


def test_escape_is_unambiguous():
    assert _escape_tree_rel("a\nb") == "a\\nb"
    assert _escape_tree_rel("a\rb") == "a\\rb"
    assert _escape_tree_rel("a\\b") == "a\\\\b"
    # A literal backslash-n filename and a newline filename differ.
    assert _escape_tree_rel("a\\nb") != _escape_tree_rel("a\nb")
    # No escaped form ever contains a literal line break.
    for rel in ("a\nb", "a\rb", "a\r\nb", "\\\n\r"):
        escaped = _escape_tree_rel(rel)
        assert "\n" not in escaped and "\r" not in escaped


# -- the collision is closed ---------------------------------------------------


def test_forged_pair_hashes_differently(tmp_path):
    one, two = _forged_pair(tmp_path)
    assert tree_hash(one) != tree_hash(two)


def test_newline_and_literal_names_hash_differently(tmp_path):
    newline = tmp_path / "newline"
    newline.mkdir()
    (newline / "a\nb").write_bytes(X)
    literal = tmp_path / "literal"
    literal.mkdir()
    (literal / "a\\nb").write_bytes(X)
    assert tree_hash(newline) != tree_hash(literal)


def test_carriage_return_name_hashes_differently(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "ab").write_bytes(X)
    cr = tmp_path / "cr"
    cr.mkdir()
    (cr / "a\rb").write_bytes(X)
    assert tree_hash(cr) != tree_hash(plain)


# -- ordinary hashes are unchanged (committed bindings survive) -----------------


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


def test_all_committed_fixtures_still_verify():
    fixtures = [
        APP / "evidence_self_check.json",
        APP / "samples" / "evidence_passing.json",
        APP / "samples" / "evidence_failing.json",
        APP / "samples" / "evidence_real_fail.json",
        APP / "samples" / "evidence_real_fail_v2.json",
        APP / "samples" / "evidence_real_repair.json",
        APP / "samples" / "evidence_real_repair_v2.json",
        APP / "samples" / "evidence_real_repair_v3.json",
        APP / "samples-js" / "evidence_passing.json",
        APP / "samples-js" / "evidence_failing.json",
        APP / "samples-js" / "evidence_real_repair.json",
        APP / "samples-js" / "evidence_real_repair_v3.json",
    ]
    for path in fixtures:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert document_ok(verify_document(data)), path


# -- end to end: a newline-named candidate binds to itself, not its twin --------


def test_record_binds_to_its_own_tree_not_the_forged_twin(tmp_path):
    one, two = _forged_pair(tmp_path)
    spec = load_spec(SPEC_PATH)
    # Both trees lack the sample's code, so both FAIL; the verdict is
    # not what is on trial — the binding is.
    record_one = run_verification(
        spec=spec, candidate_dir=one, llm=MockLLMAdapter(),
        references=MockReferenceLookup(),
        mode="judge", spec_label="spec.json", candidate_label="one")
    assert verify_chain(record_one)
    assert document_ok(verify_document(record_one, candidate_dir=one))
    results = verify_document(record_one, candidate_dir=two)
    assert not document_ok(results)
    assert results[0][1]["candidate_match"] is False
