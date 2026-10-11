"""Signing-key validation (v1.45.0).

Before this, ``signing_key()`` was a bare ``os.environ.get(...,
DEFAULT_DEV_KEY)``: the dev-key fallback applies only when the
variable is *unset*, so a set-but-empty variable — the classic
CI/deployment mistake — or a whitespace / single-character value
became the HMAC key while the record disclosed
``signing_key_is_default_dev_key: False``, claiming a custom key
had signed it. Every such record verified INTACT.
"""

import pytest

from verifygate import cli
from verifygate.evidence import (
    DEFAULT_DEV_KEY,
    MAX_SIGNING_KEY_CHARS,
    MIN_SIGNING_KEY_CHARS,
    build_record,
    signing_key,
    verify_chain,
)

ROOT_KEYS = ("spec_name", "spec_hash", "candidate_tree_hash", "mode")


def _record():
    return build_record(
        spec_name="sample",
        spec_hash="a" * 64,
        candidate_tree_hash="b" * 64,
        mode="judge",
        check_results=[{
            "name": "c", "command": "true", "exit_code": 0,
            "duration_ms": 1, "stdout_tail": "", "stderr_tail": "",
            "timed_out": False, "verdict": "PASS",
        }],
        verdict="PASS",
        diagnosis=None,
        replay_command=(
            "python3 -m verifygate run --spec s.json "
            "--candidate cand --mode judge"),
    )


BAD_KEYS = [
    "",
    " ",
    "        ",          # whitespace-only at the minimum length
    "x",                  # single character
    "short",              # below the minimum
    "k" * (MIN_SIGNING_KEY_CHARS - 1),
    "k" * (MAX_SIGNING_KEY_CHARS + 1),
    "k" * 500_000,
    "valid-length\nkey",  # control character
    "valid\tlength-key",  # control character
]


def test_bounds_are_the_documented_values():
    assert MIN_SIGNING_KEY_CHARS == 8
    assert MAX_SIGNING_KEY_CHARS == 512
    assert MIN_SIGNING_KEY_CHARS <= len(DEFAULT_DEV_KEY) <= MAX_SIGNING_KEY_CHARS


def test_unset_yields_the_labelled_dev_key(monkeypatch):
    monkeypatch.delenv("VERIFYGATE_SIGNING_KEY", raising=False)
    assert signing_key() == DEFAULT_DEV_KEY
    record = _record()
    assert record["signing_key_is_default_dev_key"] is True
    assert verify_chain(record) is True


def test_explicit_dev_key_still_discloses_dev(monkeypatch):
    monkeypatch.setenv("VERIFYGATE_SIGNING_KEY", DEFAULT_DEV_KEY)
    assert signing_key() == DEFAULT_DEV_KEY
    record = _record()
    assert record["signing_key_is_default_dev_key"] is True
    assert verify_chain(record) is True


@pytest.mark.parametrize("key", BAD_KEYS)
def test_malformed_keys_are_rejected_at_resolution(monkeypatch, key):
    monkeypatch.setenv("VERIFYGATE_SIGNING_KEY", key)
    with pytest.raises(ValueError) as excinfo:
        signing_key()
    # The error must never echo the key itself.
    if key.strip():
        assert key not in str(excinfo.value)


@pytest.mark.parametrize("key", BAD_KEYS)
def test_malformed_keys_seal_no_record(monkeypatch, key):
    monkeypatch.setenv("VERIFYGATE_SIGNING_KEY", key)
    with pytest.raises(ValueError):
        _record()


@pytest.mark.parametrize("key", BAD_KEYS)
def test_malformed_keys_fail_verification_as_usage_error(monkeypatch, key):
    monkeypatch.delenv("VERIFYGATE_SIGNING_KEY", raising=False)
    record = _record()
    monkeypatch.setenv("VERIFYGATE_SIGNING_KEY", key)
    with pytest.raises(ValueError):
        verify_chain(record)


@pytest.mark.parametrize("key", [
    "12345678",                              # exactly the minimum
    "a-custom-key",                          # the pre-existing fixture shape
    "correct horse battery staple",
    "k" * MAX_SIGNING_KEY_CHARS,             # exactly the maximum
])
def test_valid_custom_keys_seal_and_verify(monkeypatch, key):
    monkeypatch.setenv("VERIFYGATE_SIGNING_KEY", key)
    assert signing_key() == key
    record = _record()
    assert record["signing_key_is_default_dev_key"] is False
    assert verify_chain(record) is True


def test_pre_fix_probe_shapes_no_longer_verify(monkeypatch):
    # The exact probe shapes from the v1.45.0 investigation: each
    # sealed a flag-False record that verified INTACT before the fix.
    for key in ("", " ", "x", "short"):
        monkeypatch.setenv("VERIFYGATE_SIGNING_KEY", key)
        with pytest.raises(ValueError):
            _record()


def test_runner_rejects_malformed_key_before_any_check(monkeypatch):
    # The runner resolves the key beside its other validations,
    # before the workspace exists: a counting adapter proves no
    # diagnosis was ever requested (the checks never ran).
    from pathlib import Path
    from verifygate.mock_llm import MockLLMAdapter
    from verifygate.runner import run_verification
    from verifygate.spec import load_spec
    from verifygate.tavily import MockReferenceLookup

    class Counting(MockLLMAdapter):
        def __init__(self):
            self.explains = 0

        def explain_failure(self, check_results):
            self.explains += 1
            return super().explain_failure(check_results)

    root = Path(__file__).resolve().parent.parent
    llm = Counting()
    monkeypatch.setenv("VERIFYGATE_SIGNING_KEY", "")
    with pytest.raises(ValueError):
        run_verification(
            spec=load_spec(root / "samples" / "spec.json"),
            candidate_dir=root / "samples" / "failing",
            mode="judge", llm=llm, references=MockReferenceLookup(),
            spec_label="samples/spec.json",
            candidate_label="samples/failing")
    assert llm.explains == 0


def test_cli_run_with_malformed_key_exits_2(tmp_path, monkeypatch):
    monkeypatch.setenv("VERIFYGATE_SIGNING_KEY", "")
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    out = tmp_path / "rec.json"
    rc = cli.main([
        "run", "--spec", str(root / "samples" / "spec.json"),
        "--candidate", str(root / "samples" / "passing"),
        "--mode", "judge", "--out", str(out),
    ])
    assert rc == 2
    assert not out.exists()


def test_cli_verify_with_malformed_key_exits_2(tmp_path, monkeypatch):
    monkeypatch.delenv("VERIFYGATE_SIGNING_KEY", raising=False)
    record = _record()
    import json
    path = tmp_path / "rec.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setenv("VERIFYGATE_SIGNING_KEY", "x")
    rc = cli.main(["verify", "--record", str(path)])
    assert rc == 2
