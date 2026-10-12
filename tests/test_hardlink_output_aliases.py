"""Hardlink-alias output protection (v1.86.0).

The v1.48.0 output-destination protection compared *paths*,
resolved — but two different paths can name one file. A `--out`
given as a hardlink alias of the task spec (or, for `view`, of
the record) passed every path comparison and then overwrote the
spec/record through the alias, and a `repair --patched-out`
destination directory holding a file that was a hardlink to (or
a symlink at) a candidate file passed the tree-overlap check —
which judges only the destination root — so `copytree` wrote
the patched file *through* the alias and modified the original
candidate the repair exists to leave byte-unchanged. Identity
is now also judged by inode (`st_dev` / `st_ino`, following
symlinks): an output that *is* a protected artifact, under any
name, is the same exit-2 usage error, judged before any work.
An alias of an unrelated file is not a conflict and writes
normally.
"""

import os
import shutil
from pathlib import Path

from verifygate.cli import (
    _file_out_conflict,
    _patched_out_alias_conflict,
    _same_file,
    main,
)

APP = Path(__file__).resolve().parent.parent
SPEC = APP / "samples" / "spec.json"
PASSING = APP / "samples" / "passing"
FAILING = APP / "samples" / "failing"
RECORD = APP / "samples" / "evidence_passing.json"


def _copy_sample_tree(tmp_path):
    spec = tmp_path / "spec.json"
    shutil.copy(SPEC, spec)
    failing = tmp_path / "failing"
    shutil.copytree(FAILING, failing)
    passing = tmp_path / "passing"
    shutil.copytree(PASSING, passing)
    return spec, failing, passing


def _link(target, alias):
    os.link(target, alias)
    return alias


# -- helpers -----------------------------------------------------------------


def test_same_file_sees_hardlink_alias_and_unrelated(tmp_path):
    spec, _failing, _passing = _copy_sample_tree(tmp_path)
    alias = _link(spec, tmp_path / "alias.json")
    assert _same_file(alias, spec)
    assert not _same_file(tmp_path / "missing.json", spec)
    other = tmp_path / "other.json"
    other.write_text("{}", encoding="utf-8")
    assert not _same_file(other, spec)


def test_file_out_conflict_names_hardlink_alias_of_spec(tmp_path):
    spec, _failing, _passing = _copy_sample_tree(tmp_path)
    alias = _link(spec, tmp_path / "alias.json")
    assert _file_out_conflict(alias, spec_path=spec) is not None
    free = tmp_path / "free.json"
    free.write_text("{}", encoding="utf-8")
    assert _file_out_conflict(free, spec_path=spec) is None


def test_file_out_conflict_names_hardlink_alias_of_record(tmp_path):
    record = tmp_path / "rec.json"
    shutil.copy(RECORD, record)
    alias = _link(record, tmp_path / "alias.json")
    assert _file_out_conflict(alias, record_path=record) is not None


# -- file outputs --------------------------------------------------------------


def test_run_out_hardlink_alias_of_spec_is_usage_error(tmp_path):
    spec, _failing, passing = _copy_sample_tree(tmp_path)
    alias = _link(spec, tmp_path / "alias.json")
    before = spec.read_bytes()
    rc = main(["run", "--spec", str(spec), "--candidate", str(passing),
               "--mode", "judge", "--out", str(alias)])
    assert rc == 2
    assert spec.read_bytes() == before
    assert alias.read_bytes() == before


def test_run_out_hardlink_alias_of_unrelated_file_still_writes(tmp_path):
    spec, _failing, passing = _copy_sample_tree(tmp_path)
    unrelated = tmp_path / "unrelated.json"
    unrelated.write_text("old", encoding="utf-8")
    alias = _link(unrelated, tmp_path / "alias.json")
    rc = main(["run", "--spec", str(spec), "--candidate", str(passing),
               "--mode", "judge", "--out", str(alias)])
    assert rc == 0
    assert b'"verdict": "PASS"' in alias.read_bytes()


def test_view_out_hardlink_alias_of_record_is_usage_error(tmp_path):
    record = tmp_path / "rec.json"
    shutil.copy(RECORD, record)
    alias = _link(record, tmp_path / "alias.html")
    before = record.read_bytes()
    rc = main(["view", "--record", str(record), "--out", str(alias)])
    assert rc == 2
    assert record.read_bytes() == before


def test_repair_out_hardlink_alias_of_spec_is_usage_error(tmp_path):
    spec, failing, _passing = _copy_sample_tree(tmp_path)
    alias = _link(spec, tmp_path / "alias.json")
    before = spec.read_bytes()
    rc = main(["repair", "--spec", str(spec), "--candidate", str(failing),
               "--mode", "judge", "--out", str(alias)])
    assert rc == 2
    assert spec.read_bytes() == before


# -- patched-out -----------------------------------------------------------------


def test_patched_out_alias_helper_names_candidate_alias(tmp_path):
    spec, failing, _passing = _copy_sample_tree(tmp_path)
    dest = tmp_path / "patched"
    dest.mkdir()
    _link(failing / "stringtools.py", dest / "stringtools.py")
    conflict = _patched_out_alias_conflict(
        dest, candidate_dir=failing, spec_path=spec,
        report_out=tmp_path / "r.json")
    assert conflict is not None
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "note.txt").write_text("hi", encoding="utf-8")
    assert _patched_out_alias_conflict(
        clean, candidate_dir=failing, spec_path=spec,
        report_out=tmp_path / "r.json") is None
    assert _patched_out_alias_conflict(
        tmp_path / "not-yet", candidate_dir=failing,
        spec_path=spec, report_out=tmp_path / "r.json") is None


def test_repair_patched_out_with_hardlink_to_candidate_is_usage_error(
        tmp_path):
    spec, failing, _passing = _copy_sample_tree(tmp_path)
    dest = tmp_path / "patched"
    dest.mkdir()
    _link(failing / "stringtools.py", dest / "stringtools.py")
    before = (failing / "stringtools.py").read_bytes()
    rc = main(["repair", "--spec", str(spec), "--candidate", str(failing),
               "--mode", "judge", "--out", str(tmp_path / "r.json"),
               "--patched-out", str(dest)])
    assert rc == 2
    assert (failing / "stringtools.py").read_bytes() == before
    assert not (tmp_path / "r.json").exists()


def test_repair_patched_out_with_symlink_to_candidate_is_usage_error(
        tmp_path):
    spec, failing, _passing = _copy_sample_tree(tmp_path)
    dest = tmp_path / "patched"
    dest.mkdir()
    (dest / "stringtools.py").symlink_to(failing / "stringtools.py")
    before = (failing / "stringtools.py").read_bytes()
    rc = main(["repair", "--spec", str(spec), "--candidate", str(failing),
               "--mode", "judge", "--out", str(tmp_path / "r.json"),
               "--patched-out", str(dest)])
    assert rc == 2
    assert (failing / "stringtools.py").read_bytes() == before


def test_repair_patched_out_with_hardlink_to_spec_is_usage_error(tmp_path):
    spec, failing, _passing = _copy_sample_tree(tmp_path)
    dest = tmp_path / "patched"
    dest.mkdir()
    _link(spec, dest / "copy-of-spec.json")
    before = spec.read_bytes()
    rc = main(["repair", "--spec", str(spec), "--candidate", str(failing),
               "--mode", "judge", "--out", str(tmp_path / "r.json"),
               "--patched-out", str(dest)])
    assert rc == 2
    assert spec.read_bytes() == before


def test_repair_patched_out_with_unrelated_alias_still_works(tmp_path):
    spec, failing, _passing = _copy_sample_tree(tmp_path)
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("keep me", encoding="utf-8")
    dest = tmp_path / "patched"
    dest.mkdir()
    _link(unrelated, dest / "unrelated.txt")
    before = (failing / "stringtools.py").read_bytes()
    rc = main(["repair", "--spec", str(spec), "--candidate", str(failing),
               "--mode", "judge", "--out", str(tmp_path / "r.json"),
               "--patched-out", str(dest)])
    assert rc == 0
    assert (dest / "stringtools.py").exists()
    assert (failing / "stringtools.py").read_bytes() == before
    assert unrelated.read_text(encoding="utf-8") == "keep me"


def test_repair_patched_out_with_plain_existing_file_still_works(tmp_path):
    # A pre-existing *regular* destination file with the patched
    # file's name is the established merge-overwrite shape, not
    # an alias: the copy replaces its content and nothing else.
    spec, failing, _passing = _copy_sample_tree(tmp_path)
    dest = tmp_path / "patched"
    dest.mkdir()
    (dest / "stringtools.py").write_text("stale", encoding="utf-8")
    before = (failing / "stringtools.py").read_bytes()
    rc = main(["repair", "--spec", str(spec), "--candidate", str(failing),
               "--mode", "judge", "--out", str(tmp_path / "r.json"),
               "--patched-out", str(dest)])
    assert rc == 0
    assert (dest / "stringtools.py").read_text(
        encoding="utf-8") != "stale"
    assert (failing / "stringtools.py").read_bytes() == before
