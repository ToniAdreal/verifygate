"""Duplicate JSON object keys rejected by the bounded loader (v1.77.0).

Before this, json.loads silently kept the *last* value for a
repeated object key. A record whose text states
``"verdict": "FAIL", "verdict": "PASS"`` verified INTACT:
this loader read PASS (last wins), while a first-wins parser
— or a human reading the raw text top-down — reads FAIL. The
same artifact was judged two different ways depending on the
consumer, on the very field the record exists to seal. A
spec could likewise hide a first ``command`` behind a second.

The loader now rejects a repeated key at any nesting level —
including keys that only collide after \\uXXXX decoding — as
the same JsonFileError as malformed JSON. Documents
VerifyGate writes are built from dicts and can never contain
a duplicate, so no genuine document is affected; the same key
in *different* objects is not a duplicate.
"""

from __future__ import annotations

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


def _write(tmp_path: Path, text: str, name: str = "doc.json") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _load(path: Path, kind: str = "record"):
    cap = MAX_SPEC_FILE_BYTES if kind == "spec" else MAX_RECORD_FILE_BYTES
    return load_json_file(path, max_bytes=cap, kind=kind)


# --- The rejection ---------------------------------------------------


def test_duplicate_top_level_key_is_rejected(tmp_path):
    path = _write(tmp_path, '{"verdict": "FAIL", "verdict": "PASS"}')
    with pytest.raises(JsonFileError, match="duplicate object key"):
        _load(path)


def test_duplicate_nested_key_is_rejected(tmp_path):
    path = _write(tmp_path, '{"entries": {"kind": "check", "kind": "x"}}')
    with pytest.raises(JsonFileError, match="duplicate object key"):
        _load(path)


def test_duplicate_key_inside_an_array_object_is_rejected(tmp_path):
    path = _write(tmp_path, '{"a": [{"exit_code": 1, "exit_code": 0}]}')
    with pytest.raises(JsonFileError, match="duplicate object key"):
        _load(path)


def test_duplicate_key_via_unicode_escape_is_rejected(tmp_path):
    # "\u0076erdict" decodes to "verdict": the collision exists
    # only after decoding, and the hook sees decoded keys.
    path = _write(tmp_path, r'{"verdict": "FAIL", "\u0076erdict": "PASS"}')
    with pytest.raises(JsonFileError, match="duplicate object key"):
        _load(path)


def test_duplicate_error_never_echoes_the_key(tmp_path):
    key = "k" * 500
    path = _write(tmp_path, '{"' + key + '": 1, "' + key + '": 2}')
    with pytest.raises(JsonFileError) as excinfo:
        _load(path)
    assert key not in str(excinfo.value)


# --- What is not a duplicate -----------------------------------------


def test_same_key_in_sibling_objects_loads(tmp_path):
    path = _write(tmp_path, '{"a": [{"x": 1}, {"x": 2}], "x": 3}')
    assert _load(path) == {"a": [{"x": 1}, {"x": 2}], "x": 3}


def test_keys_differing_only_by_case_load(tmp_path):
    path = _write(tmp_path, '{"Verdict": "FAIL", "verdict": "PASS"}')
    assert _load(path) == {"Verdict": "FAIL", "verdict": "PASS"}


def test_duplicate_spec_key_is_a_spec_error(tmp_path):
    path = _write(
        tmp_path,
        '{"name": "x", "name": "y", "brief": "b", "checks": []}',
        "spec.json",
    )
    with pytest.raises(SpecError, match="duplicate object key"):
        load_spec(path)


def test_duplicate_check_key_is_a_spec_error(tmp_path):
    path = _write(
        tmp_path,
        '{"name": "x", "brief": "b", "checks": [{"name": "c",'
        ' "command": "false", "command": "true", "timeout_s": 5}]}',
        "spec.json",
    )
    with pytest.raises(SpecError, match="duplicate object key"):
        load_spec(path)


def test_duplicate_key_in_a_repair_report_is_rejected(tmp_path):
    text = (ROOT / "samples" / "evidence_real_repair.json").read_text(
        encoding="utf-8")
    dup = text.replace('"final_verdict": "PASS"',
                       '"final_verdict": "FAIL", "final_verdict": "PASS"', 1)
    assert dup != text
    path = _write(tmp_path, dup, "report.json")
    with pytest.raises(JsonFileError, match="duplicate object key"):
        _load(path)


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


# --- CLI contract -----------------------------------------------------


def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "verifygate", *args],
        capture_output=True, text=True, cwd=ROOT, timeout=120)


def _dup_verdict_record(tmp_path: Path) -> Path:
    text = (ROOT / "samples" / "evidence_passing.json").read_text(
        encoding="utf-8")
    dup = text.replace('"verdict": "PASS"',
                       '"verdict": "FAIL", "verdict": "PASS"', 1)
    assert dup != text
    return _write(tmp_path, dup, "dup_record.json")


def test_cli_verify_on_duplicate_verdict_record_exits_2(tmp_path):
    record = _dup_verdict_record(tmp_path)
    proc = _cli("verify", "--record", str(record))
    assert proc.returncode == 2
    assert "record error" in proc.stderr
    assert "RECORD INTACT" not in proc.stdout
    assert "Traceback" not in proc.stderr


def test_cli_view_on_duplicate_record_exits_2_and_writes_nothing(tmp_path):
    record = _dup_verdict_record(tmp_path)
    out = tmp_path / "view.html"
    proc = _cli("view", "--record", str(record), "--out", str(out))
    assert proc.returncode == 2
    assert "Traceback" not in proc.stderr
    assert not out.exists()


def test_cli_replay_on_duplicate_record_exits_2(tmp_path):
    record = _dup_verdict_record(tmp_path)
    proc = _cli("replay", "--record", str(record), "--spec",
                str(ROOT / "samples" / "spec.json"), "--candidate",
                str(ROOT / "samples" / "passing"))
    assert proc.returncode == 2
    assert "Traceback" not in proc.stderr


def test_cli_run_on_duplicate_spec_exits_2_and_writes_nothing(tmp_path):
    spec = _write(
        tmp_path,
        '{"name": "x", "name": "y", "brief": "b", "checks":'
        ' [{"name": "c", "command": "true", "timeout_s": 5}]}',
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
