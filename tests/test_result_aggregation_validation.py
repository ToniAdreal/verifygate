"""Result-aggregation validation (v1.56.0).

``verify.document_ok`` and ``replay.replay_ok`` are the two
functions that turn per-record results into the intact /
reproduced answers, and both took their input on trust — the
v1.55.0 verdict-aggregation shape one layer up:

* ``document_ok([])`` was True (``all`` of nothing): a
  document with no records was "intact";
* ``replay_ok(iter([]))`` was True (an iterator is always
  truthy, ``all`` of nothing is true) while
  ``replay_ok([])`` was False;
* ``None`` crashed ``document_ok`` with a ``TypeError`` and
  silently returned False from ``replay_ok`` — an answer
  about records that were never judged;
* a string, or a list of plain dicts, crashed with an
  unhandled ``TypeError`` / ``KeyError`` / unpacking error;
* a result whose ``chain_ok`` was a truthy non-bool counted
  as a verified chain (``document_ok``), and a replay result
  missing its match fields crashed instead of failing.

Both inputs are now materialised and every entry validated
(``validate_verify_result`` / ``validate_replay_result``): the
shapes ``check_record`` / the replay paths emit. An empty
input is False; a malformed one is one ``ValueError``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verifygate.mock_llm import MockLLMAdapter
from verifygate.replay import replay_document, replay_ok, validate_replay_result
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup
from verifygate.verify import document_ok, validate_verify_result, verify_document

APP = Path(__file__).resolve().parents[1]
SAMPLES = APP / "samples"
REAL_REPAIR = SAMPLES / "evidence_real_repair.json"


def _doc_result(**overrides) -> dict:
    base = {
        "verdict": "PASS",
        "mode": "judge",
        "spec_name": "t",
        "chain_ok": True,
        "candidate_match": None,
        "spec_match": None,
        "report_match": None,
    }
    base.update(overrides)
    return base


def _replay_result(**overrides) -> dict:
    base = {
        "label": "record",
        "chain_ok": True,
        "expected_verdict": "PASS",
        "actual_verdict": "PASS",
        "verdict_match": True,
        "checks_match": True,
        "candidate_match": True,
        "spec_match": True,
        "error": None,
    }
    base.update(overrides)
    return base


# -- genuine aggregates are unchanged ------------------------------------


def test_genuine_document_results_aggregate():
    assert document_ok([("record", _doc_result())]) is True
    assert document_ok([("record", _doc_result(chain_ok=False))]) is False
    assert document_ok(
        [("record", _doc_result(candidate_match=False))]) is False
    assert document_ok(
        [("record", _doc_result(spec_match=False))]) is False
    assert document_ok(
        [("record", _doc_result(report_match=False))]) is False
    assert document_ok([
        ("attempt 0", _doc_result()),
        ("attempt 1", _doc_result(report_match=True)),
    ]) is True


def test_genuine_replay_results_aggregate():
    assert replay_ok([_replay_result()]) is True
    assert replay_ok([_replay_result(chain_ok=False)]) is False
    assert replay_ok([_replay_result(verdict_match=False)]) is False
    assert replay_ok([_replay_result(error="boom")]) is False
    assert replay_ok(
        [_replay_result(), _replay_result(checks_match=False)]) is False


def test_producer_outputs_still_aggregate():
    record = json.loads(
        (SAMPLES / "evidence_passing.json").read_text(encoding="utf-8"))
    assert document_ok(verify_document(record)) is True
    report = json.loads(REAL_REPAIR.read_text(encoding="utf-8"))
    assert document_ok(verify_document(report)) is True
    results = replay_document(
        record,
        spec=load_spec(SAMPLES / "spec.json"),
        candidate_dir=SAMPLES / "passing",
        mode="judge",
        llm=MockLLMAdapter(),
        references=MockReferenceLookup(),
        spec_label=str(SAMPLES / "spec.json"),
    )
    assert replay_ok(results) is True


def test_validators_return_the_result_unchanged():
    doc = _doc_result()
    assert validate_verify_result(doc) is doc
    rep = _replay_result()
    assert validate_replay_result(rep) is rep


# -- empty inputs ----------------------------------------------------------


def test_empty_document_inputs_are_false_in_every_shape():
    # The vacuous-intact regression: document_ok([]) was True.
    assert document_ok([]) is False
    assert document_ok(()) is False
    assert document_ok(iter([])) is False


def test_empty_replay_inputs_are_false_in_every_shape():
    # The vacuous-reproduced regression: replay_ok(iter([]))
    # was True while replay_ok([]) was False.
    assert replay_ok([]) is False
    assert replay_ok(()) is False
    assert replay_ok(iter([])) is False
    assert replay_ok(x for x in ()) is False


def test_iterators_of_genuine_results_aggregate():
    assert document_ok(iter([("record", _doc_result())])) is True
    assert replay_ok(iter([_replay_result()])) is True


# -- the input itself -------------------------------------------------------


@pytest.mark.parametrize("bad", [None, "x", b"x", 123, 1.5, True, object()])
def test_document_non_iterable_inputs_are_value_errors(bad):
    with pytest.raises(ValueError):
        document_ok(bad)


@pytest.mark.parametrize("bad", [None, "x", b"x", 123, 1.5, True, object()])
def test_replay_non_iterable_inputs_are_value_errors(bad):
    with pytest.raises(ValueError):
        replay_ok(bad)


@pytest.mark.parametrize("bad_item", [
    "record",                       # a bare string is not a pair
    ("record",),                    # one element
    ("record", _doc_result(), 3),   # three elements
    (123, _doc_result()),           # non-string label
    (None, _doc_result()),
    ("record", "not-a-result"),     # non-object result
    ("record", None),
    ("record", {}),                 # no chain_ok at all
])
def test_document_malformed_items_are_value_errors(bad_item):
    with pytest.raises(ValueError):
        document_ok([bad_item])


@pytest.mark.parametrize("bad_result", [
    "not-a-result", None, 123, ["chain_ok"],
])
def test_replay_non_object_results_are_value_errors(bad_result):
    with pytest.raises(ValueError):
        replay_ok([bad_result])
    with pytest.raises(ValueError):
        validate_replay_result(bad_result)


# -- result field shapes ----------------------------------------------------


@pytest.mark.parametrize("overrides", [
    dict(chain_ok="yes"),           # truthy string counted as verified
    dict(chain_ok=1),               # truthy int
    dict(chain_ok=None),
    dict(candidate_match="match"),
    dict(candidate_match=0),
    dict(spec_match="match"),
    dict(report_match=1),
])
def test_document_malformed_result_fields_are_value_errors(overrides):
    with pytest.raises(ValueError):
        validate_verify_result(_doc_result(**overrides))
    with pytest.raises(ValueError):
        document_ok([("record", _doc_result(**overrides))])


def test_document_missing_chain_ok_is_a_value_error():
    result = _doc_result()
    del result["chain_ok"]
    with pytest.raises(ValueError):
        validate_verify_result(result)


def test_document_omitted_binding_fields_are_not_judged():
    # document_ok reads bindings through .get: a caller-built
    # result that omits one is judged on what it carries.
    result = _doc_result()
    del result["report_match"]
    assert validate_verify_result(result) is result
    assert document_ok([("record", result)]) is True


@pytest.mark.parametrize("field", [
    "chain_ok", "verdict_match", "checks_match",
    "candidate_match", "spec_match",
])
def test_replay_match_fields_must_be_present_bools(field):
    result = _replay_result(**{field: "yes"})
    with pytest.raises(ValueError):
        validate_replay_result(result)
    with pytest.raises(ValueError):
        replay_ok([result])
    missing = _replay_result()
    del missing[field]
    with pytest.raises(ValueError):
        validate_replay_result(missing)


@pytest.mark.parametrize("bad_error", [123, ["boom"], {"e": 1}, True])
def test_replay_error_field_must_be_string_or_none(bad_error):
    with pytest.raises(ValueError):
        validate_replay_result(_replay_result(error=bad_error))
    with pytest.raises(ValueError):
        replay_ok([_replay_result(error=bad_error)])
