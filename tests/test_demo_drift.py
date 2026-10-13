"""Rendered-artifact drift guard (v1.104.0).

The committed HTML pages are renders of committed JSON records, made
by ``verifygate view``. Nothing re-rendered them when the renderer —
or a record field the renderer prints — changed, so the two legacy
chain-v1 demo pages silently went stale: they printed the patched
attempt's pre-v1.3.0 replay string, a ``verifygate run`` command
pointing at a deleted ``/tmp/verifygate-repair-…`` workspace, even
though the records themselves had carried the corrected, executable
``verifygate replay …`` command since v1.3.0. A judge following the
hosted demo's Replay card hit a dead command.

These tests pin every committed render to a fresh render of its
source record, produced exactly the way the CLI produces it, so a
future renderer or record change that is not followed by a
re-render fails the suite instead of shipping a stale page.
"""

import json
from pathlib import Path

from verifygate.jsonio import (
    MAX_RECORD_FILE_BYTES,
    load_json_file,
    replace_lone_surrogates,
)
from verifygate.webview import render_document

ROOT = Path(__file__).resolve().parent.parent

# (committed page, source record it is rendered from)
RENDERED_PAGES = (
    ("demo/evidence_real_repair.html", "samples/evidence_real_repair.json"),
    ("demo/evidence_js_real_repair.html", "samples-js/evidence_real_repair.json"),
    ("demo/evidence_real_repair_v3.html", "samples/evidence_real_repair_v3.json"),
    ("demo/evidence_js_real_repair_v3.html", "samples-js/evidence_real_repair_v3.json"),
    ("samples/evidence_view.html", "samples/evidence_real_repair.json"),
)


def _fresh_render(record_rel: str) -> str:
    data = load_json_file(
        ROOT / record_rel, max_bytes=MAX_RECORD_FILE_BYTES, kind="record"
    )
    return replace_lone_surrogates(render_document(data))


def test_committed_pages_match_fresh_renders():
    for page_rel, record_rel in RENDERED_PAGES:
        committed = (ROOT / page_rel).read_text(encoding="utf-8")
        fresh = _fresh_render(record_rel)
        assert committed == fresh, (
            f"{page_rel} is stale: re-render it from {record_rel} "
            "with `verifygate view` and commit the result"
        )


def test_no_demo_page_prints_a_dead_replay_command():
    # The Replay card is the instruction a reader copy-pastes; a
    # patched attempt's command must be the executable replay form,
    # never a run against a deleted temp workspace. (Sealed check
    # outputs may legitimately mention /tmp paths — they are the
    # historical record of where the checks ran, not instructions.)
    for page_rel, _ in RENDERED_PAGES:
        html = (ROOT / page_rel).read_text(encoding="utf-8")
        assert "--candidate /tmp/" not in html, page_rel
        assert "verifygate replay" in html, page_rel


def test_render_sources_are_the_committed_records():
    # Guard the mapping itself: every source must be a VerifyGate
    # document, so a renamed record fails here rather than silently
    # dropping a page from the drift check.
    for _, record_rel in RENDERED_PAGES:
        data = json.loads((ROOT / record_rel).read_text(encoding="utf-8"))
        assert data.get("tool") == "verifygate", record_rel
