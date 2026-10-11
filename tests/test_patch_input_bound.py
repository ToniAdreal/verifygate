"""Patch-input bound: an applied patch is stored in full, or not applied.

Before v1.37.0 the repair loop applied a proposal of any size but
stored only its first 8,000 characters in the repair report. A
valid 9,413-character diff (the bundled sample's known-good fix
with one added line padded) applied, the candidate PASSed, and
``verify`` reported the report INTACT — yet ``verifygate replay``
could not reconstruct attempt 1 from the truncated fragment
(its tree hash and verdict did not match). The report's central
promise — "replay the recorded patches and confirm the verdict" —
was false for exactly the patches too large to store.

Contract pinned here:
* ``patch.MAX_PATCH_TEXT_CHARS`` (65,536, re-exported by
  ``repair`` — one value for the applier's input ceiling, the
  report's stored cap, and ``verify``'s mirror) is generous:
  every committed real-mode patch sits far below it;
* ``parse_patch`` / ``apply_patch`` reject a text over the
  ceiling — and a non-string text — with ``PatchError`` before
  anything is parsed, staged, or written;
* a valid patch between the old 8,000 cap and the new ceiling
  applies, is stored *in full*, and its report replays;
* an oversize valid proposal is a ``patch_rejected`` rejection
  in the repair loop: attempt 0's sealed FAIL survives, the
  stored fragment is exactly the cap, the error names the
  ceiling, the report verifies, and the original candidate is
  byte-unchanged;
* the rejection is fed back and a following in-bound proposal
  repairs FAIL -> PASS in the next round.
"""

import json
from pathlib import Path

import pytest

from verifygate.mock_llm import MockLLMAdapter
from verifygate.patch import (
    MAX_PATCH_TEXT_CHARS,
    PatchError,
    apply_patch,
    parse_patch,
)
from verifygate.repair import MAX_PATCH_TEXT_CHARS as REPAIR_CAP
from verifygate.repair import run_repair
from verifygate.replay import replay_document, replay_ok
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup
from verifygate.verify import document_ok, verify_document

APP = Path(__file__).resolve().parent.parent
SPEC = load_spec(APP / "samples" / "spec.json")
FAILING_DIR = APP / "samples" / "failing"
COMMITTED_REPORTS = [
    APP / "samples" / "evidence_real_repair.json",
    APP / "samples" / "evidence_real_repair_v2.json",
    APP / "samples" / "evidence_real_repair_v3.json",
    APP / "samples-js" / "evidence_real_repair.json",
    APP / "samples-js" / "evidence_real_repair_v3.json",
]


def _padded_fix(pad_chars: int) -> str:
    """The sample's known-good diff with one added line padded.

    The padding sits inside an added docstring line, so the diff
    stays valid and the patched candidate still passes — only its
    total length changes.
    """
    base = MockLLMAdapter().generate_patch(SPEC.brief, {"candidate_files": {}})
    return base.replace(
        "join words with single hyphens.",
        "join words with single hyphens. " + "x" * pad_chars)


# -- the ceiling itself -------------------------------------------------------


def test_ceiling_is_one_generous_shared_value():
    assert MAX_PATCH_TEXT_CHARS == 65_536
    assert REPAIR_CAP == MAX_PATCH_TEXT_CHARS
    for path in COMMITTED_REPORTS:
        report = json.loads(path.read_text(encoding="utf-8"))
        for attempt in report["attempts"]:
            patch = attempt.get("patch_from_previous")
            if patch and patch.get("patch_text"):
                assert len(patch["patch_text"]) < MAX_PATCH_TEXT_CHARS // 10


# -- the applier rejects before parsing ---------------------------------------


def test_oversize_patch_text_is_a_patch_error(tmp_path):
    with pytest.raises(PatchError) as excinfo:
        apply_patch(tmp_path, "x" * (MAX_PATCH_TEXT_CHARS + 1))
    assert "exceeds the maximum" in str(excinfo.value)
    assert list(tmp_path.iterdir()) == []


def test_oversize_parse_is_a_patch_error():
    with pytest.raises(PatchError):
        parse_patch("x" * (MAX_PATCH_TEXT_CHARS + 1))


def test_non_string_patch_text_is_a_patch_error(tmp_path):
    for bad in (None, 123, b"--- a/x\n"):
        with pytest.raises(PatchError):
            apply_patch(tmp_path, bad)


def test_patch_text_exactly_at_the_ceiling_parses(tmp_path):
    # A creation diff padded with trailing prose to exactly the cap:
    # prose is ignored by the parser, so this both parses and applies.
    diff = "--- /dev/null\n+++ b/a.txt\n@@ -0,0 +1 @@\n+hi\n"
    text = diff + "p" * (MAX_PATCH_TEXT_CHARS - len(diff))
    assert len(text) == MAX_PATCH_TEXT_CHARS
    assert apply_patch(tmp_path, text) == ["a.txt"]


# -- in-bound large patches apply, store in full, and replay ------------------


class OnePatchLLM(MockLLMAdapter):
    name = "stub-one-patch"

    def __init__(self, patch_text: str):
        self._patch_text = patch_text

    def generate_patch(self, brief, failing_evidence=None):
        return self._patch_text


def _repair(llm, max_rounds=1):
    return run_repair(
        spec=SPEC, candidate_dir=FAILING_DIR, mode="judge", llm=llm,
        references=MockReferenceLookup(), spec_label="samples/spec.json",
        candidate_label="samples/failing", max_rounds=max_rounds)


def test_patch_above_the_old_cap_applies_stored_in_full_and_replays():
    patch_text = _padded_fix(9_000)
    assert 8_000 < len(patch_text) < MAX_PATCH_TEXT_CHARS
    report = _repair(OnePatchLLM(patch_text))
    assert report["final_verdict"] == "PASS"
    stored = report["attempts"][-1]["patch_from_previous"]
    assert stored["applied"] is True
    assert stored["patch_text"] == patch_text  # in full, not truncated
    assert document_ok(verify_document(report))
    results = replay_document(
        report, spec=SPEC, candidate_dir=FAILING_DIR, mode="judge",
        llm=MockLLMAdapter(), references=MockReferenceLookup(),
        spec_label="samples/spec.json")
    assert replay_ok(results)


# -- oversize proposals are rejections, not silent truncations -----------------


def test_oversize_valid_proposal_is_rejected_and_report_survives():
    patch_text = _padded_fix(MAX_PATCH_TEXT_CHARS)
    assert len(patch_text) > MAX_PATCH_TEXT_CHARS
    before = (FAILING_DIR / "stringtools.py").read_bytes()
    report = _repair(OnePatchLLM(patch_text))
    assert report["final_verdict"] == "FAIL"
    assert report["stopped_reason"] == "patch_not_applicable"
    assert len(report["attempts"]) == 1  # attempt 0's FAIL record intact
    failed = report["failed_patch"]
    assert failed["error_kind"] == "patch_rejected"
    assert "exceeds the maximum" in failed["error"]
    assert len(failed["patch_text"]) == MAX_PATCH_TEXT_CHARS
    assert document_ok(verify_document(report))
    assert (FAILING_DIR / "stringtools.py").read_bytes() == before


class OversizeThenFixLLM(MockLLMAdapter):
    name = "stub-oversize-then-fix"

    def __init__(self):
        self.calls = 0

    def generate_patch(self, brief, failing_evidence=None):
        self.calls += 1
        if self.calls == 1:
            assert failing_evidence is not None
            assert "patch_error" not in failing_evidence
            return _padded_fix(MAX_PATCH_TEXT_CHARS)
        # The size rejection is fed back before the retry.
        assert "exceeds the maximum" in (failing_evidence or {}).get(
            "patch_error", "")
        return MockLLMAdapter().generate_patch(brief, failing_evidence)


def test_oversize_rejection_is_fed_back_and_a_smaller_diff_repairs():
    llm = OversizeThenFixLLM()
    report = _repair(llm, max_rounds=2)
    assert llm.calls == 2
    assert report["final_verdict"] == "PASS"
    assert report["rounds_used"] == 2
    assert document_ok(verify_document(report))
    results = replay_document(
        report, spec=SPEC, candidate_dir=FAILING_DIR, mode="judge",
        llm=MockLLMAdapter(), references=MockReferenceLookup(),
        spec_label="samples/spec.json")
    assert replay_ok(results)
