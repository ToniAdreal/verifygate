"""Replay-command update validation (v1.54.0).

``evidence.update_replay_command`` is the one sanctioned post-seal
edit: the repair loop rewrites a patched attempt's ``replay`` field
and the record is re-sealed. Before v1.54.0 it took both arguments
on trust:

* a ``replay_command`` that was not a string, was empty, carried a
  shell fragment, named the wrong mode, or ran to 500,000
  characters was written and (under chain v3) re-sealed — a record
  that fails its own verification the moment it is written, because
  ``_metadata_ok`` holds the field to the two emitted templates,
  the header's mode, and ``MAX_REPLAY_CHARS``;
* a non-object ``record`` raised ``TypeError`` on assignment, an
  empty ``entries`` list an ``IndexError``, a non-list one a
  ``TypeError``, a string ``chain_version`` a ``TypeError`` from
  the ``>= 3`` comparison, and a v3-shaped record missing a
  sealed-payload field a ``KeyError`` — each after the ``replay``
  field had already been overwritten.

Both arguments are now validated before anything is mutated: one
``ValueError``, and the record is left byte-for-byte unchanged.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from verifygate.evidence import (
    MAX_REPLAY_CHARS,
    build_record,
    update_replay_command,
    validate_replay_command,
    verify_chain,
)
from verifygate.mock_llm import MockLLMAdapter
from verifygate.repair import run_repair
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup
from verifygate.verify import document_ok, verify_document

APP = Path(__file__).resolve().parents[1]

RUN_CMD = ("python3 -m verifygate run --spec samples/spec.json "
           "--candidate samples/failing --mode judge")
REPLAY_CMD = ("python3 -m verifygate replay "
              "--record <repair-report.json> --spec samples/spec.json "
              "--candidate samples/failing --mode judge  "
              "# attempt 1: patched copy, reconstructed from the "
              "report's recorded patch")
QUOTED_RUN_CMD = ("python3 -m verifygate run --spec 'my spec.json' "
                  "--candidate 'my cand' --mode real")


def _record(mode: str = "judge") -> dict:
    return build_record(
        spec_name="stringtools-slugify-fix",
        spec_hash="a" * 64,
        candidate_tree_hash="b" * 64,
        mode=mode,
        check_results=[{
            "name": "unit_tests", "command": "python3 -m pytest -q",
            "exit_code": 0, "duration_ms": 12,
            "stdout_tail": "", "stderr_tail": "",
            "timed_out": False, "verdict": "PASS",
        }],
        verdict="PASS",
        diagnosis=None,
        replay_command=(
            "python3 -m verifygate run --spec samples/spec.json "
            f"--candidate samples/passing --mode {mode}"),
    )


# -- validate_replay_command: genuine commands -----------------------------


def test_helper_accepts_both_emitted_templates():
    assert validate_replay_command(RUN_CMD) == RUN_CMD
    assert validate_replay_command(REPLAY_CMD) == REPLAY_CMD


def test_helper_accepts_quoted_labels():
    assert validate_replay_command(QUOTED_RUN_CMD) == QUOTED_RUN_CMD


def test_helper_accepts_matching_header_mode():
    assert validate_replay_command(RUN_CMD, "judge") == RUN_CMD
    assert validate_replay_command(QUOTED_RUN_CMD, "real") == QUOTED_RUN_CMD


# -- validate_replay_command: malformed commands ---------------------------

BAD_COMMAND_TYPES = [123, None, True, 2.5, ["x"], {"a": 1}, b"bytes"]


@pytest.mark.parametrize("bad", BAD_COMMAND_TYPES)
def test_helper_rejects_non_string_commands(bad):
    with pytest.raises(ValueError, match="non-empty string"):
        validate_replay_command(bad)


def test_helper_rejects_empty_command():
    with pytest.raises(ValueError, match="non-empty string"):
        validate_replay_command("")


def test_helper_rejects_over_long_command():
    long_cmd = RUN_CMD + "x" * MAX_REPLAY_CHARS
    with pytest.raises(ValueError, match="at most"):
        validate_replay_command(long_cmd)


@pytest.mark.parametrize("bad", [
    "python3 -m verifygate run --spec s --candidate c --mode bogus",
    "python3 -m verifygate run --spec s --candidate c --mode judge; echo pwned",
    "python3 -m verifygate run --spec s --candidate c --mode judge\nrm -rf /",
    RUN_CMD + "\n",  # the templates' $ anchor must not excuse this
    RUN_CMD + " ",
    "verifygate run --spec s --candidate c --mode judge",
    "python3 -m verifygate replay --record r --spec s --candidate c "
    "--mode judge  # attempt 0: patched copy, reconstructed from the "
    "report's recorded patch",
    "echo hello",
])
def test_helper_rejects_non_template_commands(bad):
    with pytest.raises(ValueError, match="templates"):
        validate_replay_command(bad)


@pytest.mark.parametrize("cmd,mode", [
    (RUN_CMD, "real"),
    (REPLAY_CMD, "real"),
    (QUOTED_RUN_CMD, "judge"),
])
def test_helper_rejects_mode_mismatch(cmd, mode):
    with pytest.raises(ValueError, match="does not match"):
        validate_replay_command(cmd, mode)


# -- update_replay_command: the genuine edit --------------------------------


def test_update_genuine_v3_record_reseals_and_verifies():
    record = _record()
    old_hash, old_sig = record["record_hash"], record["signature"]
    update_replay_command(record, REPLAY_CMD)
    assert record["replay"] == REPLAY_CMD
    assert record["record_hash"] != old_hash
    assert record["signature"] != old_sig
    assert verify_chain(record) is True


def test_update_genuine_run_template_verifies():
    record = _record()
    update_replay_command(record, RUN_CMD)
    assert record["replay"] == RUN_CMD
    assert verify_chain(record) is True


def test_update_legacy_record_replaces_without_reseal():
    # A v1-shaped record (no chain_version): the field was unsealed
    # metadata, so only the field changes — no hash/signature work.
    record = _record()
    del record["chain_version"]
    old_hash, old_sig = record["record_hash"], record["signature"]
    update_replay_command(record, RUN_CMD)
    assert record["replay"] == RUN_CMD
    assert record["record_hash"] == old_hash
    assert record["signature"] == old_sig


# -- update_replay_command: malformed commands leave the record alone ------


@pytest.mark.parametrize("bad", BAD_COMMAND_TYPES + [
    "",
    RUN_CMD + "x" * MAX_REPLAY_CHARS,
    "python3 -m verifygate run --spec s --candidate c --mode real",
    RUN_CMD + "; echo pwned",
    RUN_CMD + "\n",
])
def test_update_rejects_bad_command_and_leaves_record_unchanged(bad):
    record = _record()
    before = copy.deepcopy(record)
    with pytest.raises(ValueError):
        update_replay_command(record, bad)
    assert record == before
    assert verify_chain(record) is True


# -- update_replay_command: malformed records -------------------------------


@pytest.mark.parametrize("bad", [None, [], "record", 123, 2.5, True])
def test_update_rejects_non_object_record(bad):
    with pytest.raises(ValueError, match="record object"):
        update_replay_command(bad, RUN_CMD)


def _broken(**changes) -> dict:
    record = _record()
    record.update(changes)
    return record


MALFORMED_RECORDS = [
    {},
    {"entries": []},
    {"entries": "nope"},
    {"entries": [{}]},  # header without a mode
    {"entries": [{"mode": "bogus", "hash": "a" * 64}]},
    _broken(chain_version="3"),
    _broken(chain_version=99),
    _broken(chain_version=None),
    _broken(chain_version=True),
    _broken(chain_version=2.0),
    _broken(entries=[]),
    _broken(entries="nope"),
    _broken(entries=[{"kind": "header"}]),
]


@pytest.mark.parametrize("bad", MALFORMED_RECORDS[:-1])
def test_update_rejects_malformed_records(bad):
    before = copy.deepcopy(bad)
    with pytest.raises(ValueError):
        update_replay_command(bad, RUN_CMD)
    assert bad == before


def test_update_rejects_record_whose_header_entry_lacks_a_mode():
    bad = MALFORMED_RECORDS[-1]
    before = copy.deepcopy(bad)
    with pytest.raises(ValueError, match="mode"):
        update_replay_command(bad, RUN_CMD)
    assert bad == before


@pytest.mark.parametrize("field", [
    "verdict", "version", "created_at", "signing_key_is_default_dev_key",
])
def test_update_rejects_v3_record_missing_sealed_payload_field(field):
    record = _record()
    del record[field]
    before = copy.deepcopy(record)
    with pytest.raises(ValueError, match="sealed-payload"):
        update_replay_command(record, REPLAY_CMD)
    assert record == before


def test_update_rejects_v3_record_whose_last_entry_has_no_hash():
    record = _record()
    del record["entries"][-1]["hash"]
    before = copy.deepcopy(record)
    with pytest.raises(ValueError, match="hash"):
        update_replay_command(record, REPLAY_CMD)
    assert record == before


def test_update_rejects_v3_record_whose_last_entry_is_not_an_object():
    record = _record()
    record["entries"][-1] = "check"
    with pytest.raises(ValueError, match="entries"):
        update_replay_command(record, REPLAY_CMD)


# -- the repair loop's own use still works end to end -----------------------


def test_repair_loop_patched_attempt_replay_validates_and_verifies():
    spec = load_spec(APP / "samples" / "spec.json")
    report = run_repair(
        spec=spec,
        candidate_dir=APP / "samples" / "failing",
        mode="judge",
        llm=MockLLMAdapter(),
        references=MockReferenceLookup(),
        spec_label="samples/spec.json",
        candidate_label="samples/failing",
        max_rounds=3,
    )
    assert report["final_verdict"] == "PASS"
    patched = report["attempts"][-1]["evidence"]
    # The rewritten field is exactly what the new validator accepts,
    # in the header's mode, and the whole report still verifies.
    assert validate_replay_command(
        patched["replay"], patched["entries"][0]["mode"]) == patched["replay"]
    assert document_ok(verify_document(
        json.loads(json.dumps(report)))) is True
