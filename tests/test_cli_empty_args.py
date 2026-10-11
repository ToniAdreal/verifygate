"""Supplied-but-empty optional CLI arguments (v1.50.0).

The CLI told "not supplied" apart from "supplied" by truthiness,
so an explicitly empty value was silently swapped for the
default — the falsy-swap class closed for the adapters'
transport (v1.44.0) and model id (v1.49.0), surviving at the
argument layer. ``run`` / ``repair`` / ``view`` with
``--out ""`` wrote the *default* output path, ``repair
--patched-out ""`` was silently ignored, and ``verify
--spec ""`` verified with no spec binding at all and reported
RECORD INTACT. Each is now a usage error (exit 2, nothing
written); omitting the argument keeps the default behaviour.
"""

import shutil
from pathlib import Path

from verifygate.cli import main

APP = Path(__file__).resolve().parent.parent
SPEC = APP / "samples" / "spec.json"
PASSING = APP / "samples" / "passing"
FAILING = APP / "samples" / "failing"
RECORD = APP / "samples" / "evidence_passing.json"


def test_run_empty_out_is_usage_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = main(["run", "--spec", str(SPEC), "--candidate", str(PASSING),
               "--mode", "judge", "--out", ""])
    assert rc == 2
    # The default output must NOT have been written in its place.
    assert not (tmp_path / "evidence-stringtools-slugify-fix.json").exists()


def test_run_omitted_out_still_uses_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = main(["run", "--spec", str(SPEC), "--candidate", str(PASSING),
               "--mode", "judge"])
    assert rc == 0
    assert (tmp_path / "evidence-stringtools-slugify-fix.json").exists()


def test_repair_empty_out_is_usage_error(tmp_path):
    out = tmp_path / "report.json"
    rc = main(["repair", "--spec", str(SPEC), "--candidate", str(FAILING),
               "--mode", "judge", "--out", ""])
    assert rc == 2
    assert not out.exists()


def test_repair_empty_patched_out_is_usage_error(tmp_path):
    out = tmp_path / "report.json"
    rc = main(["repair", "--spec", str(SPEC), "--candidate", str(FAILING),
               "--mode", "judge", "--out", str(out), "--patched-out", ""])
    assert rc == 2
    assert not out.exists()


def test_repair_omitted_patched_out_still_repairs(tmp_path):
    out = tmp_path / "report.json"
    rc = main(["repair", "--spec", str(SPEC), "--candidate", str(FAILING),
               "--mode", "judge", "--out", str(out)])
    assert rc == 0
    assert out.exists()


def test_view_empty_out_is_usage_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    record = tmp_path / "rec.json"
    shutil.copy(RECORD, record)
    rc = main(["view", "--record", str(record), "--out", ""])
    assert rc == 2
    # The default rendering (<record stem>.html) must NOT appear.
    assert not (tmp_path / "rec.html").exists()


def test_view_omitted_out_still_uses_default(tmp_path):
    record = tmp_path / "rec.json"
    shutil.copy(RECORD, record)
    rc = main(["view", "--record", str(record)])
    assert rc == 0
    assert (tmp_path / "rec.html").exists()


def test_verify_empty_spec_is_usage_error(capsys):
    rc = main(["verify", "--record", str(RECORD), "--spec", ""])
    assert rc == 2
    assert "INTACT" not in capsys.readouterr().out


def test_verify_genuine_spec_still_binds():
    rc = main(["verify", "--record", str(RECORD), "--spec", str(SPEC),
               "--candidate", str(PASSING)])
    assert rc == 0


def test_verify_omitted_spec_still_verifies_unbound():
    rc = main(["verify", "--record", str(RECORD)])
    assert rc == 0


def test_verify_empty_candidate_still_usage_error():
    # Already rejected by the library's candidate-directory
    # validation (v1.41.0); pinned here so the CLI's argument
    # layer keeps both bindings to the same judgement.
    rc = main(["verify", "--record", str(RECORD), "--candidate", ""])
    assert rc == 2
