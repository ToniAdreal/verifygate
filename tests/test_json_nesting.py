"""Nesting-depth bound for JSON input files (v1.75.0).

Before v1.75.0 the bounded loader handed its text to
``json.loads`` with no depth judgement: a document nested
~10,000 levels deep — tens of kilobytes, far under the byte
ceilings — crashed ``verify`` / ``view`` / ``replay`` (record)
and ``run`` / ``repair`` (spec) with an unhandled
``RecursionError`` traceback and exit 1, instead of the CLI
contract's one-line record/spec error with exit 2. The loader
now converts a decoder ``RecursionError`` into
``JsonFileError`` and rejects any document nested deeper than
``MAX_JSON_DEPTH``, measured iteratively after decoding.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from verifygate.jsonio import (
    MAX_JSON_DEPTH,
    MAX_RECORD_FILE_BYTES,
    MAX_SPEC_FILE_BYTES,
    JsonFileError,
    _json_depth,
    load_json_file,
)
from verifygate.spec import SpecError, load_spec

ROOT = Path(__file__).resolve().parent.parent


def _nested_list(depth: int):
    doc: list = []
    current = doc
    for _ in range(depth - 1):
        child: list = []
        current.append(child)
        current = child
    return doc


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "verifygate", *args],
        cwd=ROOT, capture_output=True, text=True, timeout=120)


# -- depth measurement ----------------------------------------------------


def test_depth_helper_is_iterative_and_exact():
    assert _json_depth(42) == 0
    assert _json_depth("text") == 0
    assert _json_depth([]) == 1
    assert _json_depth({}) == 1
    assert _json_depth({"a": [1, {"b": []}]}) == 4
    assert _json_depth(_nested_list(MAX_JSON_DEPTH)) == MAX_JSON_DEPTH


def test_genuine_documents_nest_far_below_the_ceiling():
    assert MAX_JSON_DEPTH == 100
    for rel in (
        "samples/evidence_real_repair.json",
        "samples/evidence_real_repair_v3.json",
        "samples-js/evidence_real_repair_v3.json",
        "evidence_self_check.json",
    ):
        data = json.loads((ROOT / rel).read_text(encoding="utf-8"))
        assert _json_depth(data) <= 12, rel


# -- loader ----------------------------------------------------------------


def test_at_ceiling_loads_and_above_ceiling_is_rejected(tmp_path):
    at = _write(tmp_path, "at.json", json.dumps(_nested_list(MAX_JSON_DEPTH)))
    assert load_json_file(at, max_bytes=MAX_RECORD_FILE_BYTES) is not None
    over = _write(
        tmp_path, "over.json", json.dumps(_nested_list(MAX_JSON_DEPTH + 1)))
    with pytest.raises(JsonFileError, match="nesting depth"):
        load_json_file(over, max_bytes=MAX_RECORD_FILE_BYTES)


def test_extreme_nesting_is_a_json_file_error_not_recursion(tmp_path):
    path = _write(tmp_path, "deep.json", "[" * 200_000 + "]" * 200_000)
    with pytest.raises(JsonFileError):
        load_json_file(path, max_bytes=MAX_RECORD_FILE_BYTES, kind="record")


def test_deeply_nested_field_inside_a_record_is_rejected(tmp_path):
    record = json.loads(
        (ROOT / "samples/evidence_passing.json").read_text(encoding="utf-8"))
    record["entries"][1]["nested"] = _nested_list(500)
    path = _write(tmp_path, "deepfield.json", json.dumps(record))
    with pytest.raises(JsonFileError, match="nesting depth"):
        load_json_file(path, max_bytes=MAX_RECORD_FILE_BYTES, kind="record")


def test_deep_spec_is_a_spec_error(tmp_path):
    spec = _write(tmp_path, "deep_spec.json", "[" * 50_000 + "]" * 50_000)
    with pytest.raises(SpecError):
        load_spec(spec)


# -- CLI -------------------------------------------------------------------


def test_cli_verify_deep_record_exits_2_without_traceback(tmp_path):
    path = _write(tmp_path, "deep.json", "[" * 200_000 + "]" * 200_000)
    proc = _run_cli("verify", "--record", str(path))
    assert proc.returncode == 2
    assert "record error" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_cli_view_and_replay_deep_record_exit_2(tmp_path):
    path = _write(tmp_path, "deep.json", "[" * 200_000 + "]" * 200_000)
    view = _run_cli("view", "--record", str(path))
    assert view.returncode == 2
    assert "Traceback" not in view.stderr
    replay = _run_cli(
        "replay", "--record", str(path),
        "--spec", "samples/spec.json", "--candidate", "samples/passing")
    assert replay.returncode == 2
    assert "Traceback" not in replay.stderr


def test_cli_run_deep_spec_exits_2_and_writes_nothing(tmp_path):
    spec = _write(tmp_path, "deep_spec.json", "[" * 50_000 + "]" * 50_000)
    out = tmp_path / "out.json"
    proc = _run_cli(
        "run", "--spec", str(spec), "--candidate", "samples/passing",
        "--mode", "judge", "--out", str(out))
    assert proc.returncode == 2
    assert "spec error" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert not out.exists()


def test_cli_verify_genuine_record_still_intact():
    proc = _run_cli(
        "verify", "--record", "samples/evidence_real_repair.json",
        "--spec", "samples/spec.json", "--candidate", "samples/failing")
    assert proc.returncode == 0
    assert "RECORD INTACT" in proc.stdout


def test_spec_loader_still_loads_genuine_spec():
    spec = load_spec(ROOT / "samples/spec.json")
    assert spec.name == "stringtools-slugify-fix"
    assert MAX_SPEC_FILE_BYTES >= 1  # loader constant sanity
