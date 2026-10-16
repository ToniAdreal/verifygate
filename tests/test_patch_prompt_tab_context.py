"""Tab-context patch prompt guidance (v1.125.0).

The generate_patch prompt taught the diff format with a worked
example whose context lines start with a space, but never said the
space is a marker distinct from the file's own indentation. On the
first live task whose fix landed in a tab-indented file — an XML
property list rate card (v1.124.0) — Nemotron Nano's patch contents
were right, but its hunks reused the file's leading tab as the diff
context prefix instead of the required leading space, so each hunk
line carried no marker, read as an empty line, and was rightly
rejected (the hunk body then carries fewer lines than its header
declares). The run ended patch_not_applicable after 5 calls /
27,951 tokens; Super 120B repaired the same task in 1 round.

The prompt now teaches the rule explicitly: a context line is one
space (the marker), then the tab (file content), then the line
exactly as it appears; a removed line is '-' first, then the
tab-indented content. These tests pin that guidance in the captured
prompt, pin the applier rejection that makes the guidance necessary,
and prove end-to-end (fake transport, zero network) that a model
following it — returning a verbatim space-plus-tab diff — repairs a
tab-indented plist task FAIL -> PASS while the original candidate
stays byte-unchanged.
"""

import json
from pathlib import Path

import pytest

from verifygate.evidence import verify_chain
from verifygate.patch import PatchError, apply_patch, parse_patch
from verifygate.repair import run_repair
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup
from verifygate.tokenfactory import TokenFactoryAdapter

PLIST = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<plist version="1.0">\n'
    "<dict>\n"
    "\t<key>SILVER</key>\n"
    "\t<real>0.03</real>\n"
    "</dict>\n"
    "</plist>\n"
)

PRICING = (
    "import plistlib\n"
    "\n"
    "\n"
    "def total(subtotal):\n"
    "    with open('rates.plist', 'rb') as fh:\n"
    "        rates = plistlib.load(fh)\n"
    "    rate = rates['SILVER'] if subtotal >= 100 else 0\n"
    "    return round(subtotal * (1 - rate), 2)\n"
)

CHECKOUT_TEST = (
    "from pricing import total\n"
    "\n"
    "assert total(100) == 90.0, total(100)\n"
    "assert total(40) == 40.0, total(40)\n"
    "print('checkout ok')\n"
)

# The correct fix, written the way the guidance teaches: every
# context line is one space (the marker) followed by the file's own
# tab-indented content; the removed line is '-' then the tab content.
GOOD_DIFF = (
    "--- a/rates.plist\n"
    "+++ b/rates.plist\n"
    "@@ -4,3 +4,3 @@\n"
    " \t<key>SILVER</key>\n"
    "-\t<real>0.03</real>\n"
    "+\t<real>0.10</real>\n"
    " </dict>\n"
)

# The v1.124.0 failure shape: the file's tab used as the context
# prefix, so the context line carries no diff marker at all.
TAB_PREFIX_DIFF = (
    "--- a/rates.plist\n"
    "+++ b/rates.plist\n"
    "@@ -4,3 +4,3 @@\n"
    "\t<key>SILVER</key>\n"
    "-\t<real>0.03</real>\n"
    "+\t<real>0.10</real>\n"
    " </dict>\n"
)


def _response(content: str, total: int = 100) -> dict:
    return {
        "model": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
        "choices": [{
            "message": {"content": content},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": total - 40,
            "completion_tokens": 40,
            "total_tokens": total,
        },
    }


def _capture_prompt() -> str:
    bodies: list[dict] = []

    def transport(body):
        bodies.append(body)
        return _response(GOOD_DIFF, total=100)

    adapter = TokenFactoryAdapter(transport=transport)
    adapter.generate_patch(
        "Fix the SILVER rate in rates.plist.",
        {"candidate_files": {"rates.plist": PLIST}},
    )
    return bodies[0]["messages"][-1]["content"]


def test_patch_prompt_teaches_the_tab_context_marker():
    user = _capture_prompt()
    assert "Mind the context-line marker on tab-indented files" in user
    assert "that space is the diff marker, not part of the file" in user
    assert "the tab is file content and comes AFTER" in user
    assert "Never let the file's own tab be the first" in user
    assert "reads as an empty line" in user


def test_patch_prompt_keeps_the_earlier_guidance():
    user = _capture_prompt()
    # The tab rule sits alongside, not instead of, the standing
    # format guidance.
    assert "Reason silently" in user
    assert "Keep hunks small" in user
    assert "CREATE a file" in user
    assert "DELETE a file" in user
    assert "change ONLY a file's permissions" in user


def test_tab_prefixed_context_line_is_rejected(tmp_path):
    target = tmp_path / "rates.plist"
    target.write_text(PLIST)
    with pytest.raises(PatchError):
        apply_patch(tmp_path, parse_patch(TAB_PREFIX_DIFF))
    # Atomicity: the rejected diff changed nothing.
    assert target.read_text() == PLIST


def test_space_plus_tab_diff_repairs_plist_task(tmp_path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    plist_file = candidate / "rates.plist"
    plist_file.write_text(PLIST)
    (candidate / "pricing.py").write_text(PRICING)
    (candidate / "checkout_test.py").write_text(CHECKOUT_TEST)
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps({
        "name": "silver-rate-plist-fix",
        "brief": (
            "pricing.py (correct; do not edit it) charges subtotal "
            ">= 100 with the SILVER rate from rates.plist applied. "
            "The published SILVER rate is 10% off. Fix rates.plist."
        ),
        "checks": [
            {"name": "unit_tests",
             "command": "python3 checkout_test.py", "timeout_s": 30},
        ],
    }))
    spec = load_spec(spec_file)

    prompts: list[str] = []

    def transport(body):
        user = body["messages"][-1]["content"]
        if "unified diff" in user:
            prompts.append(user)
            return _response(GOOD_DIFF, total=100)
        return _response(
            "Diagnosis: the SILVER rate in rates.plist is wrong.",
            total=100)

    adapter = TokenFactoryAdapter(transport=transport)
    report = run_repair(
        spec=spec, candidate_dir=candidate, mode="real",
        llm=adapter, references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label="candidate",
        max_rounds=3,
    )
    assert [a["verdict"] for a in report["attempts"]] == ["FAIL", "PASS"]
    assert report["final_verdict"] == "PASS"
    patch = report["attempts"][1]["patch_from_previous"]
    assert patch["applied"] is True
    assert patch["changed_files"] == ["rates.plist"]
    for a in report["attempts"]:
        assert verify_chain(a["evidence"]) is True
    # The model was shown the tab-indented file verbatim (the
    # v1.124.0 snapshot) and the tab guidance in the same prompt.
    assert "\t<real>0.03</real>" in prompts[0]
    assert "Mind the context-line marker" in prompts[0]
    # The patched copy carries the fix; the original is unchanged.
    patched = Path(report["attempts"][1]["candidate_dir"])
    assert "\t<real>0.10</real>" in (patched / "rates.plist").read_text()
    assert plist_file.read_text() == PLIST
