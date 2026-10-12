"""Error-note normalisation (v1.60.0).

The two text helpers on the runner's sealing path judged only
the *length* of what they were handed:

* ``runner._cap_error`` truncated a long string but copied any
  other value verbatim — and the one note it does not format
  itself, a lookup's ``last_error``, is adapter-supplied: a
  third-party ``ReferenceLookup`` may set it to anything. An
  ``int`` or ``dict`` note sealed verbatim produced a record
  whose diagnosis entry fails the structure check (a note must
  be a string or ``None`` within ``MAX_ERROR_CHARS``), so the
  record failed its own verification the moment it was written;
  a non-serializable note (an exception object, a set) crashed
  ``build_record``'s canonical JSON encoding with a raw
  ``TypeError`` and the FAIL record was lost entirely.
* ``runner.normalise_explanation`` assumed a string and crashed
  with a raw ``TypeError`` from ``len()`` on any other type.

Contract pinned here: a non-string error note is discarded for
a deterministic placeholder naming only the offending type;
string notes pass through exactly as before (``None``, short,
at-cap, truncated over-cap); a non-string explanation is one
``ValueError``; and a FAIL run whose lookup reports any of the
malformed notes still seals a record that verifies, with the
original candidate byte-unchanged.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from verifygate.evidence import MAX_ERROR_CHARS, verify_chain
from verifygate.mock_llm import MockLLMAdapter
from verifygate.runner import (
    _cap_error,
    normalise_explanation,
    run_verification,
    write_record,
)
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup
from verifygate.verify import document_ok, verify_document

APP = Path(__file__).resolve().parent.parent
SPEC = APP / "samples" / "spec.json"
FAILING = APP / "samples" / "failing"

BAD_NOTES = [
    12345,
    1.5,
    True,
    {"code": 500},
    ["network down"],
    ("network down",),
    {"a", "b"},
    b"bytes",
    RuntimeError("boom"),
    object(),
]


class _StubLookup(MockReferenceLookup):
    """A lookup that reports a caller-chosen ``last_error``."""

    name = "stub-lookup"

    def __init__(self, note):
        super().__init__()
        self._note = note

    def lookup(self, query, max_results=3):
        self.last_error = self._note
        return []


def _run_with_lookup(lookup):
    spec = load_spec(SPEC)
    return run_verification(
        spec=spec, candidate_dir=FAILING, mode="judge",
        llm=MockLLMAdapter(), references=lookup,
        spec_label=str(SPEC), candidate_label=str(FAILING))


def _diagnosis(record):
    return next(e for e in record["entries"] if e["kind"] == "diagnosis")


def _md5_tree(root: Path) -> str:
    h = hashlib.md5()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()


# --- _cap_error: the string contract is unchanged ---------------------


def test_cap_error_string_contract_unchanged():
    assert _cap_error(None) is None
    assert _cap_error("short") == "short"
    assert _cap_error("x" * MAX_ERROR_CHARS) == "x" * MAX_ERROR_CHARS
    assert _cap_error("x" * (MAX_ERROR_CHARS + 1)) == \
        "x" * MAX_ERROR_CHARS


# --- _cap_error: non-string notes are normalised ----------------------


@pytest.mark.parametrize("note", BAD_NOTES)
def test_cap_error_non_string_note_becomes_placeholder(note):
    out = _cap_error(note)
    assert isinstance(out, str)
    assert len(out) <= MAX_ERROR_CHARS
    assert out == (f"malformed error note discarded "
                   f"(got {type(note).__name__})")
    # Nothing of the malformed value itself is carried over.
    assert "boom" not in out
    assert "network down" not in out
    assert "500" not in out


def test_cap_error_placeholder_is_deterministic():
    assert _cap_error(1) == _cap_error(999)
    assert _cap_error({}) == _cap_error({"other": 1})


# --- normalise_explanation: non-string input is a ValueError ----------


@pytest.mark.parametrize(
    "bad", [None, 123, 1.5, True, ["text"], {"text": 1}, b"text"])
def test_normalise_explanation_rejects_non_string(bad):
    with pytest.raises(ValueError):
        normalise_explanation(bad)


def test_normalise_explanation_string_contract_unchanged():
    assert normalise_explanation("a short diagnosis") == \
        "a short diagnosis"
    assert normalise_explanation("") == ""


# --- Runner level: a malformed last_error still seals a record --------


@pytest.mark.parametrize("note", BAD_NOTES)
def test_runner_seals_verifying_record_over_bad_note(note):
    before = _md5_tree(FAILING)
    record = _run_with_lookup(_StubLookup(note))
    diagnosis = _diagnosis(record)
    assert diagnosis["reference_error"] == (
        f"malformed error note discarded (got {type(note).__name__})")
    assert diagnosis["diagnosis_error"] is None
    assert record["verdict"] == "FAIL"
    assert verify_chain(record) is True
    results = verify_document(record)
    assert document_ok(results) is True
    assert _md5_tree(FAILING) == before


def test_runner_record_over_exception_note_is_writable(tmp_path):
    # The pre-fix crash shape: an exception-object note escaped
    # build_record's JSON encoding as a raw TypeError and the
    # record was lost. It now seals, verifies, and writes.
    record = _run_with_lookup(_StubLookup(RuntimeError("boom")))
    out = write_record(record, tmp_path / "record.json")
    assert out.exists()
    assert verify_chain(record) is True


def test_runner_genuine_string_note_sealed_verbatim():
    record = _run_with_lookup(_StubLookup("tavily: HTTP 503"))
    assert _diagnosis(record)["reference_error"] == "tavily: HTTP 503"
    assert verify_chain(record) is True


def test_runner_huge_string_note_truncated_and_verifies():
    record = _run_with_lookup(_StubLookup("x" * 500_000))
    note = _diagnosis(record)["reference_error"]
    assert note == "x" * MAX_ERROR_CHARS
    assert verify_chain(record) is True


def test_runner_mock_lookup_record_unchanged():
    record = _run_with_lookup(MockReferenceLookup())
    diagnosis = _diagnosis(record)
    assert diagnosis["reference_error"] is None
    assert verify_chain(record) is True
