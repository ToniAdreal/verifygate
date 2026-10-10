# VerifyGate

**AI proposes. Deterministic checks dispose. The evidence record proves it.**

VerifyGate is a pre-delivery verification harness for coding agents. A coding
agent (or a human) submits a code change plus a task spec; VerifyGate runs
the spec's deterministic checks — unit tests, reproduction scripts,
lint/type gates — in a sandboxed copy of the candidate, and only artifacts
that pass receive a signed, replayable **evidence record**.

The LLM (NVIDIA Nemotron via Nebius Token Factory) may *propose* patches and
*explain* failures. It never decides PASS/FAIL. Verdicts come only from
deterministic validators: exit codes and assertions.

## Quickstart (Judge Mode — zero keys, zero network)

```bash
# Passing candidate
python3 -m verifygate run --spec samples/spec.json \
    --candidate samples/passing --mode judge --out evidence-passing.json
# -> VERDICT: PASS

# Failing candidate (intentionally broken slugify)
python3 -m verifygate run --spec samples/spec.json \
    --candidate samples/failing --mode judge --out evidence-failing.json
# -> VERDICT: FAIL, with a diagnosis entry in the evidence record
```

Judge Mode swaps the LLM for a deterministic mock adapter: every validator
still runs for real — only the *words* (patch proposals, explanations) are
template-rendered. A FAIL record is a deliverable, not an error: it carries
the diagnosis meant to feed the next repair iteration.

## Repair loop (Judge Mode)

```bash
python3 -m verifygate repair --spec samples/spec.json \
    --candidate samples/failing --mode judge --max-rounds 3 \
    --out repair-report.json --patched-out patched-candidate/
# attempt 0: FAIL -> mock proposes the known-good slugify fix as a unified
# diff -> VerifyGate applies it to a *copy* of the candidate -> re-verifies
# attempt 1: PASS
```

The loop feeds each FAIL's check results to the LLM adapter, applies the
proposed unified diff to a fresh copy of the candidate (the original is
never modified; a diff whose context does not match the file on disk is
rejected, never forced), and re-runs every check from scratch. It stops
at the first PASS, when no usable patch is produced, or after
`--max-rounds` patch attempts. The repair report embeds every attempt's
full evidence record, so each intermediate verdict stays independently
replayable. In real mode the same loop drives Nemotron via Token Factory:
the model proposes diffs, deterministic checks still dispose.

The sample candidates' own checks use `pytest` (the sample library's test
suite); VerifyGate itself is standard-library only.

## Second sample: JavaScript (language-agnostic proof)

VerifyGate is written in Python, but check commands are arbitrary shell
commands, so the harness is language-agnostic by construction.
`samples-js/` proves it with a second, independent task — fix a cart
`total()` that ignores its tax rate — whose checks are Node's built-in
test runner and a Node repro script. No Python runs inside the
candidate at all:

```bash
python3 -m verifygate run --spec samples-js/spec.json \
    --candidate samples-js/passing --mode judge   # -> VERDICT: PASS
python3 -m verifygate run --spec samples-js/spec.json \
    --candidate samples-js/failing --mode judge   # -> VERDICT: FAIL
python3 -m verifygate repair --spec samples-js/spec.json \
    --candidate samples-js/failing --mode judge --max-rounds 3
# -> attempt 0: FAIL, attempt 1: PASS (mock proposes the cart diff in
#    Judge Mode, exactly as for the Python sample)
```

Judge Mode evidence records for both outcomes are committed at
`samples-js/evidence_passing.json` and `samples-js/evidence_failing.json`.
Real mode is language-agnostic too: a committed real-mode repair report
at `samples-js/evidence_real_repair.json` shows Nemotron 3 Super 120B
repairing the Node candidate — attempt 0 FAIL with the model's own
diagnosis, its diff applied to `cart.js`, attempt 1 PASS (2 calls,
6,390 tokens sealed in the hash chains). The model's fix is its own
implementation, distinct from both the passing sample and the mock's
known-good diff.

## Real mode (Nebius Token Factory)

Real mode drives an NVIDIA Nemotron model on Nebius Token Factory for
patch generation and failure explanation. Authentication uses the
stored Token Factory connector credential — no API key is read from
the environment or the command line. The model is configuration:
`--model` or `NEBIUS_MODEL`, defaulting to the pinned
`nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`.

```bash
# Verify with a real Nemotron diagnosis on failure
python3 -m verifygate run --spec samples/spec.json \
    --candidate samples/failing --mode real

# The full loop: FAIL -> Nemotron diff -> apply -> re-verify
python3 -m verifygate repair --spec samples/spec.json \
    --candidate samples/failing --mode real \
    --model nvidia/nemotron-3-super-120b-a12b --max-rounds 3 \
    --out evidence-real-repair.json
# -> attempt 0: FAIL (real diagnosis), attempt 1: PASS
```

Every real model call (model, purpose, prompt/completion/total token
usage) is sealed into the evidence record's hash chain as an
`llm_usage` entry, so the token bill is as tamper-evident as the
verdict. A diff that does not apply cleanly is rejected with the
applier's exact error, which is fed back to the model for the next
round instead of ending the loop. A real FAIL → repair → PASS report
is committed at `samples/evidence_real_repair.json` (Nemotron 3 Super
120B, 2 calls, 5,367 tokens, both attempt chains valid). A real-mode
FAIL record from the Nano model is committed at
`samples/evidence_real_fail.json` (1 call, 1,133 tokens sealed) — it
also shows the Tavily degradation note (`reference_error`) sealed into
the diagnosis entry when no Tavily key is configured. The same real
repair loop has also repaired the JavaScript sample
(`samples-js/evidence_real_repair.json`, Super 120B, 6,390 tokens
sealed): real mode, like Judge Mode, is language-agnostic.

Since v1.5.0 the chain itself is hardened (chain v2 seals each
entry's kind; see *Verifying an evidence record*), and the flagship
loop is now proven under it in real mode too:
`samples/evidence_real_repair_v2.json` is a chain-v2 repair report
from Nemotron 3 Super 120B — attempt 0 FAIL with the model's own
diagnosis, a first diff rejected for a context mismatch and fed
back, a second diff applied, attempt 2 PASS (3 calls, 11,726 tokens
sealed; both chains verify, and the report replays end-to-end in
Judge Mode). Its first attempted run that day is also why the patch
prompt now orders the model to reason silently: Super spent the
whole 8,192-token budget deliberating in the reply content
(finish_reason=length) and never produced a usable diff. Total
sealed real-mode usage across all committed records: 24,616 tokens.

## Diagnosis references (Tavily)

On a FAIL, the diagnosis step performs one Tavily search built from
the failing check's key output line, and attaches the top references
(title, URL, snippet, distinctly sourced `tavily`) to the diagnosis
entry in the evidence record. References are commentary — they never
influence the verdict.

The lookup is structurally incapable of breaking a run: a missing
`TAVILY_API_KEY`, a network or HTTP failure, or a malformed response
all degrade to *no references*, and the reason is sealed into the
record as the diagnosis entry's `reference_error`, so a reader can
see the degradation instead of guessing at it. Judge Mode uses a
deterministic mock reference instead. The evidence web view renders
the degradation note next to the diagnosis when one is present.

## Evidence web view

Any evidence record or repair report renders as one self-contained
HTML page — inline CSS, no JavaScript, no external resources:

```bash
python3 -m verifygate view --record samples/evidence_real_repair.json \
    --out samples/evidence_view.html
```

The page is generated from the sealed JSON: every record string is
HTML-escaped, reference URLs become links only for http/https, and
the hash chain is recomputed at render time — an intact record shows
"Hash chain verified", a tampered one renders a visible CHAIN INVALID
banner instead of a clean verdict. A pre-rendered view of the real
Nemotron repair report is committed at `samples/evidence_view.html`.

## Sandbox hardening

Candidate code is untrusted, so the sandbox treats it that way:

- **Scrubbed environment** — checks run with an allowlisted environment
  only (`PATH`, locale, `HOME`, …). API keys, the evidence signing key,
  and every other host variable never reach check processes.
- **Symlink containment** — one traversal rule drives the workspace
  copy, the candidate tree hash, and the repair loop's prompt snapshot:
  file symlinks are followed only when their target stays inside the
  candidate; links pointing outside are dropped and directory symlinks
  are never followed. A candidate cannot smuggle a host file into the
  workspace, the hash, or an LLM prompt.
- **Bounded output** — check stdout/stderr stream to temp files and
  only a capped tail is read back; a check printing gigabytes cannot
  exhaust the harness's memory.
- **Process-group timeouts** — checks start in a new session and a
  timeout kills the whole group, so no detached child outlives a FAIL.
- **Workspace cleanup** — every temp copy is removed when its owner
  is done (verification workspaces immediately; repair-loop copies
  when rejected or superseded; the CLI removes the final patched copy
  after copying it out). A leaked-copy bug once filled a temp
  filesystem during dogfooding; lifecycle tests pin the contract.
- **Permission fidelity** — the workspace copy preserves each file's
  permission bits, and the patch applier honours git mode lines
  (`new file mode`, `old mode`/`new mode`, including mode-only
  diffs). A candidate whose script is executable on disk is verified
  as executable — never silently downgraded to a "Permission denied"
  FAIL — and a repair can create or chmod a script the same way a
  human's commit would.

VerifyGate dogfoods itself: `dogfood_spec.json` at the repository
root runs the project's own pytest suite as a VerifyGate check on a
copy of this tree, and the resulting PASS record is committed next to
it as `evidence_self_check.json`. Reproduce it from a fresh clone:

```bash
python3 -m verifygate run --spec dogfood_spec.json \
    --candidate . --mode judge --out evidence_self_check.json
# -> VERDICT: PASS (the suite passes inside the sandbox)
```

## Task spec format

```json
{
  "name": "stringtools-slugify-fix",
  "brief": "Fix slugify() so it lowercases, strips punctuation, hyphenates.",
  "checks": [
    {"name": "unit_tests",   "command": "python3 -m pytest -q", "timeout_s": 60},
    {"name": "repro_script", "command": "python3 repro.py",     "timeout_s": 30}
  ]
}
```

Spec validation is strict about types: `name`, `brief`, and every
check `name` / `command` must be strings, and a check name or command
that is empty or all whitespace is rejected — a whitespace-only
command is not a check at all, since the shell exits 0 on it and it
would seal a vacuous PASS. `timeout_s` must be a real integer between
1 and 3600 (JSON `true` is not accepted as 1). A malformed spec is a
usage error — one-line message, exit 2 — never a traceback or a
silently reinterpreted run.

## Evidence records

Each run emits a JSON record binding the spec hash, the candidate tree
hash, and every check result (command, exit code, duration, output tails).
Entries are hash-chained and the record hash is HMAC-signed
(`VERIFYGATE_SIGNING_KEY`; a clearly-labelled public dev key is the
default). `verifygate.evidence.verify_chain()` detects any edit, and the
record's `replay` field is the exact command to reproduce the verdict.

Records sealed by this version carry `chain_version: 2`: every entry's
hash covers its *kind* as well as its content. Chain v1 hashed the
content only, so an entry could be relabelled (a FAIL check re-tagged
as a "diagnosis", which consumers render as commentary) without
breaking the chain; verification now also enforces the record's
structure — kinds, their order, and the fields each kind must carry —
for both chain versions, so a relabelled legacy record fails too,
while every untampered legacy record (including all the committed
real-mode evidence) still verifies exactly as before.

## CLI contract

All five subcommands (`run`, `repair`, `view`, `verify`, `replay`)
share one exit-code contract: **0** = PASS / record intact / replay
matched, **1** = FAIL / record tampered / replay did not reproduce,
**2** = usage or output error. Every error in the third class — a
missing candidate directory, a candidate path that is a file, an
unwritable `--out` / `--patched-out` path, a malformed record — is
reported as a one-line message on stderr, never as a traceback.
`verifygate --version` prints the package version, and the package
installs a `verifygate` console script (`pip install .`) that behaves
identically to `python3 -m verifygate` from any working directory;
the clean-tree rehearsal exercises both invocation forms.

## Verifying an evidence record

A record's whole point is that a *third party* can check it without
trusting the producer — and without re-running anything:

```bash
python3 -m verifygate verify --record samples/evidence_real_repair.json \
    --spec samples/spec.json --candidate samples/failing
#   attempt 0: verdict FAIL, mode real, spec stringtools-slugify-fix — chain OK; candidate: match; spec: match; report summary: match
#   attempt 1: verdict PASS, mode real, spec stringtools-slugify-fix — chain OK; candidate: not bound (patched copy); spec: match; report summary: match
# RECORD INTACT: 2 record(s) verified
```

`verify` recomputes the hash chain and signature of every record (for a
repair report, every attempt's embedded record) and exits 0 only when
all are intact. Two optional bindings answer "does this record describe
*these* artifacts?": `--spec` recomputes the spec file's hash and
`--candidate` the candidate directory's tree hash, each compared
against the sealed header. For a repair report the candidate binding
applies to attempt 0 (the submitted candidate); later attempts verify
patched copies that no longer exist on disk, so they are reported as
not bound rather than mismatched. A repair report's own summary fields
(`final_verdict`, `spec_name`, `mode`, and each attempt wrapper's
`verdict` / `record_hash`) sit outside the chains, so they are
cross-checked against the sealed records they summarise — flipping a
stated final verdict to disagree with the sealed attempts fails
verification just like editing a record. The cross-check also covers
the two quoted figures: `llm_usage_total` is recomputed from the
sealed per-attempt usage entries plus the report's unsealed-calls
list, and `rounds_used` from the attempts' round numbers and the
preserved rejected patch — rewriting the token bill or the round
count fails verification too, and the web view prints the derived
figures (with a mismatch banner) rather than the rewritten ones.
Tampering with a verdict, a check result, a diagnosis, or a sealed
token-usage figure fails verification (exit 1); usage errors exit 2.
No checks are re-run, and no keys or network are needed.

## Replaying an evidence record

`verify` proves a record is intact; **replay proves it is
reproducible**. `verifygate replay` re-runs the checks and compares
the verdict and every per-check verdict/exit code against the sealed
record, and checks that the candidate's tree hash still matches the
sealed header:

```bash
python3 -m verifygate replay --record samples/evidence_real_repair.json \
    --spec samples/spec.json --candidate samples/failing --mode judge
#   attempt 0: expected FAIL, replayed FAIL — chain OK; candidate tree: match; checks: match; spec: match
#   attempt 1: expected PASS, replayed PASS — chain OK; candidate tree: match; checks: match; spec: match
# REPLAY MATCHED: 2 record(s) reproduced (mode judge)
```

The supplied spec is bound to the record just like the candidate:
its hash must equal the sealed header's `spec_hash`, so replaying
with a different task that merely runs the same check commands
reports `spec: MISMATCH` and does not reproduce.

For a repair report, each patched attempt is *reconstructed* — the
report carries the patch that produced it, so replay applies the
recorded patches in order to fresh copies of the original candidate
(the original is never modified) and re-verifies each reconstructed
tree; a tree whose hash differs from the sealed header did not
reproduce the attempt. Earlier reports pointed a patched attempt's
`replay` field at the ephemeral temp copy the loop had already
deleted; that field now names this command (it is top-level metadata,
not part of the hash chain, and the two committed real-mode reports
have been corrected in place — their chains still verify).

The replay mode is the caller's choice: verdicts come from the
deterministic checks, which are mode-independent, so a real-mode
record replays in Judge Mode with zero keys and zero model calls
(only the diagnosis words would differ, and words are never
compared). A candidate edited after the fact, or a tampered record,
does not reproduce (exit 1); usage errors exit 2.

## Layout

```
verifygate/   spec, sandbox, validators, evidence, patch application,
              repair loop, record verification, record replay,
              llm adapters, cli
samples/      fictional demo task + passing / failing candidates
samples-js/   second fictional task (JavaScript/Node) proving the
              harness is language-agnostic, incl. a committed
              real-mode (Nemotron) repair report
tests/        pytest suite for VerifyGate itself
dogfood_spec.json / evidence_self_check.json
              VerifyGate's self-verification spec + its PASS record
```

## Tests

```bash
python3 -m pytest -q
```

CI (`.github/workflows/verify.yml`) runs the suite and then the full
clean-tree rehearsal on every push and pull request.

## Hosted demo

`demo/` is a static, self-contained site (no JavaScript, no external
resources) deployable to any static host: `demo/index.html` explains the
project and the Judge Mode quickstart, shows the standalone
`verifygate verify` check and the `verifygate replay` reproduction
check, and links to both rendered real-mode
Nemotron repair reports — `demo/evidence_real_repair.html` (Python
sample, FAIL → PASS, 5,367 tokens sealed) and
`demo/evidence_js_real_repair.html` (JavaScript sample, FAIL → PASS,
6,390 tokens sealed) — with their hash chains verified on the page.
No candidate code is executed by the hosted demo.

## Clean-tree rehearsal

```bash
scripts/rehearse.sh
```

Exports the tracked tree (`git archive HEAD` — exactly what a fresh
clone contains) into a fresh directory and runs the whole Judge Mode
spine there: the pytest suite, PASS and FAIL sample runs with their
expected exit codes, the FAIL → PASS repair loop, the evidence web view
of the committed real-mode report, a dogfood run of VerifyGate on its
own tree, a `verifygate verify` check of every record written
during the rehearsal plus the committed real-mode reports for the
JavaScript sample and the chain-v2 Python report, a
`verifygate replay` reproduction of the rehearsal's repair report
and all three committed real-mode repair reports, and
an installed-package smoke test (`pip install` the export, then run
the `verifygate` console script from a different directory).

## License

MIT — see `LICENSE`.

## Version

1.9.0 — permission-bit fidelity: the sandbox workspace copy now
preserves each file's permission bits (before this, the copy was
content-only, so an executable script arrived non-executable and a
check invoking it directly — `./check.sh` — sealed a FAIL with exit
126 "Permission denied" while the same candidate passed on disk;
the reverse, inventing executability, is equally impossible now).
The patch applier honours git mode lines — `new file mode 100755`
creates an executable file, an `old mode`/`new mode` pair (including
a mode-only diff with no hunks) chmods an existing one — so a repair
loop fix that ships a script is verified the way it was proposed.
1.8.0 — unsealed-summary + replay-spec hardening: a repair report's
quoted figures are now recomputed from its sealed attempts during
`verifygate verify` (the `llm_usage_total` from the sealed usage
entries plus the unsealed-calls list, `rounds_used` from the attempt
round numbers and the preserved rejected patch) — before this,
rewriting a 5,367-token bill as 1 token or 1 round as 99 still
verified RECORD INTACT, and the web view printed the rewritten
figures; the view now derives the figures and flags a disagreeing
summary. And `verifygate replay` now binds the supplied spec to the
record's sealed `spec_hash` — a different task running the same
check commands no longer reports REPLAY MATCHED.
1.7.0 — spec-validation hardening: the task-spec parser now rejects
malformed specs as usage errors (exit 2) before any check runs,
closing four holes reproduced against 1.6.0 — a whitespace-only check
command was accepted and sealed a *vacuous PASS* (the shell exits 0
on empty input); a non-string command crashed `run` with an
unhandled traceback; `timeout_s: true` was silently accepted as a
1-second timeout (`bool` is an `int` subclass), and absurd timeouts
were unbounded (now capped at 3600 s); a non-string check name or
brief was sealed verbatim into the record. Record structure
verification mirrors the parser, so a record carrying a non-string
or whitespace check name/command no longer verifies, while every
untampered committed record still does.
1.6.0 — chain-v2 real-mode proof: the flagship repair loop is now
proven under the hardened chain with a live model, not only in
Judge Mode — `samples/evidence_real_repair_v2.json` (Nemotron 3
Super 120B: FAIL → a rejected diff fed back → PASS, 3 calls, 11,726
tokens sealed, both records `chain_version: 2`) is pinned by tests
and verified + replayed in the clean-tree rehearsal. The run also
fixed an adapter failure mode: a reasoning model can spend its
entire token budget deliberating inside the reply content
(finish_reason=length) and never emit a diff, so the patch prompt
now explicitly orders silent reasoning and names that failure.
Cumulative sealed real-mode usage across committed records: 24,616
tokens.
1.5.0 — chain-integrity hardening: chain v2 seals each entry's kind
together with its content, and verification enforces the record
structure (kinds, order, per-kind required fields) for both chain
versions — closing a relabel hole in which a FAIL check entry in a
legacy (v1) record could be re-tagged as a diagnosis without breaking
its chain, while every untampered legacy record still verifies.
`verifygate verify` now also cross-checks a repair report's unchained
summary fields (stated final verdict, per-attempt wrapper verdicts
and record hashes, spec name, mode) against its sealed attempt
records, and the evidence web view derives its repair-report verdict
badges from the sealed records, rendering a visible SUMMARY MISMATCH
banner when a stated summary disagrees. 18 new integrity tests
(135 total).
1.4.0 — CLI contract hardening: every subcommand now reports usage
and output errors (missing or non-directory candidate, unwritable
`--out` / `--patched-out`, malformed record) as a one-line stderr
message with exit code 2, never as a traceback — before this release
`run` on a missing candidate and every unwritable `--out` crashed
with one, contradicting the documented exit codes. `verifygate
--version` added. The clean-tree rehearsal gains an
installed-package step (`pip install` the export, run the console
script from another directory), so the pip-installed entry point is
exercised exactly as a judge would use it (10 steps).
1.3.0 — true replay: `verifygate replay` re-runs a record's checks
and compares verdicts (repair attempts are reconstructed from the
original candidate plus the patches recorded in the report, tree
hashes compared against the sealed headers), so "replayable" is now
an executable command instead of a printed string. The repair loop
no longer writes dead `/tmp` paths into patched attempts' `replay`
fields, and the two committed real-mode reports' fields were
corrected in place (unchained metadata; chains still verify). The
hosted demo documents the command; the rehearsal gains a replay
step (9 steps).
1.2.0 — hosted demo catches up with the real-mode evidence: the demo
site now also renders the JavaScript sample's real Nemotron repair
report (`demo/evidence_js_real_repair.html`, chains verified on the
page, 6,390 tokens sealed) and documents the standalone
`verifygate verify` check on the landing page, so a judge arriving
from the hosted demo sees both languages' real-mode proof and the
third-party verification command without cloning first.
1.1.0 — real mode proven language-agnostic: a committed real-mode
repair report for the JavaScript sample (`samples-js/evidence_real_repair.json`)
shows Nemotron 3 Super 120B repairing the Node candidate FAIL → PASS
(2 calls, 6,390 tokens sealed in the hash chains, the model's own fix),
pinned by a test and verified in the clean-tree rehearsal.
1.0.0 — workspace lifecycle fix (temp copies are deleted, never
leaked — a dogfood run once filled the temp filesystem with orphaned
sandbox copies) plus a language-agnostic proof: a second bundled sample in
JavaScript/Node (`samples-js/`, Node's built-in test runner as the
checks) runs the full spine — PASS, FAIL + diagnosis, and a Judge Mode
repair FAIL → PASS — with committed evidence records; the clean-tree
rehearsal gains a JavaScript step (8 steps), and a GitHub Actions
workflow runs the suite + rehearsal on every push/PR.
0.9.0 — standalone record verification: `verifygate verify` lets any
third party recompute a record's (or a repair report's) hash chain and
signature and bind it to the spec file and candidate directory on disk,
without re-running any check and without keys or network; the
clean-tree rehearsal now chain-checks its records through this CLI.
0.8.0 — in-repo dogfooding: `dogfood_spec.json` and the committed
self-check PASS record (`evidence_self_check.json`) now live at the
repository root, so a fresh clone can reproduce VerifyGate verifying
its own codebase with one command; the clean-tree rehearsal gains a
dogfood step (8 steps).
0.7.0 — Tavily diagnosis hardening: the reference lookup can never
break a verification run (missing key, network/HTTP failure, or
malformed response all degrade to no references, with the reason
sealed into the record as `reference_error` and rendered by the
evidence web view); transport injected for zero-network tests.
0.6.0 — hosted Judge-Mode demo (`demo/`), clean-tree rehearsal script
(`scripts/rehearse.sh`, all six steps green from a `git archive`
export), MIT `LICENSE` file, and publication readiness (see
`PUBLICATION_CHECKLIST.md`).
