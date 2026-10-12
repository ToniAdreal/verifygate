"""Strict JSON decoding in the bounded loader (v1.76.0).

Before this, two decoder shapes escaped load_json_file's
contract that every malformed input is one JsonFileError:

* an integer literal longer than the interpreter's conversion
  limit made json.loads raise a plain ValueError (not a
  JSONDecodeError) — on the spec path, whose loader re-raises
  only JsonFileError / OSError, the CLI died with a traceback
  and exit 1;
* the non-standard constants NaN / Infinity / -Infinity (not
  JSON at all) loaded verbatim, and a numeric literal that
  overflows float range (1e999) decoded to inf without passing
  through parse_constant.

Genuine documents — standard JSON, small ints, finite floats —
load exactly as before.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from verifygate.jsonio import (
    MAX_RECORD_FILE_BYTES,
    MAX_SPEC_FILE_BYTES,
    JsonFileError,
    load_json_file,
)
from verifygate.spec import SpecError, load_spec

ROOT = Path(__file__).resolve().parents[1]
HUGE_INT = "9" * 6000


def _write(tmp_path: Path, text: str, name: str = "doc.json") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _load(path: Path, kind: str = "record"):
    cap = MAX_SPEC_FILE_BYTES if kind == "spec" else MAX_RECORD_FILE_BYTES
    return load_json_file(path, max_bytes=cap, kind=kind)


# --- Oversize integer literals -------------------------------------


def test_huge_int_is_a_json_file_error(tmp_path):
    path = _write(tmp_path, '{"a": ' + HUGE_INT + "}")
    with pytest.raises(JsonFileError, match="integer literal"):
        _load(path)


def test_huge_int_nested_in_a_record_is_a_json_file_error(tmp_path):
    path = _write(tmp_path, '{"entries": [{"exit_code": ' + HUGE_INT + "}]}")
    with pytest.raises(JsonFileError):
        _load(path)


def test_huge_int_error_never_echoes_the_literal(tmp_path):
    path = _write(tmp_path, '{"a": ' + HUGE_INT + "}")
    with pytest.raises(JsonFileError) as excinfo:
        _load(path)
    assert HUGE_INT not in str(excinfo.value)
    assert len(str(excinfo.value)) < 300


def test_huge_int_spec_is_a_spec_error(tmp_path):
    path = _write(
        tmp_path,
        '{"name": "x", "brief": "b", "checks": [{"name": "c",'
        ' "command": "true", "timeout_s": ' + HUGE_INT + "}]}",
        "spec.json",
    )
    with pytest.raises(SpecError, match="integer literal"):
        load_spec(path)


def test_legal_large_int_still_loads(tmp_path):
    path = _write(tmp_path, '{"a": ' + "1" * 100 + "}")
    assert _load(path) == {"a": int("1" * 100)}


# --- Non-standard constants and non-finite numbers -------------------


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonstandard_constants_are_rejected(tmp_path, constant):
    path = _write(tmp_path, '{"verdict": ' + constant + "}")
    with pytest.raises(JsonFileError, match="non-standard constant"):
        _load(path)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonstandard_constant_nested_is_rejected(tmp_path, constant):
    path = _write(tmp_path, '{"a": [1, {"b": ' + constant + "}]}")
    with pytest.raises(JsonFileError):
        _load(path)


@pytest.mark.parametrize("literal", ["1e999", "-1e999", "1E+999"])
def test_overflowing_numeric_literal_is_rejected(tmp_path, literal):
    path = _write(tmp_path, '{"a": ' + literal + "}")
    with pytest.raises(JsonFileError, match="out of range"):
        _load(path)


def test_finite_floats_still_load(tmp_path):
    path = _write(tmp_path, '{"a": 1.5, "b": [2.5e3, -0.25, 0.0]}')
    assert _load(path) == {"a": 1.5, "b": [2500.0, -0.25, 0.0]}


def test_nan_in_a_spec_is_a_spec_error(tmp_path):
    path = _write(
        tmp_path,
        '{"name": "x", "brief": NaN, "checks": []}',
        "spec.json",
    )
    with pytest.raises(SpecError, match="non-standard constant"):
        load_spec(path)


# --- Genuine documents are unaffected --------------------------------


def test_committed_documents_still_load():
    for spec in (ROOT / "samples" / "spec.json",
                 ROOT / "samples-js" / "spec.json",
                 ROOT / "dogfood_spec.json"):
        assert load_json_file(spec, max_bytes=MAX_SPEC_FILE_BYTES,
                              kind="spec")
    for record in (ROOT / "samples" / "evidence_passing.json",
                   ROOT / "samples" / "evidence_real_repair.json",
                   ROOT / "samples" / "evidence_real_repair_v3.json",
                   ROOT / "samples-js" / "evidence_real_repair_v3.json",
                   ROOT / "evidence_self_check.json"):
        assert load_json_file(record, max_bytes=MAX_RECORD_FILE_BYTES,
                              kind="record")


def test_malformed_json_is_still_a_json_file_error(tmp_path):
    path = _write(tmp_path, '{"a": }')
    with pytest.raises(JsonFileError):
        _load(path)


# --- CLI contract -----------------------------------------------------


def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "verifygate", *args],
        capture_output=True, text=True, cwd=ROOT, timeout=120)


def test_cli_run_on_huge_int_spec_exits_2_without_traceback(tmp_path):
    spec = _write(
        tmp_path,
        '{"name": "x", "brief": "b", "checks": [{"name": "c",'
        ' "command": "true", "timeout_s": ' + HUGE_INT + "}]}",
        "spec.json",
    )
    out = tmp_path / "record.json"
    proc = _cli("run", "--spec", str(spec), "--candidate",
                str(ROOT / "samples" / "passing"), "--mode", "judge",
                "--out", str(out))
    assert proc.returncode == 2
    assert "spec error" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert not out.exists()


def test_cli_verify_on_huge_int_record_exits_2_without_traceback(tmp_path):
    record = _write(tmp_path, '{"a": ' + HUGE_INT + "}")
    proc = _cli("verify", "--record", str(record))
    assert proc.returncode == 2
    assert "record error" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_cli_verify_on_nan_record_exits_2_without_traceback(tmp_path):
    record = _write(tmp_path, '{"verdict": NaN}')
    proc = _cli("verify", "--record", str(record))
    assert proc.returncode == 2
    assert "record error" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_cli_view_on_overflow_record_exits_2_and_writes_nothing(tmp_path):
    record = _write(tmp_path, '{"a": 1e999}')
    out = tmp_path / "view.html"
    proc = _cli("view", "--record", str(record), "--out", str(out))
    assert proc.returncode == 2
    assert "Traceback" not in proc.stderr
    assert not out.exists()
