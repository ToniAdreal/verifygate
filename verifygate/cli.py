"""VerifyGate CLI.

Usage:
  python3 -m verifygate run --spec samples/spec.json \
      --candidate samples/passing --mode judge --out evidence-passing.json

Exit code: 0 when the verdict is PASS, 1 when FAIL, 2 on usage/spec errors.
The evidence record is written either way — a FAIL record is a deliverable,
not an error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .mock_llm import MockLLMAdapter
from .runner import run_verification, write_record
from .spec import SpecError, load_spec
from .tavily import MockReferenceLookup, TavilyReferenceLookup


def _adapters(mode: str):
    if mode == "judge":
        return MockLLMAdapter(), MockReferenceLookup()
    from .tokenfactory import TokenFactoryAdapter

    return TokenFactoryAdapter(), TavilyReferenceLookup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verifygate")
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="verify a candidate against a task spec")
    run_p.add_argument("--spec", required=True)
    run_p.add_argument("--candidate", required=True)
    run_p.add_argument("--mode", choices=["judge", "real"], default="judge")
    run_p.add_argument("--out", default=None,
                       help="evidence record path (default: evidence-<spec>.json)")
    args = parser.parse_args(argv)

    if args.command == "run":
        try:
            spec = load_spec(args.spec)
        except SpecError as exc:
            print(f"spec error: {exc}", file=sys.stderr)
            return 2
        try:
            llm, references = _adapters(args.mode)
        except ValueError as exc:
            print(f"adapter error: {exc}", file=sys.stderr)
            return 2
        record = run_verification(
            spec=spec,
            candidate_dir=args.candidate,
            mode=args.mode,
            llm=llm,
            references=references,
            spec_label=args.spec,
            candidate_label=args.candidate,
        )
        out = Path(args.out or f"evidence-{spec.name}.json")
        write_record(record, out)
        checks = [e for e in record["entries"] if e["kind"] == "check"]
        for c in checks:
            print(f"  [{c['verdict']}] {c['name']}: {c['command']} "
                  f"(exit {c['exit_code']}, {c['duration_ms']} ms)")
        print(f"VERDICT: {record['verdict']}  ->  {out}")
        return 0 if record["verdict"] == "PASS" else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
