"""Symlink-loop traversal (v1.68.0).

``sandbox.iter_candidate_files`` — the single traversal behind the
workspace copy, ``evidence.tree_hash``, and the repair loop's prompt
snapshot — resolved every symlink with a bare ``Path.resolve()``.
A symlink loop (a link pointing at itself, or two links pointing at
each other, at the top level or inside a subdirectory) makes
``resolve()`` raise ``RuntimeError: Symlink loop from ...``, which
escaped the traversal: the workspace copy, the tree hash, and the
snapshot all crashed, and at the CLI the run died as an adapter
error (exit 2) with no evidence record — even though the
candidate's real files were fine and every other unusable link
(dangling, pointing outside, a directory link) is simply dropped.
An unresolvable link is now dropped exactly like those.
"""

import hashlib
import shutil
from pathlib import Path

from verifygate import sandbox
from verifygate.cli import main
from verifygate.evidence import tree_hash, verify_chain
from verifygate.mock_llm import MockLLMAdapter
from verifygate.repair import _candidate_files
from verifygate.runner import run_verification
from verifygate.sandbox import iter_candidate_files
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup

APP = Path(__file__).resolve().parent.parent
SPEC_PATH = APP / "samples" / "spec.json"


def _candidate(tmp_path):
    root = tmp_path / "cand"
    root.mkdir()
    (root / "a.txt").write_text("hello\n")
    (root / "b.py").write_text("x = 1\n")
    return root


def _rels(root):
    return [rel for rel, _src in iter_candidate_files(root)]


# -- the loops are dropped, the real files still traverse ---------------------


def test_self_loop_link_dropped(tmp_path):
    root = _candidate(tmp_path)
    (root / "loop").symlink_to("loop")
    assert _rels(root) == ["a.txt", "b.py"]


def test_pair_loop_links_dropped(tmp_path):
    root = _candidate(tmp_path)
    (root / "link_a").symlink_to("link_b")
    (root / "link_b").symlink_to("link_a")
    assert _rels(root) == ["a.txt", "b.py"]


def test_loop_in_subdirectory_dropped(tmp_path):
    root = _candidate(tmp_path)
    sub = root / "sub"
    sub.mkdir()
    (sub / "c.txt").write_text("nested\n")
    (sub / "loop").symlink_to("loop")
    assert _rels(root) == ["a.txt", "b.py", "sub/c.txt"]


def test_link_through_looping_link_dropped(tmp_path):
    # A link whose target path passes *through* a looping link is
    # unresolvable for the same reason and drops the same way.
    root = _candidate(tmp_path)
    (root / "loop").symlink_to("loop")
    (root / "through").symlink_to("loop/a.txt")
    assert _rels(root) == ["a.txt", "b.py"]


def test_genuine_links_unchanged_beside_a_loop(tmp_path):
    root = _candidate(tmp_path)
    (root / "inside_link.txt").symlink_to(root / "a.txt")
    (root / "dangling.txt").symlink_to("no-such-file")
    (root / "loop").symlink_to("loop")
    assert _rels(root) == ["a.txt", "b.py", "inside_link.txt"]


# -- every consumer of the traversal survives a looping candidate ------------


def test_tree_hash_ignores_loop(tmp_path):
    root = _candidate(tmp_path)
    before = tree_hash(root)
    (root / "loop").symlink_to("loop")
    assert tree_hash(root) == before


def test_workspace_copy_ignores_loop(tmp_path):
    root = _candidate(tmp_path)
    (root / "loop").symlink_to("loop")
    ws = sandbox.prepare_workspace(root)
    try:
        assert sorted(p.name for p in ws.iterdir()) == ["a.txt", "b.py"]
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_repair_snapshot_ignores_loop(tmp_path):
    root = _candidate(tmp_path)
    (root / "loop").symlink_to("loop")
    snapshot = _candidate_files(root)
    assert sorted(snapshot) == ["a.txt", "b.py"]


# -- end to end: a looping candidate still gets its verdict -------------------


def _sample_candidate_with_loop(tmp_path):
    root = tmp_path / "cand"
    shutil.copytree(APP / "samples" / "passing", root)
    (root / "loop").symlink_to("loop")
    return root


def _run(root):
    return run_verification(
        spec=load_spec(SPEC_PATH),
        candidate_dir=root,
        mode="judge",
        llm=MockLLMAdapter(),
        references=MockReferenceLookup(),
        spec_label="samples/spec.json",
        candidate_label="cand",
    )


def test_run_verification_passes_with_loop_link(tmp_path):
    record = _run(_sample_candidate_with_loop(tmp_path))
    assert record["verdict"] == "PASS"
    assert verify_chain(record) is True


def test_cli_run_passes_with_loop_link(tmp_path):
    root = _sample_candidate_with_loop(tmp_path)
    out = tmp_path / "record.json"
    code = main(
        [
            "run",
            "--spec",
            str(SPEC_PATH),
            "--candidate",
            str(root),
            "--mode",
            "judge",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    assert out.exists()


def test_original_candidate_byte_unchanged_after_run(tmp_path):
    root = _sample_candidate_with_loop(tmp_path)
    target = root / "stringtools.py"
    before = hashlib.md5(target.read_bytes()).hexdigest()
    _run(root)
    assert hashlib.md5(target.read_bytes()).hexdigest() == before
    assert (root / "loop").is_symlink()
