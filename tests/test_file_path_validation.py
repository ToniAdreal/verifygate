"""File-path validation (v1.46.0).

The spec/record loaders converted their path argument unchecked:
``load_spec`` ran ``Path(path)`` outside its error handling, so a
non-path value escaped as a bare ``TypeError`` instead of the
``SpecError`` every other malformed spec produces, and
``load_json_file`` handed its path straight to ``open()`` — which
treats an ``int`` as a raw file descriptor, reading (and closing)
a descriptor the caller never offered as a file. Both loaders now
validate through ``jsonio.validate_file_path`` first: the
malformed shapes are the loaders' own clean error types
(``JsonFileError`` / ``SpecError``, both ``ValueError`` — the
CLI's exit 2), before any file is opened.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from verifygate.jsonio import JsonFileError, load_json_file, validate_file_path
from verifygate.spec import SpecError, load_spec

SPEC_JSON = (
    '{"name": "t", "brief": "b", "checks": '
    '[{"name": "c", "command": "true", "timeout_s": 5}]}'
)

BAD_PATHS = [
    None,
    123,
    True,
    1.5,
    ["spec.json"],
    {"path": "spec.json"},
    b"spec.json",
    "",
]


class _StrPathLike:
    def __init__(self, value):
        self._value = value

    def __fspath__(self):
        return self._value


class _BytesPathLike:
    def __fspath__(self):
        return b"spec.json"


class _BrokenPathLike:
    def __fspath__(self):
        raise TypeError("no fspath")


# -- the helper -----------------------------------------------------------


@pytest.mark.parametrize("bad", BAD_PATHS)
def test_validate_file_path_rejects_non_paths(bad):
    with pytest.raises(JsonFileError):
        validate_file_path(bad)


@pytest.mark.parametrize("bad", [_BytesPathLike(), _BrokenPathLike()])
def test_validate_file_path_rejects_bad_pathlikes(bad):
    with pytest.raises(JsonFileError):
        validate_file_path(bad)


def test_validate_file_path_error_names_type_not_value():
    with pytest.raises(JsonFileError) as excinfo:
        validate_file_path(None, "spec path")
    assert "spec path" in str(excinfo.value)
    assert "NoneType" in str(excinfo.value)


def test_validate_file_path_accepts_str_and_path(tmp_path):
    target = tmp_path / "spec.json"
    assert validate_file_path(str(target)) == target
    assert validate_file_path(target) == target
    assert validate_file_path(_StrPathLike(str(target))) == target


# -- load_json_file ---------------------------------------------------------


@pytest.mark.parametrize("bad", BAD_PATHS)
def test_load_json_file_rejects_non_paths(bad):
    with pytest.raises(JsonFileError):
        load_json_file(bad, max_bytes=1024)


def test_load_json_file_int_is_not_treated_as_a_descriptor(tmp_path):
    # An int path must be rejected before open(): open() would
    # otherwise read from — and close — that file descriptor.
    read_fd, write_fd = os.pipe()
    try:
        with pytest.raises(JsonFileError):
            load_json_file(read_fd, max_bytes=1024)
        # The descriptor is untouched: still open, still usable.
        os.write(write_fd, b"x")
        assert os.read(read_fd, 1) == b"x"
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_load_json_file_still_loads_valid_paths(tmp_path):
    target = tmp_path / "record.json"
    target.write_text('{"tool": "verifygate"}', encoding="utf-8")
    assert load_json_file(target, max_bytes=1024) == {"tool": "verifygate"}
    assert load_json_file(str(target), max_bytes=1024) == {
        "tool": "verifygate"}


def test_load_json_file_missing_file_still_oserror(tmp_path):
    with pytest.raises(OSError):
        load_json_file(tmp_path / "nope.json", max_bytes=1024)


# -- load_spec ---------------------------------------------------------------


@pytest.mark.parametrize("bad", BAD_PATHS)
def test_load_spec_rejects_non_paths_as_spec_error(bad):
    # Before v1.46.0 every one of these escaped as a TypeError
    # traceback from Path(...), not the SpecError the CLI maps to
    # its one-line exit 2.
    with pytest.raises(SpecError):
        load_spec(bad)


def test_load_spec_error_line_stays_bounded():
    with pytest.raises(SpecError) as excinfo:
        load_spec("x" * 500_000)
    assert len(str(excinfo.value)) < 300


def test_load_spec_still_loads_valid_paths(tmp_path):
    target = tmp_path / "spec.json"
    target.write_text(SPEC_JSON, encoding="utf-8")
    assert load_spec(target).name == "t"
    assert load_spec(str(target)).name == "t"


def test_load_spec_missing_file_still_spec_error(tmp_path):
    with pytest.raises(SpecError):
        load_spec(tmp_path / "nope.json")
