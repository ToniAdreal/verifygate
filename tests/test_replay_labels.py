"""Replay labels: a record must verify for any path the caller used.

Before v1.21.0 the runner interpolated the caller's spec/candidate
labels into the record's ``replay`` command raw, while the metadata
rules require that string to match a template whose labels are bare
path tokens — no whitespace, no shell metacharacters, ASCII only.
Any ordinary path outside that alphabet (a space, a quote, a Chinese
directory name) therefore produced a record that failed its own
verification the moment it was sealed: ``run`` printed PASS and
exited 0, and ``verify`` on the record it had just written reported
CHAIN INVALID. Labels are now interpolated through
``evidence.quote_label`` — bare tokens unchanged, everything else
single-quoted as ``shlex.quote`` quotes it — and the templates
accept both forms, while an *unquoted* label carrying whitespace or
metacharacters is still rejected.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

from verifygate.cli import main
from verifygate.evidence import build_record, quote_label, verify_chain
from verifygate.verify import document_ok, verify_document

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "samples" / "spec.json"
PASSING = ROOT / "samples" / "passing"
FAILING = ROOT / "samples" / "failing"

CHECK = {
    "name": "unit", "command": "pytest -q", "exit_code": 0,
    "duration_ms": 12, "stdout_tail": "", "stderr_tail": "",
    "timed_out": False, "verdict": "PASS",
}


def _record_with_replay(replay):
    return build_record(
        spec_name="demo", spec_hash="a" * 64,
        candidate_tree_hash="b" * 64, mode="judge",
        check_results=[CHECK], verdict="PASS", diagnosis=None,
        replay_command=replay)


def _spaced_tree(tmp_path):
    """The sample spec + passing candidate under spaced paths."""
    spec_dir = tmp_path / "my spec dir"
    spec_dir.mkdir()
    spec = spec_dir / "spec.json"
    shutil.copy(SPEC, spec)
    cand = tmp_path / "my cand"
    shutil.copytree(PASSING, cand)
    return spec, cand


def _run_and_load(tmp_path, spec, cand):
    out = tmp_path / "record.json"
    rc = main(["run", "--spec", str(spec), "--candidate", str(cand),
               "--mode", "judge", "--out", str(out)])
    record = json.loads(out.read_text(encoding="utf-8"))
    return rc, record


# -- quote_label ------------------------------------------------------

def test_bare_labels_are_emitted_unchanged():
    for label in ("samples/spec.json", "dogfood_spec.json", ".",
                  "<repair-report.json>", "/tmp/x/spec.json",
                  "user@host:path/to.json", "~/spec.json"):
        assert quote_label(label) == label


def test_unsafe_labels_are_single_quoted():
    assert quote_label("my spec.json") == "'my spec.json'"
    assert quote_label("a;b") == "'a;b'"
    assert quote_label("候选") == "'候选'"
    # An embedded quote is carried as the shlex '"'"' sequence.
    assert quote_label("a'b") == "'a'\"'\"'b'"


def test_control_character_label_degrades_to_placeholder():
    assert quote_label("bad\nlabel") == "<unprintable-label>"
    record = _record_with_replay(
        "python3 -m verifygate run --spec <unprintable-label> "
        "--candidate c --mode judge")
    assert verify_chain(record) is True


# -- the sealed record verifies, whatever the paths were --------------

def test_run_with_spaced_paths_seals_a_record_that_verifies(tmp_path):
    spec, cand = _spaced_tree(tmp_path)
    rc, record = _run_and_load(tmp_path, spec, cand)
    assert rc == 0
    assert record["verdict"] == "PASS"
    assert verify_chain(record) is True
    assert record["replay"] == (
        f"python3 -m verifygate run --spec '{spec}' "
        f"--candidate '{cand}' --mode judge")
    rc = main(["verify", "--record",
               str(tmp_path / "record.json")])
    assert rc == 0


def test_run_with_unicode_and_quote_paths_verifies(tmp_path):
    cand = tmp_path / "候选'b"
    shutil.copytree(PASSING, cand)
    rc, record = _run_and_load(tmp_path, SPEC, cand)
    assert rc == 0
    assert verify_chain(record) is True
    assert "候选" in record["replay"]
    assert "'" in record["replay"]  # the label is quoted, not raw


def test_emitted_replay_command_actually_runs(tmp_path):
    spec, cand = _spaced_tree(tmp_path)
    _, record = _run_and_load(tmp_path, spec, cand)
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    proc = subprocess.run(
        record["replay"], shell=True, cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0
    assert "VERDICT: PASS" in proc.stdout


def test_replay_command_cannot_be_weaponised_by_a_path(tmp_path):
    sentinel = tmp_path / "pwned"
    cand = tmp_path / f"evil$(touch {sentinel})"
    shutil.copytree(PASSING, cand)
    _, record = _run_and_load(tmp_path, SPEC, cand)
    assert verify_chain(record) is True
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    proc = subprocess.run(
        record["replay"], shell=True, cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0
    assert not sentinel.exists()


def test_failing_candidate_with_spaced_paths_still_verifies(tmp_path):
    cand = tmp_path / "my failing cand"
    shutil.copytree(FAILING, cand)
    out = tmp_path / "record.json"
    rc = main(["run", "--spec", str(SPEC), "--candidate", str(cand),
               "--mode", "judge", "--out", str(out)])
    assert rc == 1  # FAIL is a verdict, not a usage error
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["verdict"] == "FAIL"
    assert verify_chain(record) is True


def test_repair_with_spaced_candidate_verifies_and_replays(tmp_path):
    cand = tmp_path / "my failing cand"
    shutil.copytree(FAILING, cand)
    out = tmp_path / "report.json"
    rc = main(["repair", "--spec", str(SPEC), "--candidate", str(cand),
               "--mode", "judge", "--out", str(out)])
    assert rc == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    results = verify_document(report)
    assert document_ok(results)
    attempt1 = report["attempts"][1]["evidence"]
    assert f"--candidate '{cand}'" in attempt1["replay"]
    rc = main(["replay", "--record", str(out), "--spec", str(SPEC),
               "--candidate", str(cand)])
    assert rc == 0


# -- the widening accepts only the quoted form -------------------------

def test_unquoted_spaced_label_still_fails_verification():
    record = _record_with_replay(
        "python3 -m verifygate run --spec my spec.json "
        "--candidate c --mode judge")
    assert verify_chain(record) is False


def test_unquoted_metachar_label_still_fails_verification():
    record = _record_with_replay(
        "python3 -m verifygate run --spec x; rm -rf ~ "
        "--candidate c --mode judge")
    assert verify_chain(record) is False


def test_quoted_label_with_metachars_inside_verifies():
    # Inside single quotes the metacharacters are inert text; this is
    # the exact string quote_label emits for such a path.
    record = _record_with_replay(
        "python3 -m verifygate run --spec 'a; b' "
        "--candidate 'c$(x)' --mode judge")
    assert verify_chain(record) is True
