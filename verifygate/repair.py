"""Repair loop: FAIL evidence -> LLM patch -> apply -> re-verify.

This is the agentic half of VerifyGate, kept on a short deterministic
leash. Each round:

  1. the current candidate is verified (validators decide, as always);
  2. on FAIL, the failing check results are handed to the LLM adapter,
     which *proposes* a unified diff — advisory text, nothing more;
  3. the diff is applied to a fresh copy of the candidate (the original
     on disk is never modified; a diff that does not apply cleanly
     stops the loop rather than being forced in);
  4. the patched copy is verified from scratch.

The loop ends on the first PASS, when no usable patch is produced, or
after ``max_rounds`` patch attempts — whichever comes first. The report
embeds every attempt's full evidence record, so each intermediate
verdict stays independently replayable and tamper-evident.

Replay note: a patched attempt's candidate is an ephemeral temp copy
that the loop (or the CLI) deletes, so its record's ``replay`` field
must not point at that path — it names the ``verifygate replay``
command instead, which reconstructs each patched tree from the
original candidate plus the patches recorded in the report. Since
chain v3 the ``replay`` field is sealed into the record hash, so the
correction goes through ``evidence.update_replay_command`` — the one
sanctioned post-seal edit, which re-seals the record it touches.

Round-budget validation (v1.22.0): ``max_rounds`` is validated the
way spec timeouts have been since v1.7.0 — a real ``int`` (JSON
``true`` is an ``int`` subclass and used to slip through as 1), in
range ``0..MAX_ROUNDS``. Before this, the only check was
``max_rounds < 0``: a bool or float budget produced a report whose
own ``max_rounds`` verification rejects (``verify`` requires a
non-bool int), so the loop happily emitted reports that fail their
own verification; a string or ``None`` crashed the comparison with
an unhandled ``TypeError`` instead of the documented ``ValueError``;
a float budget of 2.5 silently allowed 3 rounds; and there was no
upper bound at all — ``--max-rounds 1000000000`` was accepted
verbatim, each round being a paid Token Factory call plus a full
re-verification, with a full evidence record embedded per attempt.
``verify``'s narrative check mirrors the bound: a report claiming a
budget above ``MAX_ROUNDS`` is not one this loop can have produced.

Patch-result bounds (v1.25.0): a proposal that applies cleanly as
a diff but grows the candidate past the sandbox's candidate size
ceilings is a rejection, exactly like a diff whose context does not
match — ``apply_patch`` raises ``PatchError`` before writing
anything (see ``patch.py``), so it flows through the rejection
path below: preserved as ``failed_patch`` with
``error_kind: patch_rejected``, fed back, and capped by
``max_rounds``, with the report delivered. Before this, the loop
wrote the oversize tree, and re-verifying it raised
``CandidateTooLargeError`` out of ``run_verification`` — not a
``PatchError``, not guarded — so the whole report, attempt 0's
sealed FAIL evidence included, was lost to a CLI usage error.

Patch-report bounds (v1.30.0): the patch linkage this module
writes into a report — the preserved ``patch_text``, the patch
``error`` strings, the ``changed_files`` lists, and each linkage's
``llm_usage`` snapshot — is capped at the source here
(``MAX_PATCH_TEXT_CHARS`` for the stored proposal,
``MAX_PATCH_ERROR_CHARS`` for a stored applier/generation error),
and ``verify``'s narrative check mirrors the caps (plus generous
ceilings over the changed-file lists and the usage snapshots).
Before this, those unchained fields were judged only for type:
inflating a committed report's preserved patch, its rejection
error, its changed-file list, or its usage snapshot to
500,000-character strings still verified INTACT.

Patch-input bound (v1.37.0): the stored cap above was only a
*storage* truncation — the loop applied the proposal in full,
so a valid proposal longer than the cap (then 8,000 characters)
produced a PASS report carrying a truncated fragment of the
patch that actually passed, and replay could not reconstruct
the attempt from the fragment. ``MAX_PATCH_TEXT_CHARS`` is now
defined in ``patch.py`` (re-exported here), raised to a
generous 65,536, and enforced by the applier *before* parsing:
an oversize proposal is a ``PatchError`` rejection — preserved
below truncated to the cap, fed back, retried within
``max_rounds`` — so every applied patch is stored in full and
every report this loop emits replays from its own recorded
patches.

Usage sealing normalisation (v1.35.0): the report-level copies of
adapter-supplied metadata — the credited ``llm_adapter`` name, each
patch linkage's ``llm_usage`` snapshot, and the
``unsealed_llm_usage`` list — are normalised through the same
``evidence`` helpers the runner uses for the sealed copies, so a
report this loop emits passes its own narrative check (which caps
all three) even when an adapter's name or recorded calls are
malformed. See ``evidence.normalise_usage_calls``.

Adapter-surface resilience (v1.66.0): the credited name and every
usage snapshot above come from the adapter's *optional* surface,
which this loop read directly — ``llm.name`` before the first run
(a nameless duck-typed adapter crashed here with an
``AttributeError`` and no report at all, although
``validate_llm`` promises the name stays optional),
``llm.pending_calls()`` around every generation call, and
``llm.drain_calls()`` during report assembly (a raising usage log
destroyed a completed loop's report). All three now go through
the guarded reads in ``llm`` (``getattr`` for the name,
``pending_calls_safely`` / ``drain_calls_safely`` for the log): a
missing, broken, or non-list usage log degrades to an empty
snapshot, and a missing name to the fallback label — the report
is always delivered. See the ``llm`` module docstring.

Generation resilience (v1.20.0): ``llm.generate_patch`` is guarded
exactly like the diagnosis call in the runner. Before this, a
generation failure — a network error, a malformed Token Factory
response, an empty completion after the retry (all ``RuntimeError``
from the real adapter), or a non-string return — propagated out of
the loop: attempt 0's already-decided FAIL evidence was never
returned, the CLI reported an adapter error (exit 2) and wrote no
report at all — the same loss v1.19.0 fixed for the diagnosis, one
call later. A generation failure now ends the loop instead: the
failed round counts as used, the failure is preserved as the
report's ``failed_patch`` with ``error_kind: generation_failed``,
``stopped_reason`` is ``generation_failed``, and the report —
attempt 0's sealed record included — is delivered with final
verdict FAIL (CLI exit 1, not 2). Unlike a rejected diff, a
generation failure is not retried: there is no proposal to give
feedback on, and repeating the identical prompt would only spend
more of the caller's token budget on a failure that may be an
outage or an auth error. Calls the adapter recorded before failing
stay pending and are surfaced as the report's unsealed usage, so
the token spend is not dropped either.

Label validation (v1.40.0): the loop's ``spec_label`` /
``candidate_label`` are validated the same way, beside the mode,
spec, and budget validation — they form every attempt's replay
string (the original candidate's directly, and a patched
attempt's through the ``update_replay_command`` rewrite), so an
invalid label must fail here, before the first verification run
or any generation call, not crash ``quote_label`` after attempt
0's checks have run and lose the whole report. The runner
validates them too; this is the loop's own fail-fast.

Candidate-directory validation (v1.41.0): ``candidate_dir`` is
validated the same way, beside the labels — the loop stores its
string verbatim as every attempt's ``candidate_dir`` and copies
it once per round, so a non-path value crashed ``Path(...)``
here with a ``TypeError``, an empty string silently repaired
the *current* directory's tree, and an over-long path produced
a report whose stored attempt directories exceed the cap
verification enforces over them. The runner validates it too;
this is the loop's own fail-fast, before the first run.

Adapter validation (v1.42.0): ``llm`` and ``references`` are
validated the same way, beside the candidate directory — the
loop reads ``llm.name`` for its credited adapter before the
first run, so ``None`` / a bare object crashed here with an
unhandled ``AttributeError`` and no report at all, while a
malformed lookup crashed the first FAIL attempt the same way.
The runner validates them too; this is the loop's own
fail-fast, before the first run or any generation call.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from . import __version__
from .evidence import (
    normalise_adapter_name,
    normalise_usage_calls,
    quote_label,
    update_replay_command,
    validate_label,
    validate_mode,
)
from .llm import (
    LLMAdapter,
    drain_calls_safely,
    pending_calls_safely,
    validate_llm,
)
from .patch import MAX_PATCH_TEXT_CHARS, PatchError, apply_patch
from .runner import run_verification
from .sandbox import (
    iter_candidate_files,
    prepare_workspace,
    remove_tree,
    validate_candidate_dir,
)
from .spec import TaskSpec, validate_spec
from .tavily import ReferenceLookup, validate_references

# A generous ceiling, not a target: the default is 3, and every round
# costs one LLM patch call plus a full re-verification, with a full
# evidence record embedded per attempt. The bound exists so a typo'd
# or adversarial budget cannot loop (and, in real mode, bill) without
# limit — the same reasoning that caps a check's timeout at 3600s.
MAX_ROUNDS = 10

# Source caps for the patch linkage stored in a repair report
# (v1.30.0): the raw proposal is preserved for debuggability and
# for replay reconstruction, truncated to this many characters;
# a stored patch error (an applier rejection, whose text embeds
# the offending path, or a generation failure) is truncated to
# this many. ``verify`` mirrors both caps — see its docstring.
# Since v1.37.0 the text cap is defined in patch.py and is also
# the applier's input ceiling, so the truncation below can only
# ever cut a *rejected* proposal (oversize, or no diff in it) —
# an applied proposal is within the cap and stored in full.
MAX_PATCH_ERROR_CHARS = 2000


def _check_dicts(record: dict) -> list[dict]:
    return [
        {k: v for k, v in e.items() if k not in ("kind", "hash")}
        for e in record["entries"]
        if e["kind"] == "check"
    ]


_TEXT_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".md", ".txt", ".toml",
    ".yaml", ".yml", ".cfg", ".ini", ".sh", ".go", ".rs", ".java", ".c",
    ".h", ".cpp", ".rb", ".php", ".html", ".css",
    # Data / query / markup text formats (v1.109.0): a candidate whose
    # fix lands in one of these was previously invisible to the patch
    # prompt -- the snapshot silently omitted the very file to be
    # patched, so a real model had to invent its contents and every
    # proposed diff was correctly rejected (proven live on a .sql
    # candidate: Nano and Super both failed patch_not_applicable
    # while their diagnoses were correct).
    ".sql", ".csv", ".tsv", ".xml", ".graphql", ".proto",
    ".lua", ".pl", ".swift", ".kt", ".kts", ".scala", ".groovy",
    # Build-system fragments (v1.110.0).
    ".mk",
    # Java-style configuration (v1.111.0): a candidate whose fix lands
    # in a .properties rate/config card was invisible to the patch
    # prompt -- on the first live .properties candidate the model
    # named the right file from the brief, invented its contents, and
    # had every diff correctly rejected (patch_not_applicable).
    ".properties",
    # Env-style configuration (v1.112.0): a candidate whose fix lands
    # in an env rate/config card ('rates.env', or a file named exactly
    # '.env' -- carried in _TEXT_BASENAMES below, since a leading-dot
    # name has no suffix) was invisible to the patch prompt -- on the
    # first live .env candidate the model named the right file from
    # the brief, invented its contents, and had every diff correctly
    # rejected (patch_not_applicable).
    ".env",
    # Generic config files (v1.113.0): a candidate whose fix lands
    # in a '.conf' rate/config card was invisible to the patch
    # prompt -- on the first live .conf candidate the model named
    # the right file from the brief, invented its contents, and
    # had every diff correctly rejected (patch_not_applicable).
    ".conf",
    # JSON Lines data files (v1.114.0): a candidate whose fix lands
    # in a '.jsonl' tier/data table was invisible to the patch
    # prompt -- '.json' is whitelisted but the snapshot matches the
    # full suffix, so 'tiers.jsonl' (and its '.ndjson' alias) never
    # reached the model; on the first live .jsonl candidate the
    # model named the right file from the brief, invented its
    # contents, and had every diff correctly rejected
    # (patch_not_applicable).
    ".jsonl", ".ndjson",
    # JavaScript / TypeScript module variants (v1.115.0): a candidate
    # whose fix lands in a '.mjs' / '.cjs' (or '.mts' / '.cts') module
    # was invisible to the patch prompt -- '.js' / '.ts' are
    # whitelisted but the snapshot matches the full suffix, so a
    # 'discount.mjs' never reached the model; on the first live .mjs
    # candidate the model named the right file from the brief,
    # invented its contents as a file creation, and had every diff
    # correctly rejected (patch_not_applicable).
    ".mjs", ".cjs", ".mts", ".cts",
    # Shell variants (v1.116.0): a candidate whose fix lands in a
    # '.bash' (or '.zsh') script was invisible to the patch prompt --
    # '.sh' is whitelisted but the snapshot matches the full suffix,
    # so a 'rates.bash' never reached the model; on the first live
    # .bash candidate the model named the right file from the brief,
    # invented its contents, and had every diff correctly rejected
    # (patch_not_applicable).
    ".bash", ".zsh",
    # C++ variants (v1.117.0): a candidate whose fix lands in a
    # '.hpp' header (or a '.cc' / '.cxx' / '.hxx' sibling) was
    # invisible to the patch prompt -- '.cpp' / '.h' are whitelisted
    # but the snapshot matches the full suffix, so a 'rates.hpp'
    # header never reached the model; on the first live .hpp
    # candidate the model named the right file from the brief,
    # invented its contents, and had every diff correctly rejected
    # (patch_not_applicable).
    ".hpp", ".cc", ".cxx", ".hxx",
    # Perl modules (v1.118.0): a candidate whose fix lands in a
    # '.pm' module was invisible to the patch prompt -- '.pl' is
    # whitelisted but the snapshot matches the full suffix, so a
    # 'Rates.pm' module never reached the model; on the first live
    # .pm candidate the model named the right file from the brief,
    # invented its contents, and had every diff correctly rejected
    # (patch_not_applicable).
    ".pm",
    # AWK programs (v1.119.0): a candidate whose fix lands in a
    # '.awk' program was invisible to the patch prompt -- no AWK
    # suffix was whitelisted at all, so a 'rates.awk' program never
    # reached the model; on the first live .awk candidate the model
    # named the right file from the brief, invented its contents as
    # a file creation, and had every diff correctly rejected
    # (patch_not_applicable).
    ".awk",
    # jq programs (v1.120.0): a candidate whose fix lands in a
    # '.jq' program was invisible to the patch prompt -- no jq
    # suffix was whitelisted at all, so a 'rates.jq' program never
    # reached the model; on the first live .jq candidate the model
    # named the right file from the brief, invented its contents as
    # a file creation, and had every diff correctly rejected
    # (patch_not_applicable).
    ".jq",
    # sed programs (v1.121.0): a candidate whose fix lands in a
    # '.sed' program was invisible to the patch prompt -- no sed
    # suffix was whitelisted at all, so a 'rates.sed' program never
    # reached the model; on the first live .sed candidate the model
    # named the right file from the brief, invented its contents as
    # a file creation, and had every diff correctly rejected
    # (patch_not_applicable).
    ".sed",
    # Expect programs (v1.122.0): a candidate whose fix lands in an
    # '.exp' program was invisible to the patch prompt -- no Expect
    # suffix was whitelisted at all, so a 'rates.exp' program never
    # reached the model; on the first live .exp candidate the model
    # named the right file from the brief, invented its contents,
    # and had every diff correctly rejected
    # (patch_not_applicable).
    ".exp",
    # Tcl programs (v1.123.0): a candidate whose fix lands in a
    # '.tcl' program was invisible to the patch prompt -- no Tcl
    # suffix was whitelisted at all (the '.exp' entry covers Expect
    # scripts, not plain Tcl), so a 'rates.tcl' program never
    # reached the model; on the first live .tcl candidate the model
    # named the right file from the brief, invented its contents,
    # and had every diff correctly rejected
    # (patch_not_applicable).
    ".tcl",
    # XML property lists (v1.124.0): a candidate whose fix lands in
    # a '.plist' rate/config card was invisible to the patch prompt
    # -- no plist suffix was whitelisted at all (the '.xml' entry
    # matches the full suffix, not the content), so a 'rates.plist'
    # card never reached the model; on the first live .plist
    # candidate the model named the right file from the brief,
    # invented its contents, and had every diff correctly rejected
    # (patch_not_applicable).
    ".plist",
    # Dart programs (v1.126.0): a candidate whose fix lands in a
    # '.dart' program was invisible to the patch prompt -- no Dart
    # suffix was whitelisted at all, so a 'rates.dart' rate card
    # never reached the model; on the first live .dart candidate
    # the model named the right file from the brief, invented its
    # contents, and had every diff correctly rejected
    # (patch_not_applicable).
    ".dart",
    # R programs (v1.127.0): a candidate whose fix lands in a
    # '.r' program was invisible to the patch prompt -- no R
    # suffix was whitelisted at all, so a 'rates.r' rate card
    # never reached the model; on the first live .r candidate
    # the model named the right file from the brief, invented
    # its contents as a file creation, and had every diff
    # correctly rejected (patch_not_applicable).
    ".r",
    # Julia programs (v1.128.0): a candidate whose fix lands in a
    # '.jl' program was invisible to the patch prompt -- no Julia
    # suffix was whitelisted at all, so a 'rates.jl' rate card
    # never reached the model; on the first live .jl candidate
    # the model named the right file from the brief, invented
    # its contents, and had every diff correctly rejected
    # (patch_not_applicable).
    ".jl",
    # Haskell programs (v1.129.0): a candidate whose fix lands in a
    # '.hs' program was invisible to the patch prompt -- no Haskell
    # suffix was whitelisted at all, so a 'rates.hs' rate card
    # never reached the model; on the first live .hs candidate
    # the model named the right file from the brief, invented
    # its contents as a file creation, and had every diff
    # correctly rejected (patch_not_applicable).
    ".hs",
    # OCaml programs (v1.130.0): a candidate whose fix lands in a
    # '.ml' program was invisible to the patch prompt -- no OCaml
    # suffix was whitelisted at all, so a 'rates.ml' rate card
    # never reached the model; on the first live .ml candidate
    # the model named the right file from the brief, invented
    # its contents, and had every diff correctly rejected
    # (patch_not_applicable).
    ".ml",
    # Erlang programs (v1.131.0): a candidate whose fix lands in a
    # '.erl' program was invisible to the patch prompt -- no Erlang
    # suffix was whitelisted at all, so a 'rates.erl' rate card
    # never reached the model; on the first live .erl candidate
    # the model named the right file from the brief, invented
    # its contents as a file creation, and had every diff
    # correctly rejected (patch_not_applicable).
    ".erl",
    # Elixir programs (v1.132.0): a candidate whose fix lands in a
    # '.ex' program was invisible to the patch prompt -- no Elixir
    # suffix was whitelisted at all, so a 'rates.ex' rate card
    # never reached the model; on the first live .ex candidate
    # the model named the right file from the brief, invented
    # its contents, and had every diff correctly rejected
    # (patch_not_applicable).
    ".ex",
    # Elixir scripts (v1.133.0): a candidate whose fix lands in a
    # '.exs' script was invisible to the patch prompt -- '.ex' is
    # whitelisted but the snapshot matches the full suffix, so a
    # 'rates.exs' rate card never reached the model; on the first
    # live .exs candidate the model named the right file from the
    # brief, invented its contents as a file creation, and had
    # every diff correctly rejected (patch_not_applicable).
    ".exs",
    # F# programs (v1.134.0): a candidate whose fix lands in a
    # '.fs' program was invisible to the patch prompt -- no F#
    # suffix was whitelisted at all, so a 'rates.fs' rate card
    # never reached the model; on the first live .fs candidate
    # the model named the right file from the brief, invented
    # its contents as a file creation, and had every diff
    # correctly rejected (patch_not_applicable).
    ".fs",
    # F# scripts (v1.135.0): a candidate whose fix lands in a
    # '.fsx' script was invisible to the patch prompt -- '.fs' is
    # whitelisted but the snapshot matches the full suffix, so a
    # 'rates.fsx' rate card never reached the model; on the first
    # live .fsx candidate the model named the right file from the
    # brief, invented its contents as a file creation, and had
    # every diff correctly rejected (patch_not_applicable).
    ".fsx",
    # F# signature files (v1.136.0): a candidate whose fix lands in a
    # '.fsi' signature file was invisible to the patch prompt --
    # '.fs' is whitelisted but the snapshot matches the full suffix,
    # so a 'rates.fsi' rate card never reached the model; on the
    # first live .fsi candidate the model named the right file from
    # the brief, invented its contents as a file creation, and had
    # every diff correctly rejected (patch_not_applicable). With
    # the file visible its diffs then exposed the hunk over-count
    # bug closed in patch.py in the same release.
    ".fsi",
    # F# script files, long form (v1.137.0): a candidate whose fix
    # lands in a '.fsscript' script was invisible to the patch
    # prompt -- '.fs' / '.fsx' are whitelisted but the snapshot
    # matches the full suffix, so a 'rates.fsscript' rate card
    # never reached the model; on the first live .fsscript
    # candidate the model named the right file from the brief,
    # invented its contents, and had every diff correctly
    # rejected (patch_not_applicable).
    ".fsscript",
    # OCaml interface files (v1.138.0): a candidate whose fix
    # lands in a '.mli' interface file was invisible to the patch
    # prompt -- '.ml' is whitelisted but the snapshot matches the
    # full suffix, so a 'rates.mli' rate card never reached the
    # model; on the first live .mli candidate the model named the
    # right file from the brief, invented its contents as a file
    # creation, and had every diff correctly rejected
    # (patch_not_applicable).
    ".mli",
    # Erlang header files (v1.139.0): a candidate whose fix lands
    # in a '.hrl' header file was invisible to the patch prompt --
    # '.erl' is whitelisted but the snapshot matches the full
    # suffix, so a 'rates.hrl' rate card never reached the model;
    # on the first live .hrl candidate the model named the right
    # file from the brief, invented its contents as a file
    # creation, and had every diff correctly rejected
    # (patch_not_applicable).
    ".hrl",
    # Literate Haskell files (v1.140.0): a candidate whose fix
    # lands in a '.lhs' literate program was invisible to the
    # patch prompt -- '.hs' is whitelisted but the snapshot
    # matches the full suffix, so a 'rates.lhs' rate card never
    # reached the model; on the first live .lhs candidate the
    # model named the right file from the brief, invented its
    # contents, and had every diff correctly rejected
    # (patch_not_applicable).
    ".lhs",
    # Clojure programs (v1.141.0): a candidate whose fix lands
    # in a '.clj' program was invisible to the patch prompt --
    # no Clojure suffix was whitelisted at all, so a 'rates.clj'
    # rate card never reached the model; on the first live .clj
    # candidate the model named the right file from the brief,
    # invented its contents, and had every diff correctly
    # rejected (patch_not_applicable).
    ".clj",
    # EDN data files (v1.142.0): a candidate whose fix lands
    # in a '.edn' data file was invisible to the patch prompt --
    # no EDN suffix was whitelisted at all ('.clj' covers Clojure
    # source, not the EDN data notation a rate card is usually
    # written in), so a 'rates.edn' rate card never reached the
    # model; on the first live .edn candidate the model named
    # the right file from the brief, invented its contents as a
    # file creation, and had every diff correctly rejected
    # (patch_not_applicable).
    ".edn",
    # ClojureScript programs (v1.143.0): a candidate whose fix lands
    # in a '.cljs' program was invisible to the patch prompt --
    # '.clj' is whitelisted but the snapshot matches the full
    # suffix, so a 'rates.cljs' rate card never reached the model;
    # on the first live .cljs candidate the model named the right
    # file from the brief, invented its contents, and had every
    # diff correctly rejected (patch_not_applicable).
    ".cljs",
    # Clojure common files (v1.144.0): a candidate whose fix lands
    # in a '.cljc' program was invisible to the patch prompt --
    # '.clj' / '.cljs' are whitelisted but the snapshot matches the
    # full suffix, so a 'rates.cljc' rate card (shared by Clojure
    # and ClojureScript) never reached the model; on the first live
    # .cljc candidate the model named the right file from the
    # brief, invented its contents, and had every diff correctly
    # rejected (patch_not_applicable).
    ".cljc",
    # Zig programs (v1.145.0): a candidate whose fix lands in a
    # '.zig' program was invisible to the patch prompt -- no Zig
    # suffix was whitelisted at all, so a 'rates.zig' rate card
    # never reached the model; on the first live .zig candidate
    # the model named the right file from the brief, invented
    # its contents as a file creation, and had every diff
    # correctly rejected (patch_not_applicable).
    ".zig",
    # Nim programs (v1.146.0): a candidate whose fix lands in a
    # '.nim' program was invisible to the patch prompt -- no Nim
    # suffix was whitelisted at all, so a 'rates.nim' rate card
    # never reached the model; on the first live .nim candidate
    # the model named the right file from the brief, invented
    # its contents, and had every diff correctly rejected
    # (patch_not_applicable).
    ".nim",
    # Vue single-file components (v1.147.0): a candidate whose fix
    # lands in a '.vue' component was invisible to the patch prompt
    # -- no Vue suffix was whitelisted at all, so a 'rates.vue' rate
    # card never reached the model; on the first live .vue candidate
    # the model named the right file from the brief, invented its
    # contents, and had every diff correctly rejected
    # (patch_not_applicable).
    ".vue",
    # Svelte components (v1.148.0): a candidate whose fix lands
    # in a '.svelte' component was invisible to the patch prompt
    # -- no Svelte suffix was whitelisted at all, so a
    # 'rates.svelte' rate card never reached the model; on the
    # first live .svelte candidate the model named the right file
    # from the brief, invented its contents, and had every diff
    # correctly rejected (patch_not_applicable).
    ".svelte",
    # Astro components (v1.149.0): a candidate whose fix lands
    # in a '.astro' component was invisible to the patch prompt
    # -- no Astro suffix was whitelisted at all, so a
    # 'rates.astro' rate card never reached the model; on the
    # first live .astro candidate the model named the right file
    # from the brief, invented its contents, and had every diff
    # correctly rejected (patch_not_applicable).
    ".astro",
    # MDX files (v1.150.0): a candidate whose fix lands in a
    # '.mdx' file was invisible to the patch prompt -- '.md' is
    # whitelisted but the snapshot matches the full suffix, so a
    # 'rates.mdx' rate card never reached the model; on the
    # first live .mdx candidate the model named the right file
    # from the brief, invented its contents, and had every diff
    # correctly rejected (patch_not_applicable).
    ".mdx",
    # SCSS stylesheets (v1.151.0): a candidate whose fix lands in a
    # '.scss' file was invisible to the patch prompt -- '.css' is
    # whitelisted but the snapshot matches the full suffix, and no
    # SCSS suffix was whitelisted at all, so a 'rates.scss' rate
    # card never reached the model; on the first live .scss
    # candidate the model named the right file from the brief,
    # invented its contents, and had every diff correctly rejected
    # (patch_not_applicable).
    ".scss",
}
# Extensionless text files (v1.110.0): the build/ops files a fix most
# often lands in carry no suffix at all, so the suffix whitelist alone
# silently omitted them from the snapshot -- on the first live Makefile
# candidate the model named the right file from the brief but had never
# seen its contents, and every proposed hunk missed the file on disk
# (patch_not_applicable). Membership is by exact basename.
_TEXT_BASENAMES = {
    "Makefile", "makefile", "GNUmakefile", "Dockerfile", "Rakefile",
    "Gemfile", "Jenkinsfile", "Vagrantfile", "Procfile", "CMakeLists.txt",
    # '.env' (v1.112.0): a leading-dot filename carries no suffix, so
    # only exact-basename membership can ever include it.
    ".env",
}
_MAX_FILE_CHARS = 4000
_MAX_TOTAL_CHARS = 12000


def _candidate_files(root: str | Path) -> dict[str, str]:
    """Snapshot the candidate's small text files for the patch prompt.

    A model asked for a unified diff must see the exact file contents
    the diff will be applied to — brief + check output alone cannot
    produce a diff whose context lines match. Binary, oversized, and
    housekeeping files are skipped; the snapshot is capped overall.

    Bounded read (v1.79.0): a file is read through a text stream
    capped at ``_MAX_FILE_CHARS + 1`` *characters*, never whole.
    Before this, ``read_text`` materialised the entire file — bytes
    and decoded string both — before the length check below could
    skip it, so a single snapshot-eligible file could allocate
    roughly twice its size (a 20 MiB probe file peaked at 40 MiB)
    on its way to being skipped, and the candidate bounds allow a
    file of ~256 MiB. Reading one character past the cap decides
    the same skip, and a file the read returns in full is exactly
    the file ``read_text`` would have returned: a file over the
    cap, a file whose undecodable bytes sit inside the cap, and a
    file whose undecodable bytes sit past the cap (the capped read
    already proves it oversize) all skip exactly as before.
    """
    files: dict[str, str] = {}
    total = 0
    # Sandbox containment traversal: a candidate symlink pointing at a
    # host file must never leak that file's contents into an LLM prompt.
    for rel, source in iter_candidate_files(root):
        if (Path(rel).suffix.lower() not in _TEXT_SUFFIXES
                and Path(rel).name not in _TEXT_BASENAMES):
            continue
        try:
            with source.open(encoding="utf-8") as handle:
                text = handle.read(_MAX_FILE_CHARS + 1)
        except (UnicodeDecodeError, OSError):
            continue
        if len(text) > _MAX_FILE_CHARS or total + len(text) > _MAX_TOTAL_CHARS:
            continue
        files[rel] = text
        total += len(text)
    return files


def _usage_totals(records_and_report: list) -> dict:
    """Aggregate token usage across sealed entries + unsealed calls."""
    calls = 0
    total_tokens = 0
    for item in records_and_report:
        for c in item:
            calls += 1
            total_tokens += (c.get("usage") or {}).get("total_tokens") or 0
    return {"llm_calls": calls, "total_tokens": total_tokens}


def run_repair(
    *,
    spec: TaskSpec,
    candidate_dir: str | Path,
    mode: str,
    llm: LLMAdapter,
    references: ReferenceLookup,
    spec_label: str,
    candidate_label: str,
    max_rounds: int = 3,
) -> dict:
    # The mode is validated the same way, before any verification
    # run (v1.38.0): the loop seals it into every attempt's header
    # and into its own report summary, so an invalid mode would
    # otherwise run the whole loop and deliver a report whose every
    # record fails verification. The runner validates it too; this
    # is the loop's own fail-fast, beside its budget validation.
    validate_mode(mode)
    # The spec object is validated the same way (v1.39.0): a
    # directly-constructed TaskSpec bypasses parse_spec, and the
    # loop seals its name/brief into the report summary and hands
    # its brief to the LLM — an invalid spec must fail here, beside
    # the budget validation, before the first verification run or
    # any generation call. The runner validates it too; this is the
    # loop's own fail-fast.
    validate_spec(spec)
    # bool is an int subclass: JSON true would otherwise become a
    # 1-round budget whose report then fails verify's non-bool-int
    # rule; a float would silently allow more rounds than stated; a
    # string/None would crash the old ``< 0`` comparison outright.
    if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) \
            or not 0 <= max_rounds <= MAX_ROUNDS:
        raise ValueError(
            f"max_rounds must be an int between 0 and {MAX_ROUNDS}")
    # The labels are validated the same way (v1.40.0): they form
    # every attempt's replay string, so an invalid one — non-string,
    # empty, or over the ceiling — must fail here, beside the budget
    # validation, before the first verification run or any
    # generation call. The runner validates them too; this is the
    # loop's own fail-fast.
    validate_label(spec_label, "spec_label")
    validate_label(candidate_label, "candidate_label")
    # The candidate directory is validated the same way (v1.41.0):
    # a non-path, empty, or over-long value must fail here, beside
    # the budget validation, before the first verification run or
    # any generation call. The runner validates it too; this is the
    # loop's own fail-fast.
    validate_candidate_dir(candidate_dir)
    # The adapters are validated the same way (v1.42.0): the loop
    # reads llm.name below, before the first run — a malformed
    # adapter must be a ValueError here, not an AttributeError
    # with no report. The runner validates them too.
    validate_llm(llm)
    validate_references(references)
    # The adapter name and the usage snapshots this report carries
    # are adapter-supplied: normalise them the same way the runner
    # normalises the sealed copies (v1.35.0), so the report's own
    # narrative check — which caps both — passes on a report this
    # loop emits, whatever the adapter calls itself or recorded.
    adapter_name = normalise_adapter_name(
        getattr(llm, "name", None), "unknown-llm-adapter")
    attempts: list[dict] = []
    current_dir = Path(candidate_dir)
    current_label = candidate_label
    patch_info: dict | None = None
    record = run_verification(
        spec=spec, candidate_dir=current_dir, mode=mode, llm=llm,
        references=references, spec_label=spec_label,
        candidate_label=current_label,
    )
    attempts.append({
        "round": 0,
        "candidate_dir": str(current_dir),
        "verdict": record["verdict"],
        "record_hash": record["record_hash"],
        "patch_from_previous": None,
        "evidence": record,
    })

    rounds_used = 0
    owned_dir: Path | None = None  # latest repair-created patched copy
    patch_error: str | None = None
    failed_patch: dict | None = None
    patch_feedback: str | None = None
    stopped_reason = "passed" if record["verdict"] == "PASS" else "max_rounds"
    while record["verdict"] == "FAIL" and rounds_used < max_rounds:
        failing_evidence = {
            "spec_name": spec.name,
            "brief": spec.brief,
            "verdict": record["verdict"],
            "checks": _check_dicts(record),
            # The exact sources the proposed diff must apply to. The
            # mock ignores this; the real adapter embeds it verbatim.
            "candidate_files": _candidate_files(current_dir),
        }
        if patch_feedback:
            # A previous diff was rejected by the applier: hand the
            # exact rejection back so the next proposal can fix its
            # form, not just its content. Each retry costs one round.
            failing_evidence["patch_error"] = patch_feedback
        calls_before = len(pending_calls_safely(llm))
        rounds_used += 1
        try:
            patch_text = llm.generate_patch(spec.brief, failing_evidence)
            if not isinstance(patch_text, str):
                raise TypeError(
                    "generate_patch returned "
                    f"{type(patch_text).__name__}, not str")
        except Exception as exc:  # generation must never destroy the report
            gen_error = (
                f"patch generation failed ({type(exc).__name__}): "
                f"{str(exc)[:500]}")
            patch_info = {
                "round": rounds_used,
                "llm_adapter": adapter_name,
                "applied": False,
                "changed_files": [],
                "error": gen_error,
                "error_kind": "generation_failed",
                # Snapshot only — the calls stay pending and are
                # surfaced as the report's unsealed usage below.
                "llm_usage": normalise_usage_calls(
                    pending_calls_safely(llm)[calls_before:]),
                # No proposal exists to preserve.
                "patch_text": "",
            }
            patch_error = gen_error
            failed_patch = patch_info
            stopped_reason = "generation_failed"
            break
        patch_calls = normalise_usage_calls(
            pending_calls_safely(llm)[calls_before:]
        )
        patch_info = {
            "round": rounds_used,
            "llm_adapter": adapter_name,
            "applied": False,
            "changed_files": [],
            "error": None,
            "error_kind": None,
            # Snapshot only — the calls stay pending and are sealed
            # into the next attempt's evidence record by the runner.
            "llm_usage": patch_calls,
            # The raw proposal, kept so a rejected patch stays
            # debuggable from the report alone (truncated for size).
            "patch_text": patch_text[:MAX_PATCH_TEXT_CHARS],
        }
        # Fresh copy through the hardened workspace path (scrubbed
        # traversal: no symlink escapes, no housekeeping files).
        new_dir = prepare_workspace(current_dir)
        try:
            changed = apply_patch(new_dir, patch_text)
        except PatchError as exc:
            # The rejected copy is never used again; do not leak it.
            remove_tree(new_dir)
            # An applier message embeds the offending path, which
            # the proposal itself chose: cap what the report stores
            # (and what feedback/prompts carry) at the source.
            err_text = str(exc)[:MAX_PATCH_ERROR_CHARS]
            patch_info["error"] = err_text
            patch_info["error_kind"] = "patch_rejected"
            patch_error = err_text
            failed_patch = patch_info
            patch_feedback = err_text
            if rounds_used < max_rounds:
                continue  # retry with the rejection as feedback
            stopped_reason = "patch_not_applicable"
            break
        patch_info["applied"] = True
        patch_info["changed_files"] = changed
        patch_feedback = None
        # The previous repair-owned copy is superseded: its evidence
        # record is already sealed into `attempts`, so delete it
        # instead of leaking one full candidate copy per round. The
        # caller's own candidate directory is never touched, and the
        # final patched copy is kept (the CLI's --patched-out copies
        # from attempts[-1].candidate_dir after the loop returns).
        if owned_dir is not None:
            remove_tree(owned_dir)
        owned_dir = new_dir
        current_dir = new_dir
        current_label = str(new_dir)
        record = run_verification(
            spec=spec, candidate_dir=current_dir, mode=mode, llm=llm,
            references=references, spec_label=spec_label,
            candidate_label=current_label,
        )
        # The runner's replay string points at the ephemeral patched
        # copy (deleted before the caller can ever run it). Replace it
        # with the truthful instruction: replay the whole report from
        # the original candidate; `verifygate replay` reconstructs
        # this attempt's tree from the recorded patch.
        update_replay_command(record, (
            f"python3 -m verifygate replay --record <repair-report.json> "
            f"--spec {quote_label(spec_label)} "
            f"--candidate {quote_label(candidate_label)} "
            f"--mode {mode}  # attempt {rounds_used}: patched copy, "
            f"reconstructed from the report's recorded patch"
        ))
        attempts.append({
            "round": rounds_used,
            "candidate_dir": str(current_dir),
            "verdict": record["verdict"],
            "record_hash": record["record_hash"],
            "patch_from_previous": patch_info,
            "evidence": record,
        })
        if record["verdict"] == "PASS":
            stopped_reason = "passed"

    # Calls still pending were never sealed into a record (e.g. a patch
    # that did not apply ended the loop): surface them at report level
    # instead of dropping the usage on the floor.
    unsealed = normalise_usage_calls(drain_calls_safely(llm))
    sealed_calls = [
        c
        for a in attempts
        for e in a["evidence"]["entries"]
        if e["kind"] == "llm_usage"
        for c in e["calls"]
    ]
    return {
        "tool": "verifygate",
        "version": __version__,
        "kind": "repair-report",
        "spec_name": spec.name,
        "mode": mode,
        "llm_adapter": adapter_name,
        "max_rounds": max_rounds,
        "rounds_used": rounds_used,
        "final_verdict": attempts[-1]["verdict"],
        "stopped_reason": stopped_reason,
        "last_patch_error": patch_error,
        "failed_patch": failed_patch,
        "llm_usage_total": _usage_totals([sealed_calls, unsealed]),
        "unsealed_llm_usage": unsealed,
        "attempts": attempts,
    }
