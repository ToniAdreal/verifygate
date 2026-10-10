"""Web view: rendering is faithful, escaped, and chain-aware.

The evidence record contains attacker-influenced text (check output,
model prose, reference snippets) — these tests pin the guarantee that
none of it can become markup, that non-http(s) reference URLs never
become links, and that a tampered record renders an explicit warning
instead of a clean verdict page.
"""

import json
from pathlib import Path

from verifygate.evidence import verify_chain
from verifygate.mock_llm import MockLLMAdapter
from verifygate.runner import run_verification
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup
from verifygate.webview import render_document

APP = Path(__file__).resolve().parent.parent
SPEC = load_spec(APP / "samples" / "spec.json")


def _record(candidate: str = "failing") -> dict:
    return run_verification(
        spec=SPEC,
        candidate_dir=APP / "samples" / candidate,
        mode="judge",
        llm=MockLLMAdapter(),
        references=MockReferenceLookup(),
        spec_label="samples/spec.json",
        candidate_label=f"samples/{candidate}",
    )


def test_renders_pass_and_fail_records():
    failing = render_document(_record("failing"))
    assert "VERDICT: FAIL" in failing
    assert "unit_tests" in failing and "repro_script" in failing
    assert "Hash chain verified" in failing
    passing = render_document(_record("passing"))
    assert "VERDICT: PASS" in passing
    assert "CHAIN INVALID" not in passing


def test_record_content_is_escaped():
    record = _record("failing")
    # Inject markup into check output and the diagnosis, then re-render
    # (the chain will no longer verify — the page must say so AND the
    # payload must stay inert text).
    for entry in record["entries"]:
        if entry["kind"] == "check":
            entry["stdout_tail"] = "<script>alert('xss')</script>"
        if entry["kind"] == "diagnosis":
            entry["explanation"] = "<img src=x onerror=alert(1)>"
            entry["references"] = [{
                "title": "<b>bold</b>",
                "url": "javascript:alert(1)",
                "snippet": "<svg onload=alert(2)>",
                "source": "tavily",
            }]
    page = render_document(record)
    assert "<script>alert" not in page
    assert "<img src=x" not in page
    assert "<svg onload" not in page
    assert "&lt;script&gt;" in page
    assert 'href="javascript:' not in page
    assert "CHAIN INVALID" in page


def test_http_reference_becomes_link():
    record = _record("failing")
    page = render_document(record)
    assert '<a href="https://docs.python.org/3/"' in page


def test_repair_report_rendering():
    report = json.loads(
        (APP / "samples" / "evidence_real_repair.json").read_text())
    # The fixture's own chains must still verify before we trust it
    # as the demo source.
    for attempt in report["attempts"]:
        assert verify_chain(attempt["evidence"]) is True
    page = render_document(report)
    assert "Attempt 0" in page and "Attempt 1" in page
    assert "VERDICT: FAIL" in page and "VERDICT: PASS" in page
    assert "5,367" in page or "5367" in page
    assert "LLM usage" in page
    assert page.count("Hash chain verified") == len(report["attempts"])
    assert "<script" not in page.lower().replace(
        "&lt;script", "")  # no live script tags anywhere


def test_page_is_self_contained():
    page = render_document(_record("passing"))
    assert page.startswith("<!DOCTYPE html>")
    assert "http://" not in page and "https://" not in page
    assert "<script" not in page.lower()
    assert "<style>" in page
