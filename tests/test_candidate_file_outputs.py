"""Candidate-file output protection (v1.89.0).

A file output was judged against the spec, the record, and the
candidate *directory itself* (v1.48.0), with inode identity for
the spec/record (v1.86.0) — but never against the files inside
the candidate. `run` / `repair` with `--out` naming an existing
candidate file overwrote that file with the evidence record and
exited 0: the record sealed a tree hash for a candidate the
invocation itself had just destroyed. The same destruction
arrived through a hardlink alias of the candidate file and
through a symlink to it, both outside the candidate tree. A
file output is now judged by inode against every entry of the
candidate tree, before any check runs. Writing a *new* file
inside a candidate stays allowed (it has no inode to match) —
the shape the dogfood staging procedure relies on.
"""

import os
import shutil
from pathlib import Path

from verifygate.cli import _file_out_conflict, main

APP = Path(__file__).resolve().parent.parent
SPEC = APP / "samples" / "spec.json"
PASSING = APP / "samples" / "passing"
FAILING = APP / "samples" / "failing"


def _copy_sample_tree(tmp_path):
    spec = tmp_path / "spec.json"
    shutil.copy(SPEC, spec)
    failing = tmp_path / "failing"
    shutil.copytree(FAILING, failing)
    passing = tmp_path / "passing"
    shutil.copytree(PASSING, passing)
    return spec, failing, passing


# -- helper ---------------------------------------------------------------


def test_file_out_conflict_names_a_candidate_entry(tmp_path):
    _spec, _failing, passing = _copy_sample_tree(tmp_path)
    target = passing / "stringtools.py"
    conflict = _file_out_conflict(target, candidate_dir=passing)
    assert conflict is not None
    assert "candidate" in conflict


def test_file_out_conflict_allows_a_new_file_inside_candidate(tmp_path):
    _spec, _failing, passing = _copy_sample_tree(tmp_path)
    fresh = passing / "evidence.json"
    assert _file_out_conflict(fresh, candidate_dir=passing) is None


# -- run ------------------------------------------------------------------


def test_run_out_equal_to_candidate_file_is_usage_error(tmp_path):
    spec, _failing, passing = _copy_sample_tree(tmp_path)
    target = passing / "stringtools.py"
    before = target.read_bytes()
    rc = main(["run", "--spec", str(spec), "--candidate", str(passing),
               "--mode", "judge", "--out", str(target)])
    assert rc == 2
    assert target.read_bytes() == before


def test_run_out_hardlink_alias_of_candidate_file_is_usage_error(tmp_path):
    spec, _failing, passing = _copy_sample_tree(tmp_path)
    target = passing / "stringtools.py"
    before = target.read_bytes()
    alias = tmp_path / "alias.json"
    os.link(target, alias)
    rc = main(["run", "--spec", str(spec), "--candidate", str(passing),
               "--mode", "judge", "--out", str(alias)])
    assert rc == 2
    assert target.read_bytes() == before
    assert alias.read_bytes() == before


def test_run_out_symlink_to_candidate_file_is_usage_error(tmp_path):
    spec, _failing, passing = _copy_sample_tree(tmp_path)
    target = passing / "stringtools.py"
    before = target.read_bytes()
    link = tmp_path / "link.json"
    link.symlink_to(target)
    rc = main(["run", "--spec", str(spec), "--candidate", str(passing),
               "--mode", "judge", "--out", str(link)])
    assert rc == 2
    assert target.read_bytes() == before


def test_run_out_candidate_file_via_dotdot_spelling_is_usage_error(tmp_path):
    spec, _failing, passing = _copy_sample_tree(tmp_path)
    target = passing / "stringtools.py"
    before = target.read_bytes()
    spelling = passing / "subdir" / ".." / "stringtools.py"
    rc = main(["run", "--spec", str(spec), "--candidate", str(passing),
               "--mode", "judge", "--out", str(spelling)])
    assert rc == 2
    assert target.read_bytes() == before


# -- repair ---------------------------------------------------------------


def test_repair_out_equal_to_candidate_file_is_usage_error(tmp_path):
    spec, failing, _passing = _copy_sample_tree(tmp_path)
    target = failing / "stringtools.py"
    before = target.read_bytes()
    rc = main(["repair", "--spec", str(spec), "--candidate", str(failing),
               "--mode", "judge", "--out", str(target)])
    assert rc == 2
    assert target.read_bytes() == before


def test_repair_out_hardlink_alias_of_candidate_file_is_usage_error(tmp_path):
    spec, failing, _passing = _copy_sample_tree(tmp_path)
    target = failing / "stringtools.py"
    before = target.read_bytes()
    alias = tmp_path / "alias.json"
    os.link(target, alias)
    rc = main(["repair", "--spec", str(spec), "--candidate", str(failing),
               "--mode", "judge", "--out", str(alias)])
    assert rc == 2
    assert target.read_bytes() == before
