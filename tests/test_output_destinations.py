"""Output-destination protection (v1.48.0).

No output an invocation writes may destroy an artifact that same
invocation reads. Before this, `run` / `repair` with `--out` naming
the task spec overwrote the spec with the record it had just read,
`view` with `--out` naming its record replaced the evidence record
with its own HTML rendering (the *default* output for a record file
named `*.html` is the record path itself), and `repair
--patched-out` naming the original candidate merged the patched
tree over it — the directory a repair exists to leave
byte-unchanged — and reported success. Every destination is now
checked, resolved, before any check runs or any file is written:
a conflict is exit 2 and nothing is written. Writing a *new* file
inside a candidate stays allowed (the documented dogfood command
writes its self-check record at the candidate's root).
"""

import shutil
from pathlib import Path

from verifygate.cli import _file_out_conflict, _tree_overlap, main

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


# -- run -----------------------------------------------------------------


def test_run_out_equal_to_spec_is_usage_error_and_spec_survives(tmp_path):
    spec, _failing, passing = _copy_sample_tree(tmp_path)
    before = spec.read_bytes()
    rc = main(["run", "--spec", str(spec), "--candidate", str(passing),
               "--mode", "judge", "--out", str(spec)])
    assert rc == 2
    assert spec.read_bytes() == before


def test_run_out_equal_to_candidate_dir_is_usage_error(tmp_path):
    spec, _failing, passing = _copy_sample_tree(tmp_path)
    rc = main(["run", "--spec", str(spec), "--candidate", str(passing),
               "--mode", "judge", "--out", str(passing)])
    assert rc == 2


def test_run_out_via_symlink_to_spec_is_usage_error(tmp_path):
    spec, _failing, passing = _copy_sample_tree(tmp_path)
    link = tmp_path / "link.json"
    link.symlink_to(spec)
    before = spec.read_bytes()
    rc = main(["run", "--spec", str(spec), "--candidate", str(passing),
               "--mode", "judge", "--out", str(link)])
    assert rc == 2
    assert spec.read_bytes() == before


def test_run_out_new_file_inside_candidate_still_allowed(tmp_path):
    # The documented dogfood shape: a fresh evidence file written at
    # the candidate's root is a legitimate output, not a conflict.
    spec, _failing, passing = _copy_sample_tree(tmp_path)
    out = passing / "evidence.json"
    rc = main(["run", "--spec", str(spec), "--candidate", str(passing),
               "--mode", "judge", "--out", str(out)])
    assert rc == 0
    assert out.exists()


# -- repair ----------------------------------------------------------------


def test_repair_out_equal_to_spec_is_usage_error_and_spec_survives(tmp_path):
    spec, failing, _passing = _copy_sample_tree(tmp_path)
    before = spec.read_bytes()
    rc = main(["repair", "--spec", str(spec), "--candidate", str(failing),
               "--mode", "judge", "--out", str(spec)])
    assert rc == 2
    assert spec.read_bytes() == before


def test_repair_patched_out_equal_to_candidate_leaves_it_unchanged(tmp_path):
    spec, failing, _passing = _copy_sample_tree(tmp_path)
    before = (failing / "stringtools.py").read_bytes()
    rc = main(["repair", "--spec", str(spec), "--candidate", str(failing),
               "--mode", "judge", "--out", str(tmp_path / "r.json"),
               "--patched-out", str(failing)])
    assert rc == 2
    assert (failing / "stringtools.py").read_bytes() == before


def test_repair_patched_out_inside_candidate_is_usage_error(tmp_path):
    spec, failing, _passing = _copy_sample_tree(tmp_path)
    dest = failing / "patched"
    rc = main(["repair", "--spec", str(spec), "--candidate", str(failing),
               "--mode", "judge", "--out", str(tmp_path / "r.json"),
               "--patched-out", str(dest)])
    assert rc == 2
    assert not dest.exists()


def test_repair_patched_out_ancestor_of_candidate_is_usage_error(tmp_path):
    spec, failing, _passing = _copy_sample_tree(tmp_path)
    rc = main(["repair", "--spec", str(spec), "--candidate", str(failing),
               "--mode", "judge", "--out", str(tmp_path / "r.json"),
               "--patched-out", str(tmp_path)])
    assert rc == 2


def test_repair_patched_out_equal_to_spec_is_usage_error(tmp_path):
    spec, failing, _passing = _copy_sample_tree(tmp_path)
    before = spec.read_bytes()
    rc = main(["repair", "--spec", str(spec), "--candidate", str(failing),
               "--mode", "judge", "--out", str(tmp_path / "r.json"),
               "--patched-out", str(spec)])
    assert rc == 2
    assert spec.read_bytes() == before


def test_repair_patched_out_equal_to_report_out_is_usage_error(tmp_path):
    spec, failing, _passing = _copy_sample_tree(tmp_path)
    out = tmp_path / "r.json"
    rc = main(["repair", "--spec", str(spec), "--candidate", str(failing),
               "--mode", "judge", "--out", str(out),
               "--patched-out", str(out)])
    assert rc == 2
    assert not out.exists()


def test_repair_patched_out_outside_candidate_still_works(tmp_path):
    spec, failing, _passing = _copy_sample_tree(tmp_path)
    before = (failing / "stringtools.py").read_bytes()
    dest = tmp_path / "patched"
    rc = main(["repair", "--spec", str(spec), "--candidate", str(failing),
               "--mode", "judge", "--out", str(tmp_path / "r.json"),
               "--patched-out", str(dest)])
    assert rc == 0
    assert (dest / "stringtools.py").exists()
    assert (failing / "stringtools.py").read_bytes() == before


# -- view ------------------------------------------------------------------


def test_view_out_equal_to_record_is_usage_error_and_record_survives(tmp_path):
    record = tmp_path / "rec.json"
    shutil.copy(RECORD, record)
    before = record.read_bytes()
    rc = main(["view", "--record", str(record), "--out", str(record)])
    assert rc == 2
    assert record.read_bytes() == before


def test_view_default_out_for_html_named_record_is_usage_error(tmp_path):
    # A JSON record stored in a *.html file: the default output
    # (record path with a .html suffix) IS the record path.
    record = tmp_path / "rec.html"
    shutil.copy(RECORD, record)
    before = record.read_bytes()
    rc = main(["view", "--record", str(record)])
    assert rc == 2
    assert record.read_bytes() == before


def test_view_normal_out_still_works(tmp_path):
    out = tmp_path / "v.html"
    rc = main(["view", "--record", str(RECORD), "--out", str(out)])
    assert rc == 0
    assert "Hash chain verified" in out.read_text(encoding="utf-8")


# -- helpers -----------------------------------------------------------------


def test_file_out_conflict_names_each_protected_artifact(tmp_path):
    spec, failing, _passing = _copy_sample_tree(tmp_path)
    assert _file_out_conflict(spec, spec_path=spec) is not None
    assert _file_out_conflict(failing, candidate_dir=failing) is not None
    record = tmp_path / "r.json"
    assert _file_out_conflict(record, record_path=record) is not None
    assert _file_out_conflict(tmp_path / "free.json",
                              spec_path=spec, record_path=record,
                              candidate_dir=failing) is None


def test_tree_overlap_directions(tmp_path):
    inner = tmp_path / "a" / "b"
    assert _tree_overlap(inner, tmp_path / "a")
    assert _tree_overlap(tmp_path / "a", inner)
    assert _tree_overlap(inner, inner)
    assert not _tree_overlap(tmp_path / "a", tmp_path / "ab")
    assert not _tree_overlap(tmp_path / "a", tmp_path / "b")
