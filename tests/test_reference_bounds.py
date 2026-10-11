"""Reference bounds: diagnosis references are a bounded dimension.

Before v1.26.0 the diagnosis references were the one unbounded,
unnormalised input left on the sealing path:

* the real Tavily adapter never sliced its response to the
  requested ``max_results`` — a response carrying 500 results
  returned all 500, with titles and URLs unbounded — and
* the runner sealed whatever any ``ReferenceLookup`` implementation
  returned, verbatim: malformed references (non-object items,
  non-string fields, extra keys) produced a record that failed
  VerifyGate's own chain verification the moment it was written,
  while 500 well-formed references sealed and verified INTACT.

Contract pinned here:
* one normalisation helper (``tavily.normalise_references``) is
  shared by the real adapter and the runner — the one sealing path;
* the adapter slices to the requested count (hard ceiling
  ``MAX_RESULTS_CEILING``; an invalid request falls back, never
  raises), truncates every field to its cap, and pins the source;
* the runner seals at most ``MAX_REFERENCES`` normalised references
  from *any* lookup implementation, and a non-list return degrades
  to no references with the malformation noted in the record;
* ``evidence``'s structure check mirrors the sealing bounds, so a
  record carrying more references — or a longer field — than any
  real run can seal fails verification however it was sealed, while
  every committed fixture still verifies.
"""

import json
from pathlib import Path

from verifygate import cli
from verifygate.evidence import build_record, verify_chain
from verifygate.mock_llm import MockLLMAdapter
from verifygate.runner import run_verification
from verifygate.spec import parse_spec
from verifygate.tavily import (
    MAX_REFERENCES,
    MAX_RESULTS_CEILING,
    SNIPPET_LIMIT,
    SOURCE_LIMIT,
    TITLE_LIMIT,
    URL_LIMIT,
    MockReferenceLookup,
    TavilyReferenceLookup,
    normalise_reference,
    normalise_references,
)
from verifygate.verify import document_ok, verify_document
from verifygate.webview import render_document

APP = Path(__file__).resolve().parent.parent


def _ref(i: int = 0) -> dict:
    return {"title": f"Doc {i}", "url": f"https://example.com/{i}",
            "snippet": "snippet", "source": "stub"}


class _StubLookup:
    name = "stub"
    last_error = None

    def __init__(self, out):
        self._out = out

    def lookup(self, query, max_results=3):
        return self._out


def _run_with_refs(out) -> dict:
    spec = parse_spec(json.loads(
        (APP / "samples" / "spec.json").read_text(encoding="utf-8")))
    return run_verification(
        spec=spec, candidate_dir=APP / "samples" / "failing",
        mode="judge", llm=MockLLMAdapter(), references=_StubLookup(out),
        spec_label="samples/spec.json", candidate_label="samples/failing")


def _diagnosis(record: dict) -> dict:
    return next(e for e in record["entries"] if e["kind"] == "diagnosis")


def _record_with_refs(refs: list) -> dict:
    checks = [{
        "name": "c0", "command": "false", "exit_code": 1,
        "duration_ms": 1, "stdout_tail": "", "stderr_tail": "boom",
        "timed_out": False, "verdict": "FAIL",
    }]
    return build_record(
        spec_name="refs", spec_hash="a" * 64,
        candidate_tree_hash="b" * 64, mode="judge",
        check_results=checks, verdict="FAIL",
        diagnosis={"explanation": "it failed", "references": refs,
                   "llm_adapter": "stub", "reference_source": "stub",
                   "reference_error": None, "diagnosis_error": None},
        replay_command="python3 -m verifygate run --spec s.json "
                       "--candidate cand --mode judge")


# -- the normalisation helper ------------------------------------------


def test_normalise_drops_non_objects_and_container_fields():
    assert normalise_reference("not-a-dict") is None
    assert normalise_reference(None) is None
    assert normalise_reference({**_ref(), "title": {"x": 1}}) is None
    assert normalise_reference({**_ref(), "url": ["https://x"]}) is None


def test_normalise_coerces_scalars_drops_extras_and_truncates():
    ref = normalise_reference({
        "title": 42, "url": "https://x", "snippet": "s" * 5000,
        "source": "stub", "injected": "drop me"})
    assert ref == {"title": "42", "url": "https://x",
                   "snippet": "s" * SNIPPET_LIMIT, "source": "stub"}


def test_normalise_accepts_tavily_content_key_as_snippet():
    ref = normalise_reference(
        {"title": "T", "url": "https://x", "content": "body text"})
    assert ref["snippet"] == "body text"


def test_normalise_references_caps_count_and_rejects_non_lists():
    assert normalise_references("oops") == []
    assert normalise_references(None) == []
    refs = normalise_references([_ref(i) for i in range(50)])
    assert len(refs) == MAX_REFERENCES


# -- the real adapter ----------------------------------------------------


def _tavily(results, seen=None):
    def transport(body):
        if seen is not None:
            seen.update(body)
        return {"results": results}
    return TavilyReferenceLookup(api_key="k", transport=transport)


def test_tavily_slices_response_to_the_requested_count():
    results = [{"title": f"D{i}", "url": "https://x", "content": "c"}
               for i in range(500)]
    refs = _tavily(results).lookup("q", max_results=3)
    assert len(refs) == 3
    assert all(r["source"] == "tavily" for r in refs)


def test_tavily_truncates_every_field():
    results = [{"title": "T" * 5000, "url": "https://x/" + "u" * 5000,
                "content": "c" * 5000}]
    (ref,) = _tavily(results).lookup("q")
    assert len(ref["title"]) == TITLE_LIMIT
    assert len(ref["url"]) == URL_LIMIT
    assert len(ref["snippet"]) == SNIPPET_LIMIT


def test_tavily_request_is_clamped_and_never_raises():
    seen: dict = {}
    refs = _tavily([{"title": "D", "url": "https://x", "content": "c"}],
                   seen).lookup("q", max_results=10 ** 9)
    assert seen["max_results"] == MAX_RESULTS_CEILING
    assert len(refs) == 1
    seen.clear()
    _tavily([], seen).lookup("q", max_results="five")
    assert seen["max_results"] == MAX_REFERENCES  # invalid -> default
    seen.clear()
    _tavily([], seen).lookup("q", max_results=True)
    assert seen["max_results"] == MAX_REFERENCES  # bool -> default


# -- the runner (the one sealing path) -----------------------------------


def test_runner_seals_malformed_refs_normalised_and_verifying():
    out = [{**_ref(), "title": 42, "injected": "x"}, "junk",
           {"title": ["nested"], "url": "https://x",
            "snippet": "s", "source": "stub"}, _ref(7)]
    record = _run_with_refs(out)
    refs = _diagnosis(record)["references"]
    assert refs == [{"title": "42", "url": "https://example.com/0",
                     "snippet": "snippet", "source": "stub"}, _ref(7)]
    # The record VerifyGate itself emitted must verify (the bug).
    assert verify_chain(record)


def test_runner_caps_a_flood_of_wellformed_refs():
    record = _run_with_refs([_ref(i) for i in range(500)])
    assert len(_diagnosis(record)["references"]) == MAX_REFERENCES
    assert verify_chain(record)


def test_runner_non_list_return_degrades_with_a_sealed_note():
    record = _run_with_refs("oops")
    diagnosis = _diagnosis(record)
    assert diagnosis["references"] == []
    assert "not a list" in diagnosis["reference_error"]
    assert verify_chain(record)


def test_runner_none_return_is_simply_no_references():
    record = _run_with_refs(None)
    assert _diagnosis(record)["references"] == []
    assert verify_chain(record)


def test_mock_lookup_still_seals_its_one_reference():
    spec = parse_spec(json.loads(
        (APP / "samples" / "spec.json").read_text(encoding="utf-8")))
    record = run_verification(
        spec=spec, candidate_dir=APP / "samples" / "failing",
        mode="judge", llm=MockLLMAdapter(),
        references=MockReferenceLookup(),
        spec_label="samples/spec.json", candidate_label="samples/failing")
    refs = _diagnosis(record)["references"]
    assert len(refs) == 1 and refs[0]["source"] == "mock"
    assert verify_chain(record)


# -- the evidence mirror ---------------------------------------------------


def test_record_at_the_reference_ceiling_verifies():
    record = _record_with_refs([_ref(i) for i in range(MAX_REFERENCES)])
    assert verify_chain(record)
    assert document_ok(verify_document(record))


def test_record_above_the_reference_ceiling_fails_verification():
    record = _record_with_refs(
        [_ref(i) for i in range(MAX_REFERENCES + 1)])
    # Honestly sealed by build_record — the structure mirror rejects
    # a shape no real run can produce.
    assert not verify_chain(record)
    assert not document_ok(verify_document(record))


def test_record_with_an_overlong_reference_field_fails_verification():
    for field, limit in (("title", TITLE_LIMIT), ("url", URL_LIMIT),
                         ("snippet", SNIPPET_LIMIT),
                         ("source", SOURCE_LIMIT)):
        ref = _ref()
        ref[field] = "x" * (limit + 1)
        assert not verify_chain(_record_with_refs([ref])), field
        ref[field] = "x" * limit
        assert verify_chain(_record_with_refs([ref])), field


def test_verify_cli_and_webview_on_an_over_ceiling_record(tmp_path):
    path = tmp_path / "record.json"
    path.write_text(json.dumps(_record_with_refs(
        [_ref(i) for i in range(MAX_REFERENCES + 1)])), encoding="utf-8")
    assert cli.main(["verify", "--record", str(path)]) == 1
    html = render_document(json.loads(path.read_text(encoding="utf-8")))
    assert "CHAIN INVALID" in html


def test_all_committed_fixtures_still_verify():
    fixtures = [
        APP / "evidence_self_check.json",
        APP / "samples" / "evidence_passing.json",
        APP / "samples" / "evidence_failing.json",
        APP / "samples" / "evidence_real_fail.json",
        APP / "samples" / "evidence_real_fail_v2.json",
        APP / "samples" / "evidence_real_repair.json",
        APP / "samples" / "evidence_real_repair_v2.json",
        APP / "samples" / "evidence_real_repair_v3.json",
        APP / "samples-js" / "evidence_passing.json",
        APP / "samples-js" / "evidence_failing.json",
        APP / "samples-js" / "evidence_real_repair.json",
        APP / "samples-js" / "evidence_real_repair_v3.json",
    ]
    for path in fixtures:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert document_ok(verify_document(data)), path
