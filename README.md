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
the diagnosis entry when no Tavily key is configured.

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

## Evidence records

Each run emits a JSON record binding the spec hash, the candidate tree
hash, and every check result (command, exit code, duration, output tails).
Entries are hash-chained and the record hash is HMAC-signed
(`VERIFYGATE_SIGNING_KEY`; a clearly-labelled public dev key is the
default). `verifygate.evidence.verify_chain()` detects any edit, and the
record's `replay` field is the exact command to reproduce the verdict.

## Verifying an evidence record

A record's whole point is that a *third party* can check it without
trusting the producer — and without re-running anything:

```bash
python3 -m verifygate verify --record samples/evidence_real_repair.json \
    --spec samples/spec.json --candidate samples/failing
#   attempt 0: verdict FAIL, mode real, spec stringtools-slugify-fix — chain OK; candidate: match; spec: match
#   attempt 1: verdict PASS, mode real, spec stringtools-slugify-fix — chain OK; candidate: not bound (patched copy); spec: match
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
not bound rather than mismatched. Tampering with a verdict, a check
result, a diagnosis, or a sealed token-usage figure fails verification
(exit 1); usage errors exit 2. No checks are re-run, and no keys or
network are needed.

## Layout

```
verifygate/   spec, sandbox, validators, evidence, patch application,
              repair loop, record verification, llm adapters, cli
samples/      fictional demo task + passing / failing candidates
samples-js/   second fictional task (JavaScript/Node) proving the
              harness is language-agnostic
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
project and the Judge Mode quickstart, and links to
`demo/evidence_real_repair.html`, the rendered real-mode Nemotron repair
report (FAIL → PASS, chains verified on the page). No candidate code is
executed by the hosted demo.

## Clean-tree rehearsal

```bash
scripts/rehearse.sh
```

Exports the tracked tree (`git archive HEAD` — exactly what a fresh
clone contains) into a fresh directory and runs the whole Judge Mode
spine there: the pytest suite, PASS and FAIL sample runs with their
expected exit codes, the FAIL → PASS repair loop, the evidence web view
of the committed real-mode report, a dogfood run of VerifyGate on its
own tree, and a `verifygate verify` check of every record written
during the rehearsal.

## License

MIT — see `LICENSE`.

## Version

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
