import pytest

from verifygate.spec import SpecError, parse_spec

VALID = {
    "name": "demo",
    "brief": "do the thing",
    "checks": [{"name": "tests", "command": "python3 -m pytest -q", "timeout_s": 30}],
}


def test_parse_valid_spec():
    spec = parse_spec(VALID)
    assert spec.name == "demo"
    assert spec.checks[0].command == "python3 -m pytest -q"
    assert len(spec.hash) == 64


def test_hash_is_deterministic():
    assert parse_spec(VALID).hash == parse_spec(dict(VALID)).hash


def test_missing_name_rejected():
    bad = {k: v for k, v in VALID.items() if k != "name"}
    with pytest.raises(SpecError):
        parse_spec(bad)


def test_empty_checks_rejected():
    with pytest.raises(SpecError):
        parse_spec({"name": "x", "checks": []})


def test_duplicate_check_names_rejected():
    bad = {"name": "x", "checks": [
        {"name": "a", "command": "true"},
        {"name": "a", "command": "true"},
    ]}
    with pytest.raises(SpecError):
        parse_spec(bad)


def test_bad_timeout_rejected():
    bad = {"name": "x", "checks": [{"name": "a", "command": "true", "timeout_s": 0}]}
    with pytest.raises(SpecError):
        parse_spec(bad)
