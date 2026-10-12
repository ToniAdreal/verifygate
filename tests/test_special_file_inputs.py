"""Special-file inputs: the JSON loader must not block on a FIFO.

Before v1.73.0 ``jsonio.load_json_file`` opened whatever path it
was given. A directory failed promptly, but a FIFO blocks
``open()``/``read()`` until a writer appears — so
``verifygate verify --record <fifo>`` (and ``view`` / ``replay`` /
any command loading a spec FIFO) hung indefinitely, with no
timeout, on the very artifact it was asked to judge. The loader
now stats the path first (following symlinks) and rejects any
existing path that is not a regular file as a ``JsonFileError``
(CLI exit 2). A symlink to a regular file still loads; a missing
file still raises ``OSError``.
"""

import os
from pathlib import Path

import pytest

from verifygate import cli
from verifygate.jsonio import JsonFileError, load_json_file
from verifygate.spec import SpecError, load_spec

APP = Path(__file__).resolve().parent.parent
SPEC_PATH = APP / "samples" / "spec.json"
PASSING = APP / "samples" / "passing"
PASSING_RECORD = APP / "samples" / "evidence_passing.json"

needs_fifo = pytest.mark.skipif(
    not hasattr(os, "mkfifo"), reason="platform has no mkfifo")


def _fifo(path: Path) -> Path:
    os.mkfifo(path)
    return path


# -- loader contract ------------------------------------------------------


@needs_fifo
def test_loader_rejects_fifo_without_blocking(tmp_path):
    fifo = _fifo(tmp_path / "record.json")
    with pytest.raises(JsonFileError, match="not a regular file"):
        load_json_file(fifo, max_bytes=1024, kind="record")


def test_loader_rejects_directory_as_not_regular_file(tmp_path):
    with pytest.raises(JsonFileError, match="not a regular file"):
        load_json_file(tmp_path, max_bytes=1024, kind="record")


def test_loader_rejects_dev_null_as_not_regular_file():
    devnull = Path("/dev/null")
    if not devnull.exists():
        pytest.skip("/dev/null unavailable")
    with pytest.raises(JsonFileError, match="not a regular file"):
        load_json_file(devnull, max_bytes=1024, kind="record")


@needs_fifo
def test_loader_rejects_symlink_to_fifo(tmp_path):
    fifo = _fifo(tmp_path / "real-fifo")
    link = tmp_path / "link.json"
    link.symlink_to(fifo)
    with pytest.raises(JsonFileError, match="not a regular file"):
        load_json_file(link, max_bytes=1024, kind="record")


def test_loader_still_loads_symlink_to_regular_file(tmp_path):
    target = tmp_path / "real.json"
    target.write_text('{"a": 1}', encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    assert load_json_file(link, max_bytes=1024) == {"a": 1}


def test_loader_missing_file_still_oserror_not_json_file_error(tmp_path):
    with pytest.raises(OSError) as excinfo:
        load_json_file(tmp_path / "missing.json", max_bytes=1024)
    assert not isinstance(excinfo.value, JsonFileError)


def test_loader_broken_symlink_still_oserror(tmp_path):
    link = tmp_path / "broken.json"
    link.symlink_to(tmp_path / "does-not-exist.json")
    with pytest.raises(OSError):
        load_json_file(link, max_bytes=1024)


# -- spec loading ---------------------------------------------------------


@needs_fifo
def test_load_spec_rejects_fifo_as_spec_error(tmp_path):
    fifo = _fifo(tmp_path / "spec.json")
    with pytest.raises(SpecError, match="not a regular file"):
        load_spec(fifo)


# -- CLI surfaces ---------------------------------------------------------


@needs_fifo
def test_cli_verify_rejects_fifo_record_with_exit_2(tmp_path, capsys):
    fifo = _fifo(tmp_path / "record.json")
    assert cli.main(["verify", "--record", str(fifo)]) == 2
    assert "not a regular file" in capsys.readouterr().err


@needs_fifo
def test_cli_view_rejects_fifo_record_with_exit_2(tmp_path):
    fifo = _fifo(tmp_path / "record.json")
    out = tmp_path / "x.html"
    assert cli.main(["view", "--record", str(fifo),
                     "--out", str(out)]) == 2
    assert not out.exists()


@needs_fifo
def test_cli_replay_rejects_fifo_record_with_exit_2(tmp_path):
    fifo = _fifo(tmp_path / "record.json")
    assert cli.main([
        "replay", "--record", str(fifo), "--spec", str(SPEC_PATH),
        "--candidate", str(APP / "samples" / "failing"),
    ]) == 2


@needs_fifo
def test_cli_run_rejects_fifo_spec_with_exit_2(tmp_path):
    fifo = _fifo(tmp_path / "spec.json")
    out = tmp_path / "out.json"
    assert cli.main(["run", "--spec", str(fifo), "--candidate",
                     str(PASSING), "--mode", "judge",
                     "--out", str(out)]) == 2
    assert not out.exists()


def test_cli_verify_rejects_directory_record_with_exit_2(tmp_path, capsys):
    assert cli.main(["verify", "--record", str(tmp_path)]) == 2
    assert "not a regular file" in capsys.readouterr().err


def test_cli_verify_still_accepts_committed_record():
    assert cli.main(["verify", "--record", str(PASSING_RECORD)]) == 0
