"""Adapter-method argument validation (v1.52.0).

The entry points validate the adapters themselves (v1.42.0), but
the adapters' two advisory methods — and the reference lookups —
took their *arguments* on trust:

* a non-string brief crashed the mock with ``AttributeError`` and
  was silently stringified by the real adapter into a paid prompt
  (``Task brief: 123``); an over-long brief bloated it without
  bound;
* a non-dict ``failing_evidence`` crashed ``generate_patch`` on
  ``.items()``, and a non-JSON-serializable one in ``json.dumps``;
* a non-list ``check_results`` crashed ``explain_failure`` with a
  ``TypeError``, and an entry missing ``verdict`` with a
  ``KeyError``;
* a non-string lookup query crashed the mock lookup (whose
  contract is *must not raise*) and was sent verbatim to the real
  Tavily API as the JSON body's ``query``;
* ``diagnosis_query`` crashed on a non-list input or a non-object
  entry, despite tolerating missing fields since v0.7.0.

LLM adapter methods now raise one ``ValueError`` for a malformed
argument before any model call; lookups degrade (no references,
the reason on ``last_error``) exactly like a failed lookup.
"""

from __future__ import annotations

import pytest

from verifygate.llm import (
    validate_brief,
    validate_check_results,
    validate_failing_evidence,
)
from verifygate.mock_llm import MockLLMAdapter
from verifygate.spec import MAX_BRIEF_CHARS
from verifygate.tavily import (
    MAX_QUERY_CHARS,
    MockReferenceLookup,
    TavilyReferenceLookup,
    diagnosis_query,
)
from verifygate.tokenfactory import TokenFactoryAdapter

FAIL_DICTS = [
    {"name": "unit_tests", "command": "pytest -q", "verdict": "FAIL",
     "exit_code": 1, "timed_out": False,
     "stderr_tail": "AssertionError: boom", "stdout_tail": ""},
]


class _Transport:
    """Fake Token Factory / Tavily transport recording its bodies."""

    def __init__(self):
        self.bodies = []

    def __call__(self, body):
        self.bodies.append(body)
        return {
            "model": "fake-model",
            "choices": [{"message": {"content": "model words"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4,
                      "total_tokens": 7},
            "results": [],
        }


# -- the shared helpers ---------------------------------------------------


@pytest.mark.parametrize("bad", [None, 123, True, 1.5, b"x", ["b"], {"b": 1}])
def test_validate_brief_rejects_non_strings(bad):
    with pytest.raises(ValueError):
        validate_brief(bad)


def test_validate_brief_accepts_empty_and_at_ceiling():
    assert validate_brief("") == ""
    assert validate_brief("x" * MAX_BRIEF_CHARS) == "x" * MAX_BRIEF_CHARS


def test_validate_brief_rejects_over_ceiling_with_bounded_message():
    with pytest.raises(ValueError) as excinfo:
        validate_brief("x" * 500_000)
    assert len(str(excinfo.value)) < 300
    with pytest.raises(ValueError):
        validate_brief("x" * (MAX_BRIEF_CHARS + 1))


@pytest.mark.parametrize("bad", [[], "x", 123, True])
def test_validate_failing_evidence_rejects_non_dicts(bad):
    with pytest.raises(ValueError):
        validate_failing_evidence(bad)


def test_validate_failing_evidence_accepts_none_and_dict():
    assert validate_failing_evidence(None) is None
    assert validate_failing_evidence({"a": 1}) == {"a": 1}


@pytest.mark.parametrize(
    "bad", [None, "x", 123, {"verdict": "FAIL"}, [123], ["x"], [None],
            [{"ok": 1}, "x"]])
def test_validate_check_results_rejects_bad_shapes(bad):
    with pytest.raises(ValueError):
        validate_check_results(bad)


def test_validate_check_results_accepts_list_and_tuple():
    assert validate_check_results(FAIL_DICTS) == FAIL_DICTS
    assert validate_check_results(tuple(FAIL_DICTS)) == FAIL_DICTS
    assert validate_check_results([]) == []


# -- the mock LLM adapter ---------------------------------------------------


@pytest.mark.parametrize("bad", [None, 123, True, ["brief"]])
def test_mock_generate_patch_rejects_bad_briefs(bad):
    with pytest.raises(ValueError):
        MockLLMAdapter().generate_patch(bad)


def test_mock_generate_patch_rejects_bad_evidence():
    with pytest.raises(ValueError):
        MockLLMAdapter().generate_patch("fix it", failing_evidence=["x"])


def test_mock_generate_patch_genuine_briefs_unchanged():
    mock = MockLLMAdapter()
    assert "slugify" in mock.generate_patch("Fix slugify() please")
    assert "total(items" in mock.generate_patch("Fix the cart total")
    plan = mock.generate_patch("Do something else")
    assert plan.startswith("MOCK PATCH PROPOSAL")
    with_failures = mock.generate_patch(
        "Do something else",
        failing_evidence={"checks": [{"name": "unit_tests",
                                      "verdict": "FAIL"}, "junk", 7]})
    assert "Previous failures to address: unit_tests" in with_failures


@pytest.mark.parametrize("bad", [None, "x", 123, [123], ["x"]])
def test_mock_explain_failure_rejects_bad_check_results(bad):
    with pytest.raises(ValueError):
        MockLLMAdapter().explain_failure(bad)


def test_mock_explain_failure_tolerates_missing_fields():
    out = MockLLMAdapter().explain_failure([{"verdict": "FAIL"}])
    assert "(unnamed check)" in out
    assert "Exit code: None." in out


def test_mock_explain_failure_genuine_output_unchanged():
    out = MockLLMAdapter().explain_failure(FAIL_DICTS)
    assert "unit_tests" in out and "AssertionError: boom" in out
    assert MockLLMAdapter().explain_failure(
        [{"verdict": "PASS"}]) == "All checks passed; no diagnosis required."


# -- the real Token Factory adapter (fake transport) -------------------------


@pytest.mark.parametrize("bad", [None, 123, False, ["brief"]])
def test_tf_generate_patch_rejects_bad_briefs_without_calling(bad):
    transport = _Transport()
    adapter = TokenFactoryAdapter(transport=transport)
    with pytest.raises(ValueError):
        adapter.generate_patch(bad)
    assert transport.bodies == []
    assert adapter.pending_calls() == []


def test_tf_generate_patch_rejects_bad_evidence_without_calling():
    transport = _Transport()
    adapter = TokenFactoryAdapter(transport=transport)
    with pytest.raises(ValueError):
        adapter.generate_patch("brief", failing_evidence=["not-a-dict"])
    with pytest.raises(ValueError):
        adapter.generate_patch("brief", failing_evidence={"x": object()})
    assert transport.bodies == []


def test_tf_generate_patch_tolerates_non_dict_candidate_files():
    transport = _Transport()
    adapter = TokenFactoryAdapter(transport=transport)
    out = adapter.generate_patch(
        "brief", failing_evidence={"candidate_files": ["not", "a", "dict"],
                                   "checks": []})
    assert out == "model words"
    assert len(transport.bodies) == 1


def test_tf_generate_patch_genuine_call_unchanged():
    transport = _Transport()
    adapter = TokenFactoryAdapter(transport=transport)
    assert adapter.generate_patch("Fix the thing") == "model words"
    assert "Task brief:\nFix the thing" in \
        transport.bodies[0]["messages"][1]["content"]


@pytest.mark.parametrize("bad", [None, "x", 123, [123]])
def test_tf_explain_failure_rejects_bad_check_results_without_calling(bad):
    transport = _Transport()
    adapter = TokenFactoryAdapter(transport=transport)
    with pytest.raises(ValueError):
        adapter.explain_failure(bad)
    assert transport.bodies == []


def test_tf_explain_failure_tolerates_entries_without_verdict():
    transport = _Transport()
    adapter = TokenFactoryAdapter(transport=transport)
    assert adapter.explain_failure([{"name": "n"}]) == "model words"
    assert len(transport.bodies) == 1


def test_tf_explain_failure_rejects_non_serializable_results():
    transport = _Transport()
    adapter = TokenFactoryAdapter(transport=transport)
    with pytest.raises(ValueError):
        adapter.explain_failure(
            [{"verdict": "FAIL", "blob": object()}])
    assert transport.bodies == []


# -- the reference lookups ----------------------------------------------------


@pytest.mark.parametrize("bad", [None, 123, True, ["q"], {"q": 1}])
def test_mock_lookup_degrades_on_bad_query(bad):
    lookup = MockReferenceLookup()
    assert lookup.lookup(bad) == []
    assert lookup.last_error is not None
    assert "query" in lookup.last_error


def test_mock_lookup_degrades_on_over_long_query():
    lookup = MockReferenceLookup()
    assert lookup.lookup("x" * (MAX_QUERY_CHARS + 1)) == []
    assert lookup.last_error is not None


def test_mock_lookup_genuine_query_unchanged():
    lookup = MockReferenceLookup()
    refs = lookup.lookup("some error — documentation")
    assert len(refs) == 1 and refs[0]["source"] == "mock"
    assert lookup.last_error is None
    assert lookup.lookup("x" * MAX_QUERY_CHARS)[0]["source"] == "mock"


@pytest.mark.parametrize("bad", [None, 123, ["q"]])
def test_tavily_lookup_degrades_on_bad_query_without_calling(bad):
    transport = _Transport()
    lookup = TavilyReferenceLookup(api_key="key", transport=transport)
    assert lookup.lookup(bad) == []
    assert lookup.last_error is not None
    assert transport.bodies == []


def test_tavily_lookup_genuine_query_sends_string_query():
    transport = _Transport()
    lookup = TavilyReferenceLookup(api_key="key", transport=transport)
    assert lookup.lookup("a real query") == []
    assert transport.bodies[0]["query"] == "a real query"


# -- diagnosis_query ------------------------------------------------------------


@pytest.mark.parametrize("bad", [None, "x", 123, {"verdict": "FAIL"}])
def test_diagnosis_query_tolerates_non_list_input(bad):
    assert diagnosis_query(bad) == ""


def test_diagnosis_query_skips_non_dict_entries():
    assert diagnosis_query([123, "x", None]) == ""
    query = diagnosis_query([123, FAIL_DICTS[0]])
    assert "AssertionError: boom" in query


def test_diagnosis_query_tolerates_non_string_tails():
    entry = {"verdict": "FAIL", "stderr_tail": 12345, "command": "pytest"}
    assert "pytest" in diagnosis_query([entry])


def test_diagnosis_query_genuine_output_unchanged():
    query = diagnosis_query(FAIL_DICTS)
    assert query.startswith("AssertionError: boom")
    assert diagnosis_query([{"verdict": "PASS"}]) == ""
