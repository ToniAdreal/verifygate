"""Evidence-record static web view.

Renders a VerifyGate evidence record (or a full repair report) as one
self-contained HTML page: inline CSS, no JavaScript, no external
resources, so it can be opened from disk, hosted anywhere static, and
archived next to the JSON it came from.

Trust properties are preserved, not just displayed:

  * every string from the record is HTML-escaped before insertion —
    check output is attacker-influenced text (a failing test can print
    anything) and must never become markup;
  * reference URLs are emitted as links only for http/https, anything
    else renders as plain text;
  * the hash chain is *recomputed at render time* via
    ``evidence.verify_chain`` and the page states the result — a
    tampered record renders with a visible CHAIN INVALID banner
    instead of a confident PASS/FAIL badge alone.

The rendering is deterministic apart from the chain-verification
result: same record in, same HTML out.
"""

from __future__ import annotations

import html
import json

from .evidence import verify_chain
from .verify import (attempt_binding_problems, derived_rounds_used,
                    derived_stopped_reason, derived_usage_total,
                    narrative_problems)

_CSS = """
:root{color-scheme:light}
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;margin:0;
 background:#f6f7f9;color:#16181d;line-height:1.5}
main{max-width:960px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:1.6rem;margin:.2em 0}
h2{font-size:1.15rem;margin:1.6em 0 .5em;border-bottom:2px solid #e2e4e9;
 padding-bottom:.25em}
h3{font-size:1rem;margin:1em 0 .4em}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.85em;
 word-break:break-all}
.badge{display:inline-block;padding:.1em .6em;border-radius:999px;
 font-weight:700;font-size:.85em;letter-spacing:.02em}
.pass{background:#d9f2e3;color:#0c5a2e}
.fail{background:#fbdcdc;color:#7d1212}
.neutral{background:#e6e8ec;color:#333}
.card{background:#fff;border:1px solid #e2e4e9;border-radius:10px;
 padding:14px 16px;margin:10px 0}
.verdict-banner{border-radius:10px;padding:16px;margin:14px 0;color:#fff}
.verdict-banner.pass{background:#12753a}
.verdict-banner.fail{background:#a12626}
.verdict-banner .big{font-size:1.5rem;font-weight:800}
.chain-ok{background:#eef7f0;border:1px solid #bfe3cc;border-radius:8px;
 padding:10px 14px;margin:10px 0}
.chain-bad{background:#fdecec;border:2px solid #a12626;border-radius:8px;
 padding:10px 14px;margin:10px 0;font-weight:700;color:#7d1212}
pre{background:#101216;color:#e8eaed;border-radius:8px;padding:10px 12px;
 overflow-x:auto;white-space:pre-wrap;word-break:break-word;font-size:.82rem}
table{border-collapse:collapse;width:100%;font-size:.9rem}
th,td{border:1px solid #e2e4e9;padding:6px 8px;text-align:left;
 vertical-align:top}
th{background:#f0f1f4}
.ref{margin:.3em 0}
.src{font-size:.75em;background:#e6e8ec;border-radius:4px;padding:.05em .4em}
details{margin:.4em 0}
summary{cursor:pointer;font-weight:600;font-size:.9rem}
footer{color:#666;font-size:.85rem;margin-top:40px}
a{color:#0b57a4}
"""


def _e(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _short_hash(value, keep: int = 16) -> str:
    text = str(value or "")
    return text if len(text) <= keep else text[:keep] + "…"


def _safe_link(url: str) -> str:
    """Render an http(s) URL as a link; anything else as plain text."""
    text = str(url or "")
    if text.startswith(("https://", "http://")):
        return (f'<a href="{_e(text)}" rel="noopener noreferrer">'
                f"{_e(text)}</a>")
    return _e(text)


def _verdict_badge(verdict: str) -> str:
    cls = "pass" if verdict == "PASS" else "fail" if verdict == "FAIL" \
        else "neutral"
    return f'<span class="badge {cls}">{_e(verdict)}</span>'


def _render_check(entry: dict) -> str:
    parts = [
        '<div class="card">',
        f"<h3>{_e(entry.get('name', 'check'))} "
        f"{_verdict_badge(entry.get('verdict', ''))}</h3>",
        f'<p class="mono">{_e(entry.get("command", ""))}</p>',
        "<p>exit code: "
        f"<strong>{_e(entry.get('exit_code'))}</strong>"
        f" · duration: {_e(entry.get('duration_ms'))} ms"
        f" · timed out: {_e(bool(entry.get('timed_out')))}</p>",
    ]
    for label, key in (("stdout", "stdout_tail"), ("stderr", "stderr_tail")):
        tail = entry.get(key) or ""
        if tail:
            parts.append(
                f"<details><summary>{label} tail</summary>"
                f"<pre>{_e(tail)}</pre></details>")
    parts.append(
        f'<p class="mono">entry hash: {_e(_short_hash(entry.get("hash")))}'
        "</p></div>")
    return "\n".join(parts)


def _render_diagnosis(entry: dict) -> str:
    parts = [
        '<div class="card">',
        "<h3>Diagnosis <span class=\"src\">commentary — never a verdict "
        "input</span></h3>",
        f"<pre>{_e(entry.get('explanation', ''))}</pre>",
        f"<p>LLM adapter: {_e(entry.get('llm_adapter', ''))}"
        f" · reference source: {_e(entry.get('reference_source', ''))}</p>",
    ]
    if entry.get("diagnosis_error"):
        parts.append(
            "<p>Diagnosis unavailable: "
            f"{_e(entry.get('diagnosis_error'))} "
            "The verdict above does not depend on the diagnosis.</p>")
    if entry.get("reference_error"):
        parts.append(
            "<p>References unavailable: "
            f"{_e(entry.get('reference_error'))} "
            "The verdict above does not depend on references.</p>")
    refs = entry.get("references") or []
    if refs:
        parts.append("<h3>References</h3>")
        for ref in refs:
            parts.append(
                '<p class="ref">'
                f"<strong>{_e(ref.get('title', ''))}</strong> "
                f'<span class="src">{_e(ref.get("source", ""))}</span><br>'
                f"{_safe_link(ref.get('url', ''))}<br>"
                f"{_e(ref.get('snippet', ''))}</p>")
    parts.append("</div>")
    return "\n".join(parts)


def _render_llm_usage(entry: dict) -> str:
    rows = []
    calls = entry.get("calls")
    if not isinstance(calls, list):
        calls = []
    for call in calls:
        # A tampered record can carry a non-object call or a
        # non-object usage; render it as an empty row next to the
        # CHAIN INVALID banner instead of crashing the render.
        if not isinstance(call, dict):
            call = {}
        usage = call.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        rows.append(
            "<tr>"
            f"<td>{_e(call.get('purpose', ''))}</td>"
            f"<td class=\"mono\">{_e(call.get('model', ''))}</td>"
            f"<td>{_e(usage.get('prompt_tokens'))}</td>"
            f"<td>{_e(usage.get('completion_tokens'))}</td>"
            f"<td>{_e(usage.get('total_tokens'))}</td>"
            "</tr>")
    return (
        '<div class="card"><h3>LLM usage '
        '<span class="src">sealed in the hash chain</span></h3>'
        "<table><tr><th>purpose</th><th>model</th><th>prompt</th>"
        "<th>completion</th><th>total</th></tr>"
        + "".join(rows)
        + f"</table><p>Total tokens: "
          f"<strong>{_e(entry.get('total_tokens'))}</strong></p></div>")


def _render_generic(entry: dict) -> str:
    payload = {k: v for k, v in entry.items() if k not in ("kind", "hash")}
    return (
        '<div class="card">'
        f"<h3>{_e(entry.get('kind', 'entry'))}</h3>"
        f"<pre>{_e(json.dumps(payload, indent=2, ensure_ascii=False))}</pre>"
        f'<p class="mono">entry hash: '
        f'{_e(_short_hash(entry.get("hash")))}</p></div>')


_RENDERERS = {
    "check": _render_check,
    "diagnosis": _render_diagnosis,
    "llm_usage": _render_llm_usage,
}


def render_record(record: dict) -> str:
    """Render one evidence record as an HTML fragment (no <html> shell)."""
    verdict = record.get("verdict", "")
    chain_ok = verify_chain(record)
    header = next(
        (e for e in record.get("entries", []) if e.get("kind") == "header"),
        {})
    parts = [
        f'<div class="verdict-banner '
        f'{"pass" if verdict == "PASS" else "fail"}">'
        f'<span class="big">VERDICT: {_e(verdict)}</span><br>'
        f"spec: {_e(header.get('spec_name', ''))}"
        f" · mode: {_e(header.get('mode', record.get('mode', '')))}"
        f" · created: {_e(record.get('created_at', ''))}</div>",
    ]
    if chain_ok:
        parts.append(
            '<div class="chain-ok">Hash chain verified at render time — '
            "this record is intact and tamper-evident.</div>")
    else:
        parts.append(
            '<div class="chain-bad">⚠ HASH CHAIN INVALID — this record '
            "has been modified after sealing. Do not trust its verdict."
            "</div>")
    parts.append(
        '<div class="card"><h3>Replay</h3>'
        f'<p class="mono">{_e(record.get("replay", ""))}</p>'
        f'<p class="mono">spec hash: {_e(header.get("spec_hash", ""))}<br>'
        f"candidate tree hash: "
        f'{_e(header.get("candidate_tree_hash", ""))}<br>'
        f"record hash: {_e(record.get('record_hash', ''))}<br>"
        f"signature: {_e(_short_hash(record.get('signature'), 32))}</p>")
    if record.get("signing_key_is_default_dev_key"):
        parts.append(
            "<p>Signed with the labelled public development key "
            "(demonstrates the mechanism; not production key "
            "management).</p>")
    parts.append("</div>")

    for entry in record.get("entries", []):
        kind = entry.get("kind")
        if kind == "header":
            continue
        renderer = _RENDERERS.get(kind, _render_generic)
        parts.append(renderer(entry))
    return "\n".join(parts)


def render_repair_report(report: dict) -> str:
    """Render a repair report: summary card + one section per attempt."""
    attempts = report.get("attempts", [])
    # The report-level summary fields are not inside any hash chain.
    # The authoritative final verdict is the last attempt's *sealed*
    # record verdict; a stated summary that disagrees with it renders
    # as a visible mismatch, never as the verdict itself. The same
    # treatment covers the quoted figures (v1.8.0): the usage total
    # and rounds used are recomputed from the sealed attempts, and a
    # stated figure that disagrees is flagged, with the derived
    # figure shown instead of the rewritten one.
    sealed_final = None
    if attempts and isinstance(attempts[-1].get("evidence"), dict):
        sealed_final = attempts[-1]["evidence"].get("verdict")
    stated_final = report.get("final_verdict", "")
    final_verdict = sealed_final if sealed_final is not None else stated_final
    derived_total = derived_usage_total(report)
    stated_total = report.get("llm_usage_total")
    total = derived_total if derived_total is not None else (stated_total or {})
    total_mismatch = derived_total is not None \
        and stated_total is not None and stated_total != derived_total
    derived_rounds = derived_rounds_used(report)
    stated_rounds = report.get("rounds_used")
    rounds_shown = derived_rounds if derived_rounds is not None \
        else stated_rounds
    rounds_mismatch = derived_rounds is not None \
        and stated_rounds is not None and stated_rounds != derived_rounds
    # The narrative fields get the same treatment (v1.13.0): why the
    # loop stopped is re-derived from the sealed attempts and shown
    # in place of a rewritten claim, and any narrative disagreement
    # (patch linkage, preserved rejection, round budget, credited
    # adapter) joins the mismatch banner below.
    derived_stop = derived_stopped_reason(report)
    stated_stop = report.get("stopped_reason", "")
    stop_shown = derived_stop if derived_stop is not None else stated_stop
    narrative = narrative_problems(report)
    # Cross-attempt spec binding (v1.14.0): every attempt must have
    # been sealed under the same spec hash — the loop never changes
    # the task, so a report whose attempts name different tasks joins
    # the mismatch banner instead of presenting as one repair.
    binding = attempt_binding_problems(report)
    parts = [
        '<div class="card"><h3>Repair report</h3>'
        f"<p>spec: {_e(report.get('spec_name', ''))}"
        f" · mode: {_e(report.get('mode', ''))}"
        f" · LLM adapter: {_e(report.get('llm_adapter', ''))}</p>"
        f"<p>final verdict: "
        f"{_verdict_badge(final_verdict)}"
        f" · repair rounds used: {_e(rounds_shown)}"
        f" / {_e(report.get('max_rounds'))}"
        f" · stopped: {_e(stop_shown)}</p>",
    ]
    mismatches = []
    if sealed_final is not None and stated_final != sealed_final:
        mismatches.append(
            f"its stated final verdict ({_e(stated_final)}) does not "
            f"match its sealed final attempt record ({_e(sealed_final)})")
    if total_mismatch:
        mismatches.append(
            f"its stated LLM usage total ({_e(stated_total.get('llm_calls'))} "
            f"call(s), {_e(stated_total.get('total_tokens'))} token(s)) does "
            f"not match the sealed attempts ({_e(derived_total['llm_calls'])} "
            f"call(s), {_e(derived_total['total_tokens'])} token(s))")
    if rounds_mismatch:
        mismatches.append(
            f"its stated rounds used ({_e(stated_rounds)}) does not "
            f"match its sealed attempts ({_e(derived_rounds)})")
    if narrative:
        mismatches.append(
            "its repair narrative (stopped reason, patch linkage, "
            "preserved rejection, round budget, or credited adapter) "
            "does not match its sealed attempts: "
            + "; ".join(_e(p) for p in narrative))
    if binding:
        mismatches.append(
            "its attempts were not all sealed under the same spec: "
            + "; ".join(_e(p) for p in binding))
    if mismatches:
        parts.append(
            '<div class="chain-bad">⚠ REPORT SUMMARY MISMATCH — this '
            "report's summary disagrees with its sealed attempt "
            "records: " + "; ".join(mismatches)
            + ". Trust the sealed records.</div>")
    if total.get("llm_calls"):
        parts.append(
            f"<p>LLM usage total: {_e(total.get('llm_calls'))} call(s), "
            f"{_e(total.get('total_tokens'))} total token(s)</p>")
    if report.get("last_patch_error"):
        parts.append(
            f"<p>last patch error: "
            f"{_e(report.get('last_patch_error'))}</p>")
    parts.append("</div>")

    for attempt in attempts:
        evidence = attempt.get("evidence")
        sealed_verdict = evidence.get("verdict") \
            if isinstance(evidence, dict) else None
        heading_verdict = sealed_verdict \
            if sealed_verdict is not None else attempt.get("verdict", "")
        parts.append(
            f"<h2>Attempt {_e(attempt.get('round'))} — "
            f"{_e(heading_verdict)}</h2>")
        if sealed_verdict is not None \
                and attempt.get("verdict") not in (None, sealed_verdict):
            parts.append(
                '<div class="chain-bad">⚠ ATTEMPT SUMMARY MISMATCH — '
                "this attempt's stated verdict "
                f"({_e(attempt.get('verdict'))}) does not match its "
                f"sealed record ({_e(sealed_verdict)}).</div>")
        patch = attempt.get("patch_from_previous")
        if patch:
            if patch.get("applied"):
                parts.append(
                    '<div class="card"><p>Patch from previous attempt '
                    "applied to: "
                    f"<strong>{_e(', '.join(patch.get('changed_files') or []))}"
                    "</strong></p></div>")
            elif patch.get("error"):
                parts.append(
                    '<div class="card"><p>Patch rejected: '
                    f"{_e(patch.get('error'))}</p></div>")
        evidence = attempt.get("evidence")
        if isinstance(evidence, dict):
            parts.append(render_record(evidence))
    return "\n".join(parts)


def render_document(data: dict) -> str:
    """Render a full standalone HTML page for a record or repair report."""
    if data.get("kind") == "repair-report":
        title = f"VerifyGate repair report — {data.get('spec_name', '')}"
        body = render_repair_report(data)
    else:
        header = next(
            (e for e in data.get("entries", [])
             if e.get("kind") == "header"), {})
        title = (f"VerifyGate evidence — "
                 f"{header.get('spec_name', 'record')}")
        body = render_record(data)
    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,'
        'initial-scale=1">\n'
        f"<title>{_e(title)}</title>\n<style>{_CSS}</style>\n</head>\n"
        "<body>\n<main>\n"
        "<h1>VerifyGate — evidence record</h1>\n"
        "<p><em>AI proposes. Deterministic checks dispose. "
        "The evidence record proves it.</em></p>\n"
        f"{body}\n"
        "<footer>Generated by VerifyGate from the sealed JSON record. "
        "Verdicts come only from deterministic checks (exit codes); "
        "LLM text is commentary. Replay the checks with the command "
        "shown on the record to reproduce the verdict.</footer>\n"
        "</main>\n</body>\n</html>\n")
