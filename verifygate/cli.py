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

Malformed documents (v1.47.0): the library consumers (verify /
replay / view) now guard the document shapes they read, so a
record whose ``entries`` was rewritten into a non-list or a list
of non-objects fails cleanly — ``verify`` exit 1 with CHAIN
INVALID, ``replay`` exit 1 (not reproduced), ``view`` rendering
the page next to its CHAIN INVALID banner — instead of an
unhandled traceback (``verify`` / ``replay`` exited 1 *with* a
traceback before this, indistinguishable from a crash). The
``verify`` / ``replay`` handlers additionally catch the shape
errors (``TypeError`` / ``AttributeError`` / ``KeyError``) as a
backstop, mapping any residual malformed shape to the one-line
record error (exit 2), exactly as the ``view`` handler has since
v1.4.0.

Output-destination protection (v1.48.0): no output an invocation
writes may destroy an artifact that same invocation reads. Before
this, ``run`` / ``repair`` with ``--out`` naming the task spec
overwrote the spec with the evidence record it had just read,
``view`` with ``--out`` naming its record (including the default
output for a record file named ``*.html``, which *is* the record
path) replaced the evidence record with its own HTML rendering,
and ``repair --patched-out`` naming the original candidate — the
directory a repair exists to leave byte-unchanged — merged the
patched tree over it via ``copytree(dirs_exist_ok=True)`` and
reported success. Every output destination is now checked,
resolved (symlinks included), before any check runs or any file
is written: a file output may not be the spec file, the record
file, or the candidate directory itself, and ``--patched-out``
may not equal, contain, or sit inside the candidate tree, nor
equal the spec file or the repair report's own ``--out`` path.
A conflict is the CLI contract's one-line usage error (exit 2)
and nothing is written. Writing a *new* file inside a candidate
stays allowed — the documented dogfood command writes its
self-check record at the candidate's root.

Supplied-but-empty optional arguments (v1.50.0): the CLI told
"not supplied" apart from "supplied" by truthiness, so an
explicitly empty value was silently swapped for the default —
the falsy-swap class closed for the adapters' transport
(v1.44.0) and model id (v1.49.0), surviving at the argument
layer. ``run`` / ``repair`` / ``view`` with ``--out ""`` wrote
the *default* output path instead of the one the caller named,
``repair --patched-out ""`` was silently ignored, and
``verify --spec ""`` verified with no spec binding at all and
reported RECORD INTACT — a caller who believed they had bound
the record to a spec had bound it to nothing. An empty string
is now a usage error (exit 2) for every one of these, exactly
like the empty values the loaders and the library entry points
already rejected for ``--spec`` / ``--candidate`` /
``--record`` / ``--model``: only an omitted argument (``None``)
means "use the default / no binding".

Special-file outputs (v1.74.0): every file this CLI writes —
the evidence record, the repair report, the rendered HTML
view — now goes through ``jsonio.write_text_file``, the one
write path that stats its destination before opening it: an
existing destination that is not a regular file (a FIFO,
which blocked the write indefinitely after the verdict was
already decided; a device such as ``/dev/null``, which
silently discarded the document while the CLI reported it
written) is a one-line output error (exit 2), the mirror of
the v1.73.0 special-file rejection on the input side. And
``repair --patched-out`` judges its copy destination before
any check runs or any model call is made: an existing
destination that is not a directory is an output error,
instead of a repair that runs to completion — in real mode,
spending Token Factory calls — and only then fails at the
copy.

Hardlink-alias outputs (v1.86.0): the v1.48.0 protection compared
*paths*, resolved — but two different paths can name one file.
A ``--out`` given as a hardlink alias of the task spec (or, for
``view``, of the record) passed every path comparison and then
overwrote the spec/record through the alias: same destruction,
different spelling. Identity is therefore also judged by inode
(``st_dev`` / ``st_ino``, following symlinks): an output that
*is* the spec or the record file, under any name, is the same
usage error. The patched-out copy had the same hole one level
down: an existing destination directory holding a file that is
a hardlink to (or a symlink at) a candidate file passed the
tree-overlap check — which judges only the destination root —
and ``copytree`` then wrote the patched file *through* the
alias, modifying the original candidate the repair exists to
leave byte-unchanged. The destination tree is now scanned
before any work: any entry inside it whose followed inode is
the spec file, the report path, or any entry of the candidate
tree is an output error.

Patched-out write-through (v1.88.0): the v1.86.0 scan protects
the artifacts this invocation reads — the candidate, the spec,
the report. It does not protect *anyone else's* file. A
destination entry on a path the copy will actually write that
is a symlink (a file link, or a directory link on the way to a
copied file), or a regular file hardlinked to another name,
passed the scan whenever its target was an unrelated file —
and ``copytree`` then wrote the patched file *through* it:
the file at the other end of the link, outside the destination
tree, came back holding patched content while the CLI reported
a successful copy (both shapes reproduced against v1.87.0).
Immediately before the copy — the first moment the patched
tree, and therefore the set of paths the copy will write, is
known — those paths are scanned in the destination: a symlink
component on a written path, or a multiply-linked regular
file at a written path, is an output error (exit 2) and
nothing is copied. Entries the copy will not touch are not
judged (an unrelated alias on an unwritten path stays the
caller's business, exactly as v1.86.0 pinned), a plain
pre-existing file on a written path is still overwritten as
before, and the destination root itself is the caller's own
choice, followed the way file outputs follow it.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
from pathlib import Path

from . import __version__
from .jsonio import MAX_RECORD_FILE_BYTES, load_json_file, write_text_file
from .mock_llm import MockLLMAdapter
from .replay import replay_document, replay_ok
from .repair import MAX_ROUNDS, run_repair
from .runner import run_verification, write_record
from .sandbox import _IGNORE, remove_tree
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


def _resolved(path) -> Path:
    """Resolve ``path`` (symlinks included, non-strict)."""
    return Path(path).resolve()


def _inode(path) -> tuple[int, int] | None:
    """The (device, inode) identity of ``path``, following symlinks.

    Hardlink-alias outputs (v1.86.0, see the module docstring):
    path comparison cannot see that two different names are one
    file; the inode can. ``None`` when the path cannot be statted
    (a missing output has no identity to alias yet).
    """
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _same_file(a, b) -> bool:
    """True when ``a`` and ``b`` name the same file (any spelling)."""
    ia, ib = _inode(a), _inode(b)
    return ia is not None and ia == ib


def _file_out_conflict(out: Path, *, spec_path=None, record_path=None,
                       candidate_dir=None) -> str | None:
    """Name the input artifact a file output would destroy, or None.

    Output-destination protection (v1.48.0, see the module
    docstring): a resolved file output may not be the resolved
    spec file, the resolved record file, or the candidate
    directory itself. Deliberately *not* checked: a new file
    inside the candidate tree (the documented dogfood command
    writes its record at the candidate's root).

    Since v1.86.0 identity is also judged by inode, so a hardlink
    alias of the spec/record — a different path naming the same
    file — is the same conflict, not a spelling that slips past.
    """
    resolved_out = _resolved(out)
    for label, candidate_path in (
            ("the task spec", spec_path),
            ("the evidence record", record_path),
            ("the candidate directory", candidate_dir)):
        if candidate_path is not None \
                and (resolved_out == _resolved(candidate_path)
                     or _same_file(out, candidate_path)):
            return f"{out} would overwrite {label} ({candidate_path})"
    return None


def _tree_inodes(root) -> dict[tuple[int, int], Path]:
    """Map every entry under ``root`` (root included) by inode.

    Stat follows symlinks, so a link *at* a protected entry
    registers that entry's inode — the identity a later write
    through the link would actually reach.
    """
    root = Path(root)
    inodes: dict[tuple[int, int], Path] = {}
    own = _inode(root)
    if own is not None:
        inodes[own] = root
    if not root.is_dir():
        return inodes
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            entry = Path(dirpath) / name
            ino = _inode(entry)
            if ino is not None:
                inodes.setdefault(ino, entry)
    return inodes


def _patched_out_alias_conflict(patched, *, candidate_dir=None,
                                  spec_path=None,
                                  report_out=None) -> str | None:
    """Name the destination entry aliasing a protected artifact.

    Hardlink-alias outputs (v1.86.0, see the module docstring):
    ``shutil.copytree`` writes each patched file by opening its
    destination path, which follows hardlinks and symlinks — an
    existing destination entry that *is* a candidate file (or
    the spec, or the report path) under another name would be
    written through, modifying the very artifact the copy must
    leave alone. The tree-overlap check judges the destination
    root only; this scan judges every entry inside it, before
    any check runs or any model call is made. A destination that
    does not exist yet has no entries, so it cannot alias.
    """
    patched = Path(patched)
    if not patched.is_dir():
        return None
    protected: dict[tuple[int, int], str] = {}
    for label, path in (("the task spec", spec_path),
                        ("the repair report", report_out)):
        if path is not None:
            ino = _inode(path)
            if ino is not None:
                protected[ino] = f"{label} ({path})"
    if candidate_dir is not None:
        for ino, entry in _tree_inodes(candidate_dir).items():
            protected.setdefault(
                ino, f"the original candidate ({candidate_dir}), "
                     f"via its entry {entry}")
    if not protected:
        return None
    for dirpath, dirnames, filenames in os.walk(patched):
        for name in dirnames + filenames:
            entry = Path(dirpath) / name
            ino = _inode(entry)
            if ino is not None and ino in protected:
                return (f"{entry} is the same file as "
                        f"{protected[ino]}")
    return None


def _patched_out_write_through_conflict(patched, source) -> str | None:
    """Name a destination entry the patched-out copy would write through.

    Patched-out write-through (v1.88.0, see the module docstring):
    ``shutil.copytree`` opens each destination path, following
    symlinks and truncating hardlinked inodes, so an entry the
    copy writes can carry the write outside the destination tree
    to a file that is neither the candidate, the spec, nor the
    report — the v1.86.0 alias scan, which protects only those
    three, cannot see it. Only the paths the copy will actually
    write are judged (the entries of the patched tree, minus the
    copy's own ignore set): for each, every existing destination
    component is ``lstat``-ed — a symlink component anywhere on
    the path, or a multiply-linked regular file at the path
    itself, is a conflict. A component that does not exist yet
    ends the walk for that path: the copy creates it fresh, as a
    plain file inside the destination. The destination root is
    not judged: it is the caller's own chosen path, followed the
    way every file output follows its root.
    """
    patched = Path(patched)
    source = Path(source)
    if not patched.is_dir() or not source.is_dir():
        return None
    rels: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(source):
        base = Path(dirpath)
        ignored = _IGNORE(str(base), list(dirnames) + list(filenames))
        dirnames[:] = [d for d in dirnames if d not in ignored]
        for name in list(dirnames) + list(filenames):
            if name not in ignored:
                rels.append((base / name).relative_to(source))
    for rel in sorted(rels, key=str):
        current = patched
        parts = rel.parts
        for index, part in enumerate(parts):
            current = current / part
            try:
                info = current.lstat()
            except OSError:
                break  # missing component: the copy creates it fresh
            if stat.S_ISLNK(info.st_mode):
                return (f"{current} is a symlink; the copy would "
                        f"write through it, outside the destination "
                        f"tree")
            if index == len(parts) - 1 \
                    and stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                return (f"{current} is hardlinked to another name; "
                        f"the copy would rewrite that file too")
    return None


def _tree_overlap(a: Path, b: Path) -> bool:
    """True when resolved ``a`` equals, contains, or sits inside ``b``."""
    ra, rb = _resolved(a), _resolved(b)
    return ra == rb or ra in rb.parents or rb in ra.parents


def _empty_supplied(value, flag: str) -> str | None:
    """Usage-error text when an optional argument was supplied empty.

    Supplied-but-empty arguments (v1.50.0, see the module
    docstring): ``None`` is the parser's "not supplied" value and
    passes unjudged; an explicit empty string is a usage error,
    never a silent request for the default.
    """
    if value == "":
        return (f"{flag} must not be an empty string; omit it to use "
                f"the default / no binding")
    return None


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
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
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
        except (ValueError, OSError, TypeError, AttributeError,
                KeyError) as exc:
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
        if args.spec is not None:
            # ``is not None``, not truthiness (v1.50.0): an explicit
            # ``--spec ""`` reaches load_spec, whose path validation
            # rejects it as a spec error (exit 2) — it is not a
            # request to verify without a spec binding.
            try:
                spec_hash = load_spec(args.spec).hash
            except SpecError as exc:
                print(f"spec error: {exc}", file=sys.stderr)
                return 2
        try:
            results = verify_document(
                data, candidate_dir=args.candidate, spec_hash=spec_hash)
        except (ValueError, OSError, TypeError, AttributeError,
                KeyError) as exc:
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
        empty = _empty_supplied(args.out, "--out")
        if empty is not None:
            print(f"usage error: {empty}", file=sys.stderr)
            return 2
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
        conflict = _file_out_conflict(out, record_path=args.record)
        if conflict is not None:
            print(f"output error: {conflict}; "
                  f"choose a different --out path", file=sys.stderr)
            return 2
        try:
            html = render_document(data)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            print(f"record error: cannot render this record: {exc}",
                  file=sys.stderr)
            return 2
        try:
            write_text_file(out, html)
        except (OSError, ValueError) as exc:
            print(f"output error: {exc}", file=sys.stderr)
            return 2
        print(f"Rendered -> {out}")
        return 0

    if args.command == "run":
        empty = _empty_supplied(args.out, "--out")
        if empty is not None:
            print(f"usage error: {empty}", file=sys.stderr)
            return 2
        try:
            spec = load_spec(args.spec)
        except SpecError as exc:
            print(f"spec error: {exc}", file=sys.stderr)
            return 2
        if not Path(args.candidate).is_dir():
            print(f"candidate error: candidate directory not found: "
                  f"{args.candidate}", file=sys.stderr)
            return 2
        out = Path(args.out or f"evidence-{spec.name}.json")
        conflict = _file_out_conflict(
            out, spec_path=args.spec, candidate_dir=args.candidate)
        if conflict is not None:
            print(f"output error: {conflict}; "
                  f"choose a different --out path", file=sys.stderr)
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
        try:
            write_record(record, out)
        except (OSError, ValueError) as exc:
            # ValueError is the writer's own validation judgement
            # (v1.59.0): a one-line output error, exit 2, never a
            # traceback — the same contract as every other write.
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
        for flag, value in (("--out", args.out),
                            ("--patched-out", args.patched_out)):
            empty = _empty_supplied(value, flag)
            if empty is not None:
                print(f"usage error: {empty}", file=sys.stderr)
                return 2
        try:
            spec = load_spec(args.spec)
        except SpecError as exc:
            print(f"spec error: {exc}", file=sys.stderr)
            return 2
        out = Path(args.out or f"repair-{spec.name}.json")
        conflict = _file_out_conflict(
            out, spec_path=args.spec, candidate_dir=args.candidate)
        if conflict is not None:
            print(f"output error: {conflict}; "
                  f"choose a different --out path", file=sys.stderr)
            return 2
        if args.patched_out:
            patched = Path(args.patched_out)
            if _tree_overlap(patched, Path(args.candidate)):
                print(f"output error: --patched-out {patched} overlaps "
                      f"the candidate directory ({args.candidate}); "
                      f"the original candidate is never modified — "
                      f"choose a destination outside it",
                      file=sys.stderr)
                return 2
            conflict = _file_out_conflict(
                patched, spec_path=args.spec)
            if conflict is not None:
                print(f"output error: {conflict}; "
                      f"choose a different --patched-out path",
                      file=sys.stderr)
                return 2
            if _resolved(patched) == _resolved(out):
                print(f"output error: --patched-out {patched} is the "
                      f"repair report path ({out}); choose a different "
                      f"--patched-out path", file=sys.stderr)
                return 2
            if patched.exists() and not patched.is_dir():
                # Judged before any work (v1.74.0): the copy
                # destination must be a directory or not exist
                # yet — an existing regular file, FIFO, or device
                # can only fail after the repair has run.
                print(f"output error: --patched-out {patched} already "
                      f"exists and is not a directory; choose a "
                      f"different --patched-out path", file=sys.stderr)
                return 2
            alias = _patched_out_alias_conflict(
                patched, candidate_dir=args.candidate,
                spec_path=args.spec, report_out=out)
            if alias is not None:
                # Judged before any work (v1.86.0): an entry inside
                # the destination that aliases the candidate, the
                # spec, or the report would be written through by
                # the copy, modifying the artifact it aliases.
                print(f"output error: --patched-out {patched} contains "
                      f"an entry that aliases a protected artifact: "
                      f"{alias}; the original candidate is never "
                      f"modified — choose a different --patched-out "
                      f"path", file=sys.stderr)
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
        copy_error: OSError | shutil.Error | ValueError | None = None
        try:
            write_text_file(out, json.dumps(report, indent=2) + "\n")
        except (OSError, ValueError) as exc:
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
                source_tree = Path(report["attempts"][-1]["candidate_dir"])
                through = _patched_out_write_through_conflict(
                    dest, source_tree)
                if through is not None:
                    # Judged before the copy (v1.88.0): an entry on
                    # a path the copy will write that is a symlink
                    # or a multiply-linked file would carry the
                    # write outside the destination tree, to a file
                    # no protection has named. Nothing is copied.
                    print(f"output error: --patched-out {dest}: "
                          f"{through}; choose a different "
                          f"--patched-out path", file=sys.stderr)
                    copy_error = ValueError(through)
                else:
                    try:
                        shutil.copytree(
                            source_tree, dest,
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
                remove_tree(final_dir)
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
