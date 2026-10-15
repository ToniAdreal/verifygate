"""Prompt-snapshot .mjs coverage (v1.115.0).

The repair loop's patch prompt carries a snapshot of the candidate's
small text files (``repair._candidate_files``). JavaScript / TypeScript
module variants were invisible to it: ``.js`` / ``.ts`` are whitelisted
but the snapshot matches the full suffix, so ``discount.mjs`` (and its
``.cjs`` / ``.mts`` / ``.cts`` siblings) matched nothing and the file
to be patched never reached the prompt. On the first live candidate
whose fix landed in a .mjs module the real model named the right file
from the brief, invented its contents as a file creation, and had
every diff correctly rejected (``patch_not_applicable``, 5 real
calls / 30,048 tokens). These tests pin the inclusion, the unchanged
whitelist posture for unlisted/binary files, and an end-to-end repair
whose stub adapter can only build its diff from the snapshot it is
handed.
"""

import shutil

import pytest

from verifygate.evidence import verify_chain
from verifygate.llm import LLMAdapter
from verifygate.repair import (
    _TEXT_SUFFIXES,
    _candidate_files,
    run_repair,
)
from verifygate.spec import parse_spec
from verifygate.tavily import MockReferenceLookup

DISCOUNT_MJS = (
    "export function discountRate(subtotal) {\n"
    "  if (subtotal >= 200) return 0.20;\n"
    "  if (subtotal >= 100) return 0.01;\n"
    "  if (subtotal >= 50) return 0.15;\n"
    "  return 0;\n"
    "}\n"
)

RATES_CJS = (
    "module.exports = { standard: 0.05, express: 0.12 };\n"
)

TYPES_MTS = (
    "export type Tier = \"standard\" | \"express\";\n"
)

LEGACY_CTS = (
    "export const legacyTier: string = \"standard\";\n"
)

CHECKOUT_JS = (
    "import { discountRate } from \"./discount.mjs\";\n"
    "\n"
    "export function total(subtotal) {\n"
    "  const rate = discountRate(subtotal);\n"
    "  return Math.round(subtotal * (1 - rate) * 100) / 100;\n"
    "}\n"
)


def test_module_variants_are_snapshot_suffixes():
    for suffix in (".mjs", ".cjs", ".mts", ".cts"):
        assert suffix in _TEXT_SUFFIXES


def test_snapshot_includes_module_variant_files_verbatim(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "discount.mjs").write_text(DISCOUNT_MJS)
    (cand / "rates.cjs").write_text(RATES_CJS)
    (cand / "types.mts").write_text(TYPES_MTS)
    (cand / "legacy.cts").write_text(LEGACY_CTS)
    (cand / "checkout.js").write_text(CHECKOUT_JS)
    files = _candidate_files(cand)
    assert files["discount.mjs"] == DISCOUNT_MJS
    assert files["rates.cjs"] == RATES_CJS
    assert files["types.mts"] == TYPES_MTS
    assert files["legacy.cts"] == LEGACY_CTS
    assert files["checkout.js"] == CHECKOUT_JS


def test_snapshot_still_skips_unlisted_and_binary_files(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "blob.bin").write_text("plain text, unlisted suffix\n")
    (cand / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff\xfe")
    (cand / "notes").write_text("extensionless, unlisted basename\n")
    (cand / "ok.py").write_text("x = 1\n")
    files = _candidate_files(cand)
    assert files == {"ok.py": "x = 1\n"}


class _SnapshotMjsStub(LLMAdapter):
    """Builds its diff from the snapshot, exactly like a real model must.

    If ``discount.mjs`` is absent from the candidate_files snapshot the
    stub has nothing truthful to diff against and returns no diff --
    the pre-v1.115.0 live failure shape.
    """

    name = "snapshot-mjs-stub"

    def __init__(self):
        self.seen_snapshots = []

    def generate_patch(self, brief, failing_evidence=None):
        files = (failing_evidence or {}).get("candidate_files") or {}
        self.seen_snapshots.append(sorted(files))
        text = files.get("discount.mjs")
        if text is None:
            return "Plan only: the mjs file was not shown to me."
        lines = text.splitlines()
        out = ["--- a/discount.mjs", "+++ b/discount.mjs"]
        for old, new in (
            ("  if (subtotal >= 100) return 0.01;",
             "  if (subtotal >= 100) return 0.10;"),
            ("  if (subtotal >= 50) return 0.15;",
             "  if (subtotal >= 50) return 0.05;"),
        ):
            idx = next(i for i, ln in enumerate(lines) if ln == old)
            out.append(f"@@ -{idx + 1},1 +{idx + 1},1 @@")
            out.append(f"-{old}")
            out.append(f"+{new}")
        return "\n".join(out) + "\n"

    def explain_failure(self, check_results):
        return "stub diagnosis: the mjs discount module has wrong rates"


def _mjs_candidate(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "package.json").write_text('{"type": "module"}\n')
    (cand / "checkout.js").write_text(CHECKOUT_JS)
    (cand / "discount.mjs").write_text(DISCOUNT_MJS)
    (cand / "checkout.test.js").write_text(
        "import { test } from \"node:test\";\n"
        "import assert from \"node:assert/strict\";\n"
        "import { total } from \"./checkout.js\";\n"
        "\n"
        "test(\"bulk\", () => { assert.equal(total(250), 200); });\n"
        "test(\"over100\", () => { assert.equal(total(120), 108); });\n"
        "test(\"over50\", () => { assert.equal(total(80), 76); });\n"
        "test(\"small\", () => { assert.equal(total(30), 30); });\n"
    )
    (cand / "repro.js").write_text(
        "import { total } from \"./checkout.js\";\n"
        "\n"
        "if (total(120) !== 108 || total(80) !== 76) {\n"
        "  throw new Error(\"discount rates are wrong\");\n"
        "}\n"
        "console.log(\"repro ok\");\n"
    )
    return cand


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not available")
def test_mjs_repair_end_to_end_from_snapshot(tmp_path):
    cand = _mjs_candidate(tmp_path)
    spec = parse_spec({
        "name": "checkout-discount-mjs-fix",
        "brief": "Fix discount.mjs so every rate matches the published rules.",
        "checks": [
            {"name": "unit_tests", "command": "node --test",
             "timeout_s": 60},
            {"name": "repro", "command": "node repro.js",
             "timeout_s": 60},
        ],
    })
    stub = _SnapshotMjsStub()
    report = run_repair(
        spec=spec, candidate_dir=cand, mode="judge",
        llm=stub, references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label=str(cand), max_rounds=3,
    )
    assert report["final_verdict"] == "PASS"
    assert report["stopped_reason"] == "passed"
    assert report["rounds_used"] == 1
    # The stub was actually handed the mjs file in its snapshot.
    assert "discount.mjs" in stub.seen_snapshots[0]
    for a in report["attempts"]:
        assert verify_chain(a["evidence"]) is True
    # The original candidate on disk is never modified.
    assert (cand / "discount.mjs").read_text() == DISCOUNT_MJS
