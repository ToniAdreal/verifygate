"""JSON input file bounds: spec and record files are size-bounded on load.

Before v1.36.0 every loader read its file in full with no size
bound, while every field *inside* those documents was already
capped. Probes against v1.35.0: a spec file padded to 5 MiB with
trailing spaces was accepted and ran to PASS, and an evidence
record padded to 70 MiB verified INTACT — both were loaded into
memory in full before any check ran, so a hostile multi-gigabyte
file would exhaust the verifier asked to judge it. Loading now
goes through ``jsonio.load_json_file``, which reads at most
``max_bytes + 1`` bytes and rejects an oversize file as a clean
usage error (spec: ``SpecError``; record commands: CLI exit 2).
"""

import json
from pathlib import Path

import pytest

from verifygate import cli
from verifygate.jsonio import (
    MAX_RECORD_FILE_BYTES,
    MAX_SPEC_FILE_BYTES,
    JsonFileError,
    load_json_file,
)
from verifygate.spec import SpecError, load_spec, parse_spec

APP = Path(__file__).resolve().parent.parent
SPEC_PATH = APP / "samples" / "spec.json"
PASSING = APP / "samples" / "passing"
PASSING_RECORD = APP / "samples" / "evidence_passing.json"
REAL_REPAIR = APP / "samples" / "evidence_real_repair.json"


def _sparse(path: Path, size: int, prefix: bytes = b"") -> Path:
    """Create a file of exactly ``size`` bytes (sparse past the prefix)."""
    with open(path, "wb") as fh:
        fh.write(prefix)
        fh.truncate(size)
    assert path.stat().st_size == size
    return path


# -- the ceilings themselves -------------------------------------------


def test_ceilings_are_generous_over_every_committed_input():
    assert MAX_SPEC_FILE_BYTES == 4 * 1024 * 1024
    assert MAX_RECORD_FILE_BYTES == 64 * 1024 * 1024
    spec_files = [
        APP / "samples" / "spec.json",
        APP / "samples-js" / "spec.json",
        APP / "dogfood_spec.json",
    ]
    record_files = [
        *APP.glob("samples*/evidence_*.json"),
        APP / "evidence_self_check.json",
    ]
    assert len(record_files) >= 10
    for path in spec_files:
        assert path.stat().st_size < MAX_SPEC_FILE_BYTES // 100, path
    for path in record_files:
        assert path.stat().st_size < MAX_RECORD_FILE_BYTES // 100, path


def test_spec_at_every_field_ceiling_serialises_under_the_file_cap(tmp_path):
    raw = {
        "name": "n" * 200,
        "brief": "b" * 10_000,
        "checks": [
            {"name": f"c{i}" + "c" * 197, "command": "x" * 4_000,
             "timeout_s": 3600}
            for i in range(100)
        ],
    }
    path = tmp_path / "maxspec.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert path.stat().st_size < MAX_SPEC_FILE_BYTES
    spec = load_spec(path)  # at the field ceilings: accepted
    assert len(spec.checks) == 100


# -- the loader's own contract (exercised with small caps) --------------


def test_loader_accepts_at_cap_and_rejects_one_byte_over(tmp_path):
    path = tmp_path / "doc.json"
    path.write_text('{"a": 1}' + " " * 8, encoding="utf-8")  # 16 bytes
    assert load_json_file(path, max_bytes=16) == {"a": 1}
    with pytest.raises(JsonFileError, match="exceeds the maximum"):
        load_json_file(path, max_bytes=15)


def test_loader_rejects_malformed_and_non_utf8_as_json_file_error(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(JsonFileError):
        load_json_file(bad, max_bytes=1024)
    raw = tmp_path / "raw.json"
    raw.write_bytes(b'{"a": "\xff"}')
    with pytest.raises(JsonFileError, match="not UTF-8"):
        load_json_file(raw, max_bytes=1024)


def test_loader_lets_oserror_propagate_for_missing_files(tmp_path):
    with pytest.raises(OSError):
        load_json_file(tmp_path / "missing.json", max_bytes=1024)


def test_oversize_rejection_names_the_limit(tmp_path):
    path = _sparse(tmp_path / "big.json", 2048)
    with pytest.raises(JsonFileError, match="maximum of 1024 bytes"):
        load_json_file(path, max_bytes=1024, kind="record")


# -- spec loading ---------------------------------------------------------


def test_load_spec_rejects_oversize_file_as_spec_error(tmp_path):
    path = _sparse(tmp_path / "bigspec.json", MAX_SPEC_FILE_BYTES + 1,
                   prefix=SPEC_PATH.read_bytes())
    with pytest.raises(SpecError, match="exceeds the maximum"):
        load_spec(path)


def test_load_spec_accepts_padded_spec_under_the_cap(tmp_path):
    path = tmp_path / "padded.json"
    path.write_text(
        SPEC_PATH.read_text(encoding="utf-8") + " " * (1024 * 1024),
        encoding="utf-8")
    assert load_spec(path).name == parse_spec(
        json.loads(SPEC_PATH.read_text(encoding="utf-8"))).name


def test_cli_run_and_repair_reject_oversize_spec_with_exit_2(tmp_path):
    path = _sparse(tmp_path / "bigspec.json", MAX_SPEC_FILE_BYTES + 1,
                   prefix=SPEC_PATH.read_bytes())
    out = tmp_path / "out.json"
    assert cli.main(["run", "--spec", str(path), "--candidate",
                     str(PASSING), "--mode", "judge",
                     "--out", str(out)]) == 2
    assert not out.exists()
    assert cli.main(["repair", "--spec", str(path), "--candidate",
                     str(PASSING), "--mode", "judge",
                     "--out", str(out)]) == 2
    assert not out.exists()


# -- record loading through the CLI ---------------------------------------


def test_cli_verify_rejects_oversize_record_with_exit_2(tmp_path, capsys):
    path = _sparse(tmp_path / "bigrecord.json", MAX_RECORD_FILE_BYTES + 1,
                   prefix=PASSING_RECORD.read_bytes())
    assert cli.main(["verify", "--record", str(path)]) == 2
    assert "exceeds the maximum" in capsys.readouterr().err


def test_cli_view_rejects_oversize_record_with_exit_2(tmp_path):
    path = _sparse(tmp_path / "bigrecord.json", MAX_RECORD_FILE_BYTES + 1,
                   prefix=PASSING_RECORD.read_bytes())
    assert cli.main(["view", "--record", str(path),
                     "--out", str(tmp_path / "x.html")]) == 2
    assert not (tmp_path / "x.html").exists()


def test_cli_replay_rejects_oversize_record_with_exit_2(tmp_path):
    path = _sparse(tmp_path / "bigrecord.json", MAX_RECORD_FILE_BYTES + 1,
                   prefix=REAL_REPAIR.read_bytes())
    assert cli.main([
        "replay", "--record", str(path), "--spec", str(SPEC_PATH),
        "--candidate", str(APP / "samples" / "failing"),
    ]) == 2


def test_cli_verify_still_accepts_committed_records():
    assert cli.main(["verify", "--record", str(PASSING_RECORD)]) == 0
    assert cli.main(["verify", "--record", str(REAL_REPAIR)]) == 0
