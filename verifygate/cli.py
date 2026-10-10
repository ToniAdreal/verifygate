"""VerifyGate CLI.

Usage:
  python3 -m verifygate run --spec samples/spec.json \
      --candidate samples/passing --mode judge --out evidence-passing.json
  python3 -m verifygate repair --spec samples/spec.json \
      --candidate samples/failing --mode judge --max-rounds 3
  python3 -m verifygate verify --record evidence.json \
      --spec samples/spec.json --candidate samples/passing

Exit code: 0 when the (final) verdict is PASS, 1 when FAIL, 2 on
usage/spec errors. The evidence record is written either way — a FAIL
record is a deliverable, not an error.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .mock_llm import MockLLMAdapter
from .repair import run_repair
from .runner import run_verification, write_record
from .sandbox import _IGNORE
from .spec import SpecError, load_spec
from .tavily import MockReferenceLookup, TavilyReferenceLookup
from .verify import document_ok, verify_document
from .webview import render_document


def _adapters(mode: str, model: str | None = None):
    if mode == "judge":
        return MockLLMAdapter(), MockReferenceLookup()
    from .tokenfactory import TokenFactoryAdapter

    return TokenFactoryAdapter(model=model), TavilyReferenceLookup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verifygate")
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="verify a candidate against a task spec")
    run_p.add_argument("--spec", required=True)
    run_p.add_argument("--candidate", required=True)
    run_p.add_argument("--mode", choices=["judge", "real"], default="judge")
    run_p.add_argument("--model", default=None,
                       help="real mode: Token Factory model id "
                            "(default: NEBIUS_MODEL or Nemotron 3 Nano)")
    run_p.add_argument("--out", default=None,
                       help="evidence record path (default: evidence-<spec>.json)")
    rep_p = sub.add_parser(
        "repair", help="verify, then loop: LLM patch -> apply -> re-verify")
    rep_p.add_argument("--spec", required=True)
    rep_p.add_argument("--candidate", required=True)
    rep_p.add_argument("--mode", choices=["judge", "real"], default="judge")
    rep_p.add_argument("--model", default=None,
                       help="real mode: Token Factory model id "
                            "(default: NEBIUS_MODEL or Nemotron 3 Nano)")
    rep_p.add_argument("--max-rounds", type=int, default=3,
                       help="maximum patch attempts (default: 3)")
    rep_p.add_argument("--out", default=None,
                       help="repair report path (default: repair-<spec>.json)")
    rep_p.add_argument("--patched-out", default=None,
                       help="copy the final patched candidate into this directory")
    view_p = sub.add_parser(
        "view", help="render an evidence record / repair report as HTML")
    view_p.add_argument("--record", required=True,
                        help="evidence record or repair report JSON")
    view_p.add_argument("--out", default=None,
                        help="HTML path (default: <record stem>.html)")
    ver_p = sub.add_parser(
        "verify",
        help="verify an evidence record / repair report: hash chain, "
             "signature, and optionally its spec/candidate bindings — "
             "no checks are re-run, no keys or network needed")
    ver_p.add_argument("--record", required=True,
                       help="evidence record or repair report JSON")
    ver_p.add_argument("--candidate", default=None,
                       help="candidate directory: its tree hash must match "
                            "the record header (repair reports: attempt 0)")
    ver_p.add_argument("--spec", default=None,
                       help="task spec file: its hash must match the "
                            "record header")
    args = parser.parse_args(argv)

    if args.command == "verify":
        try:
            data = json.loads(Path(args.record).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"record error: {exc}", file=sys.stderr)
            return 2
        spec_hash = None
        if args.spec:
            try:
                spec_hash = load_spec(args.spec).hash
            except SpecError as exc:
                print(f"spec error: {exc}", file=sys.stderr)
                return 2
        try:
            results = verify_document(
                data, candidate_dir=args.candidate, spec_hash=spec_hash)
        except (ValueError, OSError) as exc:
            print(f"record error: {exc}", file=sys.stderr)
            return 2
        for label, r in results:
            chain = "chain OK" if r["chain_ok"] else "CHAIN INVALID"
            parts = [f"{label}: verdict {r['verdict']}, mode {r['mode']}, "
                     f"spec {r['spec_name']} — {chain}"]
            if r["candidate_match"] is not None:
                parts.append("candidate: "
                             + ("match" if r["candidate_match"] else "MISMATCH"))
            elif args.candidate:
                parts.append("candidate: not bound (patched copy)")
            if r["spec_match"] is not None:
                parts.append("spec: "
                             + ("match" if r["spec_match"] else "MISMATCH"))
            print("  " + "; ".join(parts))
        if document_ok(results):
            print(f"RECORD INTACT: {len(results)} record(s) verified")
            return 0
        print("RECORD FAILED VERIFICATION", file=sys.stderr)
        return 1

    if args.command == "view":
        try:
            data = json.loads(Path(args.record).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"record error: {exc}", file=sys.stderr)
            return 2
        if not isinstance(data, dict) or data.get("tool") != "verifygate":
            print("record error: not a VerifyGate record", file=sys.stderr)
            return 2
        out = Path(args.out or Path(args.record).with_suffix(".html"))
        out.write_text(render_document(data), encoding="utf-8")
        print(f"Rendered -> {out}")
        return 0

    if args.command == "run":
        try:
            spec = load_spec(args.spec)
        except SpecError as exc:
            print(f"spec error: {exc}", file=sys.stderr)
            return 2
        try:
            llm, references = _adapters(args.mode, args.model)
        except ValueError as exc:
            print(f"adapter error: {exc}", file=sys.stderr)
            return 2
        try:
            record = run_verification(
                spec=spec,
                candidate_dir=args.candidate,
                mode=args.mode,
                llm=llm,
                references=references,
                spec_label=args.spec,
                candidate_label=args.candidate,
            )
        except RuntimeError as exc:
            print(f"adapter error: {exc}", file=sys.stderr)
            return 2
        out = Path(args.out or f"evidence-{spec.name}.json")
        write_record(record, out)
        checks = [e for e in record["entries"] if e["kind"] == "check"]
        for c in checks:
            print(f"  [{c['verdict']}] {c['name']}: {c['command']} "
                  f"(exit {c['exit_code']}, {c['duration_ms']} ms)")
        print(f"VERDICT: {record['verdict']}  ->  {out}")
        return 0 if record["verdict"] == "PASS" else 1

    if args.command == "repair":
        if args.max_rounds < 0:
            print("usage error: --max-rounds must be >= 0", file=sys.stderr)
            return 2
        try:
            spec = load_spec(args.spec)
        except SpecError as exc:
            print(f"spec error: {exc}", file=sys.stderr)
            return 2
        try:
            llm, references = _adapters(args.mode, args.model)
        except ValueError as exc:
            print(f"adapter error: {exc}", file=sys.stderr)
            return 2
        try:
            report = run_repair(
                spec=spec,
                candidate_dir=args.candidate,
                mode=args.mode,
                llm=llm,
                references=references,
                spec_label=args.spec,
                candidate_label=args.candidate,
                max_rounds=args.max_rounds,
            )
        except (FileNotFoundError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        out = Path(args.out or f"repair-{spec.name}.json")
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        for a in report["attempts"]:
            patch = a["patch_from_previous"]
            note = ""
            if patch and patch["applied"]:
                note = f" (patch applied: {', '.join(patch['changed_files'])})"
            print(f"  attempt {a['round']}: {a['verdict']}{note}")
        if report["last_patch_error"]:
            print(f"  repair stopped: {report['last_patch_error']}")
        if args.patched_out and report["rounds_used"] > 0 \
                and report["attempts"][-1]["patch_from_previous"]:
            dest = Path(args.patched_out)
            shutil.copytree(report["attempts"][-1]["candidate_dir"], dest,
                            dirs_exist_ok=True, ignore=_IGNORE)
            print(f"  patched candidate copied to {dest}")
        usage = report.get("llm_usage_total") or {}
        if usage.get("llm_calls"):
            print(f"  LLM usage: {usage['llm_calls']} call(s), "
                  f"{usage['total_tokens']} total token(s) "
                  f"(sealed in the evidence chain)")
        print(f"FINAL VERDICT: {report['final_verdict']} after "
              f"{report['rounds_used']} repair round(s) "
              f"({report['stopped_reason']})  ->  {out}")
        return 0 if report["final_verdict"] == "PASS" else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
