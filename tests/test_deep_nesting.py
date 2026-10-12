"""Deep-nesting traversal (v1.70.0).

``sandbox.iter_candidate_files`` — the single traversal behind
the workspace copy, ``evidence.tree_hash``, and the repair
loop's prompt snapshot — walked the tree by recursion, and
``prepare_workspace`` recreated the directory skeleton the
same way. A candidate nested deeper than Python's recursion
limit (1,000 frames by default) therefore crashed the copy,
the hash, and the snapshot with a bare ``RecursionError``
(a ``RuntimeError``, so the CLI surfaced it as an adapter
error, exit 2, with no record), even though its files were
fine and within every size bound. ``shutil.rmtree`` walks by
recursion too, so removing a deep workspace copy raised the
same error out of the runner's cleanup and leaked the copy.
The traversal, the skeleton, and workspace removal are now
iterative: depth is bounded by the filesystem, not by the
interpreter's call stack.
"""

import contextlib
import subprocess
import sys
import tempfile
from pathlib import Path

from verifygate import sandbox
from verifygate.cli import main
from verifygate.evidence import tree_hash, verify_chain
from verifygate.mock_llm import MockLLMAdapter
from verifygate.runner import run_verification
from verifygate.sandbox import (
    check_candidate_bounds,
    iter_candidate_files,
    prepare_workspace,
    remove_tree,
)
from verifygate.spec import parse_spec
from verifygate.tavily import MockReferenceLookup

# Comfortably beyond the interpreter's recursion limit, while
# the full path stays well under the filesystem's PATH_MAX
# (single-character directory names: ~2 chars per level).
DEPTH = sys.getrecursionlimit() + 200


@contextlib.contextmanager
def _deep_candidate():
    """A candidate nested DEPTH levels deep, cleaned with rm -rf.

    Python's own recursive removal (tempfile / shutil) cannot
    clean a tree this deep — that is part of the bug — so the
    fixture shells out to ``rm -rf`` (iterative, in C) instead.
    """
    td = tempfile.mkdtemp()
    try:
        root = Path(td) / "cand"
        root.mkdir()
        (root / "ok.txt").write_text("ok\n")
        (root / "check.py").write_text("print('ok')\n")
        deepest_rel = "/".join(["d"] * DEPTH)
        deepest = root / deepest_rel
        subprocess.run(["mkdir", "-p", str(deepest)], check=True)
        (deepest / "file.txt").write_text("deep\n")
        # An empty directory at depth, for the skeleton copy.
        empty_rel = "/".join(["e"] * 50)
        subprocess.run(
            ["mkdir", "-p", str(root / empty_rel)], check=True)
        yield root, deepest_rel, empty_rel
    finally:
        subprocess.run(["rm", "-rf", td], check=False)


def _remove(path):
    subprocess.run(["rm", "-rf", str(path)], check=False)


# -- the traversal itself ------------------------------------------------------


def test_iter_deep_candidate():
    with _deep_candidate() as (root, deepest_rel, _empty):
        rels = [rel for rel, _src in iter_candidate_files(root)]
        assert f"{deepest_rel}/file.txt" in rels
        assert "ok.txt" in rels
        assert "check.py" in rels


def test_iter_order_preserved_shallow(tmp_path):
    root = tmp_path / "cand"
    root.mkdir()
    (root / "b.txt").write_text("b\n")
    (root / "a").mkdir()
    (root / "a" / "z.txt").write_text("z\n")
    (root / "a" / "a.txt").write_text("a\n")
    (root / "c").mkdir()
    (root / "c" / "m.txt").write_text("m\n")
    rels = [rel for rel, _src in iter_candidate_files(root)]
    # The iterative walk must keep the recursive walk's exact
    # sorted depth-first order (tree hashes depend on it).
    assert rels == ["a/a.txt", "a/z.txt", "b.txt", "c/m.txt"]


def test_tree_hash_and_bounds_deep():
    with _deep_candidate() as (root, _deepest, _empty):
        digest = tree_hash(root)
        assert isinstance(digest, str) and len(digest) == 64
        count, total = check_candidate_bounds(root)
        assert count == 3
        assert total > 0


# -- workspace copy and removal -------------------------------------------------


def test_prepare_workspace_deep_and_remove_tree():
    with _deep_candidate() as (root, deepest_rel, empty_rel):
        ws = prepare_workspace(root)
        try:
            assert (ws / deepest_rel / "file.txt").read_text() == "deep\n"
            assert (ws / empty_rel).is_dir()
            assert (ws / "ok.txt").read_text() == "ok\n"
        finally:
            remove_tree(ws)
        assert not ws.exists()


def test_remove_tree_deep():
    with _deep_candidate() as (root, _deepest, _empty):
        target = root / "ok.txt"
        assert target.exists()
        remove_tree(root)
        assert not root.exists()


def test_remove_tree_missing_file_and_symlink(tmp_path):
    # Missing paths are ignored, a file is unlinked, and a
    # symlink is removed without following it.
    remove_tree(tmp_path / "no-such-dir")
    f = tmp_path / "f.txt"
    f.write_text("x\n")
    remove_tree(f)
    assert not f.exists()
    external = tmp_path / "external.txt"
    external.write_text("keep\n")
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "link").symlink_to(external)
    (tree / "real.txt").write_text("r\n")
    remove_tree(tree)
    assert not tree.exists()
    assert external.read_text() == "keep\n"


# -- end to end: a deep candidate still gets its verdict -----------------------


def _deep_spec():
    return parse_spec({
        "name": "deep-test",
        "brief": "deep nesting check",
        "checks": [
            {"name": "c1", "command": "python3 check.py",
             "timeout_s": 10},
        ],
    })


def test_run_verification_deep_passes():
    with _deep_candidate() as (root, _deepest, _empty):
        record = run_verification(
            spec=_deep_spec(),
            candidate_dir=root,
            mode="judge",
            llm=MockLLMAdapter(),
            references=MockReferenceLookup(),
            spec_label="spec.json",
            candidate_label="cand",
        )
        assert record["verdict"] == "PASS"
        assert verify_chain(record) is True


def test_cli_run_deep_passes():
    with _deep_candidate() as (root, _deepest, _empty):
        import json
        td = Path(tempfile.mkdtemp())
        try:
            spec_path = td / "spec.json"
            spec_path.write_text(
                '{"name": "deep-test", "brief": "deep", "checks": '
                '[{"name": "c1", "command": "python3 check.py", '
                '"timeout_s": 10}]}')
            out = td / "out.json"
            code = main([
                "run", "--spec", str(spec_path),
                "--candidate", str(root),
                "--mode", "judge", "--out", str(out),
            ])
            assert code == 0
            assert json.loads(out.read_text())["verdict"] == "PASS"
        finally:
            _remove(td)
