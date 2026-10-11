"""Binding-hash validation: the supplied side of a binding is judged.

Before v1.43.0 ``verify`` compared whatever hash a caller
supplied against the record's sealed header verbatim. The
header fields are held to the SHA-256 hex form (64 lowercase
hex characters), but the supplied ``spec_hash`` (to
``verify_document``) and ``candidate_hash`` / ``spec_hash``
(to ``check_record``) were taken on trust:

* an int, a bool, an empty string, a truncated or upper-case
  hex string, or a 500,000-character string was reported as a
  binding *mismatch* — ``spec_match`` / ``candidate_match``
  False, ``document_ok`` False — as if a genuine hash had
  been compared and the record had failed it, instead of
  the usage error the malformed value actually is.

Both arguments are now validated up front through
``evidence.validate_binding_hash``: a malformed hash is a
``ValueError`` (the CLI maps it to exit 2) before any record
is checked or any tree is hashed, ``None`` remains the
unjudged "no binding requested" sentinel, and a well-formed
hash that differs remains a genuine mismatch, reported
exactly as before.
"""

import json
from pathlib import Path

import pytest

from verifygate.evidence import validate_binding_hash
from verifygate.spec import load_spec
from verifygate.verify import check_record, document_ok, verify_document

APP = Path(__file__).resolve().parent.parent
SPEC = load_spec(APP / "samples" / "spec.json")
PASSING_RECORD = json.loads(
    (APP / "samples" / "evidence_passing.json").read_text(encoding="utf-8"))
FAILING_DIR = APP / "samples" / "failing"

GOOD_HASH = SPEC.hash
OTHER_HASH = "b" * 64  # well-formed, simply not this spec's hash

BAD_HASHES = [
    123, True, 2.5, b"a" * 64, ["a" * 64], {"hash": "a" * 64},
    "", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "z" * 64,
    "a" * 500_000, "not-a-hash", " a" * 32,
]


def test_helper_accepts_well_formed_hash_and_none_sentinel():
    assert validate_binding_hash(GOOD_HASH) == GOOD_HASH
    assert validate_binding_hash(OTHER_HASH, "candidate_hash") == OTHER_HASH
    assert validate_binding_hash(None) is None
    assert validate_binding_hash(None, "candidate_hash") is None


@pytest.mark.parametrize("bad", BAD_HASHES)
def test_helper_rejects_malformed_hashes(bad):
    with pytest.raises(ValueError, match="SHA-256"):
        validate_binding_hash(bad)


def test_helper_error_names_the_argument_and_stays_bounded():
    with pytest.raises(ValueError) as excinfo:
        validate_binding_hash("x" * 500_000, "spec_hash")
    message = str(excinfo.value)
    assert message.startswith("spec_hash must be")
    assert len(message) < 200


@pytest.mark.parametrize("bad", BAD_HASHES)
def test_verify_document_rejects_malformed_spec_hash(bad):
    with pytest.raises(ValueError, match="spec_hash"):
        verify_document(PASSING_RECORD, spec_hash=bad)


@pytest.mark.parametrize("bad", BAD_HASHES)
def test_check_record_rejects_malformed_spec_hash(bad):
    with pytest.raises(ValueError, match="spec_hash"):
        check_record(PASSING_RECORD, spec_hash=bad)


@pytest.mark.parametrize("bad", BAD_HASHES)
def test_check_record_rejects_malformed_candidate_hash(bad):
    with pytest.raises(ValueError, match="candidate_hash"):
        check_record(PASSING_RECORD, candidate_hash=bad)


def test_verify_document_validates_spec_hash_before_candidate_dir():
    # A malformed spec hash is judged before the candidate
    # directory is looked at, let alone hashed: the missing
    # directory would raise FileNotFoundError, the malformed
    # hash must win.
    with pytest.raises(ValueError, match="spec_hash"):
        verify_document(
            PASSING_RECORD,
            candidate_dir=APP / "samples" / "does-not-exist",
            spec_hash="not-a-hash")


def test_well_formed_matching_spec_hash_still_matches():
    results = verify_document(PASSING_RECORD, spec_hash=GOOD_HASH)
    assert results[0][1]["spec_match"] is True
    assert document_ok(results)


def test_well_formed_different_spec_hash_is_a_genuine_mismatch():
    # The distinction the validation exists to preserve: a
    # well-formed hash that differs is NOT a usage error — it
    # is a record that does not describe the supplied spec.
    results = verify_document(PASSING_RECORD, spec_hash=OTHER_HASH)
    assert results[0][1]["spec_match"] is False
    assert not document_ok(results)
    result = check_record(PASSING_RECORD, candidate_hash=OTHER_HASH)
    assert result["candidate_match"] is False


def test_none_spec_hash_remains_the_unjudged_sentinel():
    results = verify_document(PASSING_RECORD, spec_hash=None)
    assert results[0][1]["spec_match"] is None
    assert document_ok(results)
    result = check_record(PASSING_RECORD)
    assert result["spec_match"] is None
    assert result["candidate_match"] is None


def test_candidate_binding_still_matches_with_spec_hash_validated():
    results = verify_document(
        PASSING_RECORD, candidate_dir=FAILING_DIR, spec_hash=GOOD_HASH)
    # The passing record was sealed against the passing
    # candidate, so binding it to the failing directory is a
    # genuine candidate mismatch alongside the spec match.
    assert results[0][1]["spec_match"] is True
    assert results[0][1]["candidate_match"] is False
