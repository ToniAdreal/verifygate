"""Deterministic mock LLM adapter — Judge Mode.

Produces stable, template-rendered text derived only from its inputs, so a
zero-key, zero-network run is fully reproducible. It never invents check
outcomes; explanations quote the actual failing commands and outputs.

For the bundled sample task, generate_patch returns the known-good fix as
a real unified diff (CONCEPT.md: in Judge Mode the mock returns the bundled
known-good candidate), so the repair loop can apply it and the validators
— not the mock — decide whether the repair worked. For any other brief it
returns a plan with no diff, and the repair loop stops cleanly.
"""

from __future__ import annotations

from .llm import LLMAdapter

# The known-good slugify fix for the bundled sample, as a unified diff
# against samples/failing/stringtools.py. Deterministic by construction.
_SAMPLE_SLUGIFY_DIFF = '''\
--- a/stringtools.py
+++ b/stringtools.py
@@ -4,9 +4,14 @@
 removes punctuation, so the spec's checks must catch it.
 """
 
+import re
+
 
 def slugify(text: str) -> str:
-    return text.strip().replace(" ", "-")
+    """Lowercase, drop punctuation, join words with single hyphens."""
+    lowered = text.strip().lower()
+    hyphenated = re.sub(r"[^a-z0-9]+", "-", lowered)
+    return hyphenated.strip("-")
 
'''


class MockLLMAdapter(LLMAdapter):
    name = "mock-deterministic"

    def generate_patch(self, brief: str, failing_evidence: dict | None = None) -> str:
        if "slugify" in brief.lower():
            return _SAMPLE_SLUGIFY_DIFF
        lines = [
            "MOCK PATCH PROPOSAL (deterministic, Judge Mode)",
            f"Brief: {brief.strip() or '(no brief)'}",
            "Plan: apply the smallest change that satisfies the brief,",
            "then let the deterministic checks decide whether it worked.",
        ]
        if failing_evidence:
            failed = [c["name"] for c in failing_evidence.get("checks", [])
                      if c.get("verdict") == "FAIL"]
            if failed:
                lines.append(f"Previous failures to address: {', '.join(failed)}")
        return "\n".join(lines)

    def explain_failure(self, check_results: list[dict]) -> str:
        failed = [c for c in check_results if c["verdict"] == "FAIL"]
        if not failed:
            return "All checks passed; no diagnosis required."
        out = ["## Diagnosis (mock, template-rendered from real check output)"]
        for c in failed:
            out.append(f"\n### {c['name']} — `{c['command']}`")
            if c.get("timed_out"):
                out.append(f"Timed out before completing (exit code: none).")
            else:
                out.append(f"Exit code: {c['exit_code']}.")
            tail = (c.get("stderr_tail") or c.get("stdout_tail") or "").strip()
            if tail:
                last = tail.splitlines()[-1] if tail.splitlines() else ""
                out.append(f"Key output line: `{last[:200]}`")
            out.append(
                "Likely cause: the candidate's behaviour diverges from the "
                "spec on this check. Fix the candidate, not the check."
            )
        return "\n".join(out)
