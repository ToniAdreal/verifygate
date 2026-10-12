"""Special-file outputs: a write destination must be a regular file.

Before v1.74.0 every destination VerifyGate writes — an evidence
record (``runner.write_record``), a repair report, a rendered
HTML view — was written with a bare ``Path.write_text``, which
opens whatever path it is given. A FIFO destination blocked the
write indefinitely, after the verdict was already decided (the
mirror of the FIFO *input* closed in v1.73.0); a device
destination such as ``/dev/null`` silently discarded the
document while the CLI reported it written. The writes now go
through ``jsonio.write_text_file``, which stats the destination
before opening it (following symlinks) and rejects any existing
destination that is not a regular file as a ``JsonFileError``
(CLI exit 2). A missing destination is created as a regular
file, as before; a directory keeps raising
``IsADirectoryError``. ``repair --patched-out`` judges an
existing non-directory destination before any work runs.
"""

import os
import socket
from pathlib import Path

import pytest

from verifygate import cli
from verifygate.jsonio import (
    JsonFileError,
    validate_output_path,
    write_text_file,
)
from verifygate.runner import write_record

APP = Path(__file__).resolve().parent.parent
SPEC_PATH = APP / "samples" / "spec.json"
PASSING = APP / "samples" / "passing"
FAILING = APP / "samples" / "failing"
PASSING_RECORD = APP / "samples" / "evidence_passing.json"

needs_fifo = pytest.mark.skipif(
    not hasattr(os, "mkfifo"), reason="platform has no mkfifo")


def _fifo(path: Path) -> Path:
    os.mkfifo(path)
    return path


def _devnull() -> Path:
    devnull = Path("/dev/null")
    if not devnull.exists():
        pytest.skip("/dev/null unavailable")
    return devnull


# -- helper contract ------------------------------------------------------


@needs_fifo
def test_validate_output_path_rejects_fifo(tmp_path):
    fifo = _fifo(tmp_path / "out.json")
    with pytest.raises(JsonFileError, match="not a regular file"):
        validate_output_path(fifo)


def test_validate_output_path_rejects_dev_null():
    with pytest.raises(JsonFileError, match="not a regular file"):
        validate_output_path(_devnull())


def test_write_text_file_rejects_dev_null():
    with pytest.raises(JsonFileError, match="not a regular file"):
        write_text_file(_devnull(), "hello\n")


def test_validate_output_path_rejects_socket(tmp_path):
    sock_path = tmp_path / "out.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(str(sock_path))
        with pytest.raises(JsonFileError, match="not a regular file"):
            validate_output_path(sock_path)
    finally:
        srv.close()


@needs_fifo
def test_validate_output_path_rejects_symlink_to_fifo(tmp_path):
    fifo = _fifo(tmp_path / "real-fifo")
    link = tmp_path / "out.json"
    link.symlink_to(fifo)
    with pytest.raises(JsonFileError, match="not a regular file"):
        validate_output_path(link)


def test_write_text_file_creates_missing_destination(tmp_path):
    target = tmp_path / "new.json"
    assert write_text_file(target, "hello\n") == target
    assert target.read_text(encoding="utf-8") == "hello\n"


def test_write_text_file_overwrites_regular_file(tmp_path):
    target = tmp_path / "out.json"
    target.write_text("OLD\n", encoding="utf-8")
    write_text_file(target, "NEW\n")
    assert target.read_text(encoding="utf-8") == "NEW\n"


def test_write_text_file_writes_through_symlink_to_regular_file(tmp_path):
    target = tmp_path / "real.json"
    target.write_text("OLD\n", encoding="utf-8")
    link = tmp_path / "out.json"
    link.symlink_to(target)
    write_text_file(link, "NEW\n")
    assert target.read_text(encoding="utf-8") == "NEW\n"


def test_write_text_file_creates_target_of_broken_symlink(tmp_path):
    target = tmp_path / "real.json"
    link = tmp_path / "out.json"
    link.symlink_to(target)
    write_text_file(link, "NEW\n")
    assert target.read_text(encoding="utf-8") == "NEW\n"


def test_write_text_file_directory_still_is_a_directory_error(tmp_path):
    with pytest.raises(IsADirectoryError):
        write_text_file(tmp_path, "hello\n")


# -- record writer --------------------------------------------------------


@needs_fifo
def test_write_record_rejects_fifo_without_blocking(tmp_path):
    fifo = _fifo(tmp_path / "record.json")
    with pytest.raises(JsonFileError, match="not a regular file"):
        write_record({"tool": "verifygate"}, fifo)


def test_write_record_rejects_dev_null():
    with pytest.raises(JsonFileError, match="not a regular file"):
        write_record({"tool": "verifygate"}, _devnull())


def test_write_record_still_writes_regular_file(tmp_path):
    target = tmp_path / "record.json"
    assert write_record({"a": 1}, target) == target
    assert '"a": 1' in target.read_text(encoding="utf-8")


# -- CLI surfaces ---------------------------------------------------------


@needs_fifo
def test_cli_run_rejects_fifo_out_with_exit_2(tmp_path, capsys):
    fifo = _fifo(tmp_path / "record.json")
    assert cli.main(["run", "--spec", str(SPEC_PATH), "--candidate",
                     str(PASSING), "--mode", "judge",
                     "--out", str(fifo)]) == 2
    assert "not a regular file" in capsys.readouterr().err


def test_cli_run_rejects_dev_null_out_with_exit_2(tmp_path, capsys):
    assert cli.main(["run", "--spec", str(SPEC_PATH), "--candidate",
                     str(PASSING), "--mode", "judge",
                     "--out", str(_devnull())]) == 2
    assert "not a regular file" in capsys.readouterr().err


@needs_fifo
def test_cli_view_rejects_fifo_out_with_exit_2(tmp_path, capsys):
    fifo = _fifo(tmp_path / "view.html")
    assert cli.main(["view", "--record", str(PASSING_RECORD),
                     "--out", str(fifo)]) == 2
    assert "not a regular file" in capsys.readouterr().err


def test_cli_view_still_renders_to_regular_file(tmp_path):
    out = tmp_path / "view.html"
    assert cli.main(["view", "--record", str(PASSING_RECORD),
                     "--out", str(out)]) == 0
    assert "verifygate" in out.read_text(encoding="utf-8").lower()


@needs_fifo
def test_cli_repair_rejects_fifo_out_with_exit_2(tmp_path, capsys):
    fifo = _fifo(tmp_path / "report.json")
    assert cli.main(["repair", "--spec", str(SPEC_PATH), "--candidate",
                     str(FAILING), "--mode", "judge",
                     "--out", str(fifo)]) == 2
    assert "not a regular file" in capsys.readouterr().err


@needs_fifo
def test_cli_repair_rejects_fifo_patched_out_before_any_work(
        tmp_path, capsys):
    fifo = _fifo(tmp_path / "patched")
    report = tmp_path / "report.json"
    assert cli.main(["repair", "--spec", str(SPEC_PATH), "--candidate",
                     str(FAILING), "--mode", "judge",
                     "--out", str(report),
                     "--patched-out", str(fifo)]) == 2
    err = capsys.readouterr().err
    assert "not a directory" in err
    # The rejection happens before the repair runs: no report.
    assert not report.exists()


def test_cli_repair_rejects_regular_file_patched_out_before_any_work(
        tmp_path, capsys):
    dest = tmp_path / "patched"
    dest.write_text("occupied\n", encoding="utf-8")
    report = tmp_path / "report.json"
    assert cli.main(["repair", "--spec", str(SPEC_PATH), "--candidate",
                     str(FAILING), "--mode", "judge",
                     "--out", str(report),
                     "--patched-out", str(dest)]) == 2
    assert "not a directory" in capsys.readouterr().err
    assert not report.exists()
    assert dest.read_text(encoding="utf-8") == "occupied\n"


def test_cli_repair_still_copies_to_new_patched_out(tmp_path):
    dest = tmp_path / "patched"
    report = tmp_path / "report.json"
    assert cli.main(["repair", "--spec", str(SPEC_PATH), "--candidate",
                     str(FAILING), "--mode", "judge",
                     "--out", str(report),
                     "--patched-out", str(dest)]) == 0
    assert report.exists()
    assert (dest / "stringtools.py").exists()
