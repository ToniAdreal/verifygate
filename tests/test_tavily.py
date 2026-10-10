"""Tavily diagnosis references: parsing, degradation, and the rule
that a lookup failure can never break a verification run.

References are commentary, never verdict inputs — so every failure
mode of the real adapter (no key, network/HTTP error, malformed
response) must degrade to no references with the reason recorded,
first on the adapter (``last_error``) and then inside the sealed
evidence record (``reference_error`` in the diagnosis entry).
"""

import urllib.error
from pathlib import Path

from verifygate.evidence import verify_chain
from verifygate.mock_llm import MockLLMAdapter
from verifygate.runner import run_verification
from verifygate.spec import load_spec
from verifygate.tavily import (
    MockReferenceLookup,
    ReferenceLookup,
    TavilyReferenceLookup,
    diagnosis_query,
)
from verifygate.webview import render_document

APP = Path(__file__).resolve().parent.parent
SPEC = load_spec(APP / "samples" / "spec.json")

FAILING = [{
    "name": "unit_tests",
    "command": "python3 -m pytest -q",
    "verdict": "FAIL",
    "exit_code": 1,
    "stderr_tail": "AssertionError: slug mismatch\n",
    "stdout_tail": "",
}]


def _run_with_references(references) -> dict:
    return run_verification(
        spec=SPEC,
        candidate_dir=APP / "samples" / "failing",
        mode="judge",
        llm=MockLLMAdapter(),
        references=references,
        spec_label="samples/spec.json",
        candidate_label="samples/failing",
    )


def _diagnosis(record: dict) -> dict:
    return next(e for e in record["entries"] if e["kind"] == "diagnosis")


# -- mock ----------------------------------------------------------------


def test_mock_lookup_is_canned_and_clean():
    mock = MockReferenceLookup()
    refs = mock.lookup("anything at all")
    assert mock.last_error is None
    assert len(refs) == 1 and refs[0]["source"] == "mock"


# -- real adapter: degradation --------------------------------------------


def test_no_key_degrades_without_raising(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    adapter = TavilyReferenceLookup()
    assert adapter.lookup("some error") == []
    assert adapter.last_error is not None
    assert "TAVILY_API_KEY" in adapter.last_error


def test_explicit_empty_key_degrades():
    adapter = TavilyReferenceLookup(api_key="")
    assert adapter.lookup("q") == []
    assert adapter.last_error is not None


def test_transport_failure_degrades_without_raising():
    def boom(body):
        raise urllib.error.URLError("connection refused")

    adapter = TavilyReferenceLookup(api_key="test-key", transport=boom)
    assert adapter.lookup("q") == []
    assert adapter.last_error is not None
    assert "URLError" in adapter.last_error


def test_malformed_response_degrades():
    adapter = TavilyReferenceLookup(
        api_key="test-key", transport=lambda body: {"unexpected": True})
    assert adapter.lookup("q") == []
    assert adapter.last_error is not None
    assert "malformed" in adapter.last_error

    adapter2 = TavilyReferenceLookup(
        api_key="test-key", transport=lambda body: ["not", "a", "dict"])
    assert adapter2.lookup("q") == []
    assert adapter2.last_error is not None


# -- real adapter: success path --------------------------------------------


def test_successful_lookup_parses_and_truncates():
    seen = {}

    def fake(body):
        seen.update(body)
        return {"results": [
            {"title": "Doc A", "url": "https://example.com/a",
             "content": "x" * 500},
            {"title": "Doc B", "url": "https://example.com/b",
             "content": "short"},
            "not-a-dict",
            {"url": "https://example.com/no-title"},
        ]}

    adapter = TavilyReferenceLookup(api_key="test-key", transport=fake)
    refs = adapter.lookup("my query", max_results=5)
    assert adapter.last_error is None
    assert seen["query"] == "my query"
    assert seen["max_results"] == 5
    assert len(refs) == 3  # the non-dict item is skipped, not fatal
    assert refs[0]["source"] == "tavily"
    assert len(refs[0]["snippet"]) == 300  # truncated, not the raw 500
    assert refs[2]["title"] == "" and refs[2]["snippet"] == ""


# -- diagnosis query --------------------------------------------------------


def test_diagnosis_query_uses_first_failure_tail():
    query = diagnosis_query(FAILING)
    assert "AssertionError: slug mismatch" in query
    assert diagnosis_query(
        [{"name": "ok", "command": "true", "verdict": "PASS"}]) == ""
    assert diagnosis_query([]) == ""


def test_diagnosis_query_tolerates_missing_fields():
    # A check dict missing the usual keys must not raise.
    query = diagnosis_query([{"verdict": "FAIL"}])
    assert query  # falls back to the (empty) command placeholder text


# -- runner integration: degradation is sealed, verdict is unaffected -------


class _ExplodingLookup(ReferenceLookup):
    name = "exploding-stub"

    def lookup(self, query: str, max_results: int = 3) -> list[dict]:
        raise RuntimeError("lookup blew up")


def test_raising_lookup_cannot_break_a_run():
    record = _run_with_references(_ExplodingLookup())
    assert record["verdict"] == "FAIL"  # decided by the checks alone
    assert verify_chain(record) is True
    diagnosis = _diagnosis(record)
    assert diagnosis["references"] == []
    assert diagnosis["reference_error"] is not None
    assert "lookup blew up" in diagnosis["reference_error"]


def test_no_key_tavily_run_notes_degradation_in_record(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    record = _run_with_references(TavilyReferenceLookup())
    assert record["verdict"] == "FAIL"
    assert verify_chain(record) is True
    diagnosis = _diagnosis(record)
    assert diagnosis["references"] == []
    assert diagnosis["reference_source"] == "tavily"
    assert "TAVILY_API_KEY" in diagnosis["reference_error"]


def test_successful_tavily_refs_are_sealed_in_record():
    def fake(body):
        return {"results": [
            {"title": "Slug docs", "url": "https://example.com/slug",
             "content": "how slugify should behave"}]}

    record = _run_with_references(
        TavilyReferenceLookup(api_key="test-key", transport=fake))
    assert verify_chain(record) is True
    diagnosis = _diagnosis(record)
    assert diagnosis["reference_error"] is None
    assert diagnosis["references"][0]["source"] == "tavily"
    assert diagnosis["references"][0]["title"] == "Slug docs"


def test_mock_run_records_no_reference_error():
    record = _run_with_references(MockReferenceLookup())
    diagnosis = _diagnosis(record)
    assert diagnosis["reference_error"] is None
    assert diagnosis["references"][0]["source"] == "mock"


def test_webview_renders_reference_degradation_note(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    record = _run_with_references(TavilyReferenceLookup())
    page = render_document(record)
    assert "References unavailable" in page
    assert "TAVILY_API_KEY" in page
    assert "Hash chain verified" in page
