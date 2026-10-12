"""Record-writer validation (v1.59.0).

``runner.write_record`` — the one function that materialises a
sealed record to disk, and the CLI ``run`` handler's only write
path — converted its ``out_path`` with a bare ``Path(...)`` and
took its ``record`` on trust. The loaders got path validation in
v1.46.0 (``jsonio.validate_file_path``); the writer was left: a
non-path ``out_path`` crashed with a raw ``TypeError``, an empty
string surfaced as an ``IsADirectoryError`` (for the current
directory) instead of the one usage error, a non-object record
was silently written as a JSON file no VerifyGate consumer
accepts — overwriting whatever already sat at the destination —
and a non-serializable record escaped as a raw ``TypeError``
from ``json.dumps``, past the CLI handler's ``OSError``-only
write handling. Both arguments are now judged before anything
is written: the path through ``jsonio.validate_file_path``, the
record as a dict that must serialise (serialised first, so a
rejection leaves a pre-existing file byte-for-byte unchanged).
Genuine filesystem faults stay ``OSError``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verifygate.cli import main
from verifygate.evidence import verify_chain
from verifygate.jsonio import JsonFileError, load_json_file
from verifygate.mock_llm import MockLLMAdapter
from verifygate.runner import run_verification, write_record
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup

APP = Path(__file__).resolve().parent.parent
SPEC = APP / "samples" / "spec.json"
PASSING = APP / "samples" / "passing"

BAD_PATHS = [
    None,
    123,
    True,
    1.5,
    ["out.json"],
    {"path": "out.json"},
    b"out.json",
    "",
]

BAD_RECORDS = [None, "hello", [1, 2], 123, True, b"bytes"]


class _StrPathLike:
    def __init__(self, value):
        self._value = value

    def __fspath__(self):
        return self._value


class _BytesPathLike:
    def __fspath__(self):
        return b"out.json"


class _BrokenPathLike:
    def __fspath__(self):
        raise TypeError("no fspath")


# --- the destination path -------------------------------------------------


@pytest.mark.parametrize("bad", BAD_PATHS)
def test_bad_out_path_is_value_error_and_writes_nothing(tmp_path, bad):
    with pytest.raises(ValueError):
        write_record({"tool": "verifygate"}, bad)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("bad", BAD_PATHS)
def test_bad_out_path_is_json_file_error(tmp_path, bad):
    # The writer raises the loaders' own error type (v1.46.0), so
    # a malformed destination is the same judgement on both sides.
    with pytest.raises(JsonFileError):
        write_record({"tool": "verifygate"}, bad)


def test_empty_out_path_is_not_the_current_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        write_record({"tool": "verifygate"}, "")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("bad", [_BytesPathLike(), _BrokenPathLike(),
                                 _StrPathLike(""), _StrPathLike(b"x")])
def test_bad_path_like_is_value_error(tmp_path, bad):
    with pytest.raises(ValueError):
        write_record({"tool": "verifygate"}, bad)
    assert list(tmp_path.iterdir()) == []


def test_str_path_like_is_accepted(tmp_path):
    target = tmp_path / "out.json"
    returned = write_record({"a": 1}, _StrPathLike(str(target)))
    assert returned == target
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}


# --- the record itself ----------------------------------------------------


@pytest.mark.parametrize("bad", BAD_RECORDS)
def test_non_dict_record_is_value_error_and_writes_nothing(tmp_path, bad):
    target = tmp_path / "out.json"
    with pytest.raises(ValueError):
        write_record(bad, target)
    assert not target.exists()


def test_non_dict_record_leaves_existing_file_unchanged(tmp_path):
    target = tmp_path / "out.json"
    target.write_text("ORIGINAL\n", encoding="utf-8")
    with pytest.raises(ValueError):
        write_record("hello", target)
    assert target.read_text(encoding="utf-8") == "ORIGINAL\n"


def test_non_serializable_record_is_value_error_not_type_error(tmp_path):
    target = tmp_path / "out.json"
    with pytest.raises(ValueError):
        write_record({"x": {1, 2}}, target)
    assert not target.exists()


def test_circular_record_is_value_error_and_writes_nothing(tmp_path):
    record: dict = {}
    record["self"] = record
    target = tmp_path / "out.json"
    with pytest.raises(ValueError):
        write_record(record, target)
    assert not target.exists()


def test_non_serializable_record_leaves_existing_file_unchanged(tmp_path):
    target = tmp_path / "out.json"
    target.write_text("ORIGINAL\n", encoding="utf-8")
    with pytest.raises(ValueError):
        write_record({"x": object()}, target)
    assert target.read_text(encoding="utf-8") == "ORIGINAL\n"


# --- genuine writes are unchanged ------------------------------------------


def test_valid_write_round_trips_through_the_loader(tmp_path):
    target = tmp_path / "out.json"
    returned = write_record({"tool": "verifygate", "n": 1}, target)
    assert returned == target
    loaded = load_json_file(target, max_bytes=1 << 20, kind="record")
    assert loaded == {"tool": "verifygate", "n": 1}


def test_genuine_record_written_and_verifies(tmp_path):
    record = run_verification(
        spec=load_spec(SPEC),
        candidate_dir=PASSING,
        mode="judge",
        llm=MockLLMAdapter(),
        references=MockReferenceLookup(),
        spec_label="samples/spec.json",
        candidate_label="samples/passing",
    )
    target = write_record(record, tmp_path / "evidence.json")
    loaded = load_json_file(target, max_bytes=1 << 20, kind="record")
    assert loaded == record
    assert verify_chain(loaded) is True


def test_missing_parent_is_os_error_not_value_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        write_record({"a": 1}, tmp_path / "no-such-dir" / "out.json")


def test_directory_destination_is_os_error_not_value_error(tmp_path):
    with pytest.raises(IsADirectoryError):
        write_record({"a": 1}, tmp_path)


# --- the CLI contract -------------------------------------------------------


def test_cli_run_with_directory_out_is_output_error_exit_2(tmp_path, capsys):
    rc = main(["run", "--spec", str(SPEC), "--candidate", str(PASSING),
               "--mode", "judge", "--out", str(tmp_path)])
    assert rc == 2
    assert "output error" in capsys.readouterr().err


def test_cli_run_still_writes_a_genuine_record(tmp_path):
    out = tmp_path / "evidence.json"
    rc = main(["run", "--spec", str(SPEC), "--candidate", str(PASSING),
               "--mode", "judge", "--out", str(out)])
    assert rc == 0
    loaded = load_json_file(out, max_bytes=1 << 20, kind="record")
    assert loaded["verdict"] == "PASS"
    assert verify_chain(loaded) is True
