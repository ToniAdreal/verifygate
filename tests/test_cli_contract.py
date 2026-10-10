"""CLI contract: every subcommand reports usage / output errors as a
one-line message with exit code 2 — never as an unhandled traceback —
and `--version` agrees with the package metadata.

Before v1.4.0, `verifygate run` on a missing candidate directory and
every subcommand writing to an unwritable `--out` path crashed with a
traceback and exit 1, contradicting the documented exit-code contract
(0 = PASS / intact, 1 = FAIL / tampered, 2 = usage error) that the
other subcommands already honoured. These tests pin the contract for
all five subcommands.
"""

import json
from pathlib import Path

import pytest

from verifygate import __version__
from verifygate.cli import main

APP = Path(__file__).resolve().parent.parent
SPEC = APP / "samples" / "spec.json"
PASSING = APP / "samples" / "passing"
FAILING = APP / "samples" / "failing"
RECORD = APP / "samples" / "evidence_passing.json"


def test_version_flag_matches_package_version(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == f"verifygate {__version__}"


def test_pyproject_version_matches_package_version():
    text = (APP / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{__version__}"' in text
    assert 'verifygate = "verifygate.cli:main"' in text


def test_run_missing_candidate_is_usage_error(tmp_path):
    rc = main(["run", "--spec", str(SPEC),
               "--candidate", str(tmp_path / "does-not-exist"),
               "--mode", "judge", "--out", str(tmp_path / "e.json")])
    assert rc == 2


def test_run_candidate_that_is_a_file_is_usage_error(tmp_path):
    rc = main(["run", "--spec", str(SPEC), "--candidate", str(SPEC),
               "--mode", "judge", "--out", str(tmp_path / "e.json")])
    assert rc == 2


def test_run_unwritable_out_is_usage_error(tmp_path):
    rc = main(["run", "--spec", str(SPEC), "--candidate", str(PASSING),
               "--mode", "judge",
               "--out", str(tmp_path / "no-such-dir" / "e.json")])
    assert rc == 2


def test_repair_unwritable_out_is_usage_error(tmp_path):
    rc = main(["repair", "--spec", str(SPEC), "--candidate", str(FAILING),
               "--mode", "judge",
               "--out", str(tmp_path / "no-such-dir" / "r.json")])
    assert rc == 2


def test_repair_patched_out_under_a_file_is_usage_error(tmp_path):
    # copytree happily creates missing intermediate directories, so the
    # unwritable case is a parent path that is a regular file.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    rc = main(["repair", "--spec", str(SPEC), "--candidate", str(FAILING),
               "--mode", "judge", "--out", str(tmp_path / "r.json"),
               "--patched-out", str(blocker / "patched")])
    assert rc == 2


def test_view_unwritable_out_is_usage_error(tmp_path):
    rc = main(["view", "--record", str(RECORD),
               "--out", str(tmp_path / "no-such-dir" / "v.html")])
    assert rc == 2


def test_view_empty_record_renders_chain_invalid_not_crash(tmp_path):
    # A tool-tagged but entry-less document is not a usage error for
    # `view`: by design (since the web view landed) it renders a page
    # whose banner says the chain is invalid, instead of crashing.
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"tool": "verifygate"}), encoding="utf-8")
    out = tmp_path / "v.html"
    rc = main(["view", "--record", str(bad), "--out", str(out)])
    assert rc == 0
    assert "CHAIN INVALID" in out.read_text(encoding="utf-8")


def test_verify_missing_candidate_is_usage_error(tmp_path):
    rc = main(["verify", "--record", str(RECORD),
               "--candidate", str(tmp_path / "does-not-exist")])
    assert rc == 2


def test_verdict_exit_codes_unchanged(tmp_path):
    assert main(["run", "--spec", str(SPEC), "--candidate", str(PASSING),
                 "--mode", "judge",
                 "--out", str(tmp_path / "p.json")]) == 0
    assert main(["run", "--spec", str(SPEC), "--candidate", str(FAILING),
                 "--mode", "judge",
                 "--out", str(tmp_path / "f.json")]) == 1
