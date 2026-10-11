"""VerifyGate CLI.

Usage:
  python3 -m verifygate run --spec samples/spec.json \
      --candidate samples/passing --mode judge --out evidence-passing.json
  python3 -m verifygate repair --spec samples/spec.json \
      --candidate samples/failing --mode judge --max-rounds 3
  python3 -m verifygate verify --record evidence.json \
      --spec samples/spec.json --candidate samples/passing
  python3 -m verifygate replay --record repair-report.json \
      --spec samples/spec.json --candidate samples/failing

Exit code: run/repair — 0 when the (final) verdict is PASS, 1 when
FAIL, 2 on usage/spec errors (the evidence record is written either
way — a FAIL record is a deliverable, not an error); verify/replay —
0 when the record is intact / every verdict reproduces, 1 when not,
2 on usage errors.

Record files are loaded through the bounded JSON loader
(``jsonio.load_json_file``, ``MAX_RECORD_FILE_BYTES``; v1.36.0): a
record is an untrusted third-party artifact, so an oversize file is
a usage error (exit 2) rejected from its first bytes, not a memory
exhaustion inside the verifier asked to judge it.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import __version__
from .jsonio import MAX_RECORD_FILE_BYTES, load_json_file
from .mock_llm import MockLLMAdapter
from .replay import replay_document, replay_ok
from .repair import MAX_ROUNDS, run_repair
from .runner import run_verification, write_record
from .sandbox import _IGNORE
from .spec import SpecError, load_spec
from .tavily import MockReferenceLookup, TavilyReferenceLookup
from .verify import document_ok, verify_document
from .webview import render_document


def _adapters(mode: str, model: str | None = None):
    # The mode is validated, never assumed (v1.38.0): before this,
    # any non-"judge" value silently selected the real adapters —
    # including the replay path's mode, which comes from a record's
    # own (untrusted) header — so a bogus mode could trigger real,
    # paid Token Factory calls instead of a usage error.
    from .evidence import validate_mode

    validate_mode(mode)
    if mode == "judge":
        return MockLLMAdapter(), MockReferenceLookup()
    from .tokenfactory import TokenFactoryAdapter

    return TokenFactoryAdapter(model=model), TavilyReferenceLookup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verifygate")
    parser.add_argument("--version", action="version",
                        version=f"verifygate {__version__}")
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
                       help=f"maximum patch attempts (default: 3, "
                            f"maximum: {MAX_ROUNDS})")
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
    rpl_p = sub.add_parser(
        "replay",
        help="replay an evidence record / repair report: re-run the "
             "checks (repair attempts are reconstructed from the "
             "recorded patches) and compare verdicts — the checks are "
             "mode-independent, so a real-mode record replays in "
             "Judge Mode with zero keys unless --mode real is given")
    rpl_p.add_argument("--record", required=True,
                       help="evidence record or repair report JSON")
    rpl_p.add_argument("--spec", required=True,
                       help="task spec file the record was sealed against")
    rpl_p.add_argument("--candidate", required=True,
                       help="candidate directory the record was sealed "
                            "against (repair reports: the original, "
                            "unpatched candidate)")
    rpl_p.add_argument("--mode", choices=["judge", "real"], default=None,
                       help="mode for the replay runs (default: the "
                            "record's own mode)")
    rpl_p.add_argument("--model", default=None,
                       help="real mode: Token Factory model id "
                            "(default: NEBIUS_MODEL or Nemotron 3 Nano)")
    args = parser.parse_args(argv)

    if args.command == "replay":
        try:
            data = load_json_file(
                args.record, max_bytes=MAX_RECORD_FILE_BYTES, kind="record")
        except (OSError, ValueError) as exc:
            print(f"record error: {exc}", file=sys.stderr)
            return 2
        try:
            spec = load_spec(args.spec)
        except SpecError as exc:
            print(f"spec error: {exc}", file=sys.stderr)
            return 2
        if not Path(args.candidate).is_dir():
            print(f"record error: candidate directory not found: "
                  f"{args.candidate}", file=sys.stderr)
            return 2
        try:
            from .verify import collect_records, header_of
            first = collect_records(data)[0][1]
        except ValueError as exc:
            print(f"record error: {exc}", file=sys.stderr)
            return 2
        mode = args.mode or header_of(first).get("mode") or "judge"
        try:
            llm, references = _adapters(mode, args.model)
        except ValueError as exc:
            print(f"adapter error: {exc}", file=sys.stderr)
            return 2
        try:
            results = replay_document(
                data, spec=spec, candidate_dir=args.candidate, mode=mode,
                llm=llm, references=references, spec_label=args.spec)
        except (ValueError, OSError) as exc:
            print(f"record error: {exc}", file=sys.stderr)
            return 2
        for r in results:
            chain = "chain OK" if r["chain_ok"] else "CHAIN INVALID"
            spec_part = "spec: match" if r["spec_match"] \
                else "spec: MISMATCH"
            if r["error"]:
                print(f"  {r['label']}: expected {r['expected_verdict']} "
                      f"— {chain}; {spec_part}; REPLAY ERROR: {r['error']}")
                continue
            tree = "candidate tree: match" if r["candidate_match"] \
                else "candidate tree: MISMATCH"
            checks = "checks: match" if r["checks_match"] \
                else "checks: MISMATCH"
            print(f"  {r['label']}: expected {r['expected_verdict']}, "
                  f"replayed {r['actual_verdict']} — {chain}; "
                  f"{tree}; {checks}; {spec_part}")
        if replay_ok(results):
            print(f"REPLAY MATCHED: {len(results)} record(s) reproduced "
                  f"(mode {mode})")
            return 0
        print("REPLAY DID NOT REPRODUCE THE RECORD", file=sys.stderr)
        return 1

    if args.command == "verify":
        try:
            data = load_json_file(
                args.record, max_bytes=MAX_RECORD_FILE_BYTES, kind="record")
        except (OSError, ValueError) as exc:
            print(f"record error: {exc}", file=sys.stderr)
            return 2
        if args.candidate and not Path(args.candidate).is_dir():
            print(f"record error: candidate directory not found: "
                  f"{args.candidate}", file=sys.stderr)
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
            if r.get("report_match") is not None:
                parts.append("report summary: "
                             + ("match" if r["report_match"] else "MISMATCH"))
            print("  " + "; ".join(parts))
        if document_ok(results):
            print(f"RECORD INTACT: {len(results)} record(s) verified")
            return 0
        print("RECORD FAILED VERIFICATION", file=sys.stderr)
        return 1

    if args.command == "view":
        try:
            data = load_json_file(
                args.record, max_bytes=MAX_RECORD_FILE_BYTES, kind="record")
        except (OSError, ValueError) as exc:
            print(f"record error: {exc}", file=sys.stderr)
            return 2
        if not isinstance(data, dict) or data.get("tool") != "verifygate":
            print("record error: not a VerifyGate record", file=sys.stderr)
            return 2
        out = Path(args.out or Path(args.record).with_suffix(".html"))
        try:
            html = render_document(data)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            print(f"record error: cannot render this record: {exc}",
                  file=sys.stderr)
            return 2
        try:
            out.write_text(html, encoding="utf-8")
        except OSError as exc:
            print(f"output error: {exc}", file=sys.stderr)
            return 2
        print(f"Rendered -> {out}")
        return 0

    if args.command == "run":
        try:
            spec = load_spec(args.spec)
        except SpecError as exc:
            print(f"spec error: {exc}", file=sys.stderr)
            return 2
        if not Path(args.candidate).is_dir():
            print(f"candidate error: candidate directory not found: "
                  f"{args.candidate}", file=sys.stderr)
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
        except (OSError, ValueError) as exc:
            # ValueError here is the sandbox's candidate-bounds
            # rejection (CandidateTooLargeError): a usage error, one
            # line, never a traceback.
            print(f"candidate error: {exc}", file=sys.stderr)
            return 2
        out = Path(args.out or f"evidence-{spec.name}.json")
        try:
            write_record(record, out)
        except OSError as exc:
            print(f"output error: {exc}", file=sys.stderr)
            return 2
        checks = [e for e in record["entries"] if e["kind"] == "check"]
        for c in checks:
            print(f"  [{c['verdict']}] {c['name']}: {c['command']} "
                  f"(exit {c['exit_code']}, {c['duration_ms']} ms)")
        print(f"VERDICT: {record['verdict']}  ->  {out}")
        return 0 if record["verdict"] == "PASS" else 1

    if args.command == "repair":
        if not 0 <= args.max_rounds <= MAX_ROUNDS:
            print(f"usage error: --max-rounds must be between 0 and "
                  f"{MAX_ROUNDS}", file=sys.stderr)
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
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            # ValueError covers the sandbox's candidate-bounds
            # rejection, raised by the loop's first verification run.
            print(f"error: {exc}", file=sys.stderr)
            return 2
        out = Path(args.out or f"repair-{spec.name}.json")
        copy_error: OSError | shutil.Error | None = None
        try:
            out.write_text(json.dumps(report, indent=2) + "\n",
                           encoding="utf-8")
        except OSError as exc:
            print(f"output error: {exc}", file=sys.stderr)
            copy_error = exc
        if copy_error is None:
            for a in report["attempts"]:
                patch = a["patch_from_previous"]
                note = ""
                if patch and patch["applied"]:
                    note = (f" (patch applied: "
                            f"{', '.join(patch['changed_files'])})")
                print(f"  attempt {a['round']}: {a['verdict']}{note}")
            if report["last_patch_error"]:
                print(f"  repair stopped: {report['last_patch_error']}")
            if args.patched_out and report["rounds_used"] > 0 \
                    and report["attempts"][-1]["patch_from_previous"]:
                dest = Path(args.patched_out)
                try:
                    shutil.copytree(
                        report["attempts"][-1]["candidate_dir"], dest,
                        dirs_exist_ok=True, ignore=_IGNORE)
                    print(f"  patched candidate copied to {dest}")
                except (OSError, shutil.Error) as exc:
                    print(f"output error: {exc}", file=sys.stderr)
                    copy_error = exc
        # The repair loop keeps its final patched copy for the caller;
        # the CLI has now copied it out (or not been asked to), and the
        # sealed report is self-contained — remove the temp copy so a
        # CLI run leaves nothing behind in the temp filesystem.
        if report["rounds_used"] > 0:
            final_dir = Path(report["attempts"][-1]["candidate_dir"])
            if final_dir.name.startswith("verifygate-"):
                shutil.rmtree(final_dir, ignore_errors=True)
        if copy_error is not None:
            return 2
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
