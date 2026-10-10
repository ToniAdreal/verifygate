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

VerifyGate dogfoods itself: `dogfood_spec.json` in the project root
runs the project's own pytest suite as a VerifyGate check, and the
resulting PASS record is kept next to it as `evidence_self_check.json`.

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

## Layout

```
verifygate/   spec, sandbox, validators, evidence, patch application,
              repair loop, llm adapters, cli
samples/      fictional demo task + passing / failing candidates
tests/        pytest suite for VerifyGate itself
```

## Tests

```bash
python3 -m pytest -q
```

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
of the committed real-mode report, and a hash-chain check of every
record written during the rehearsal.

## License

MIT — see `LICENSE`.

## Version

0.7.0 — Tavily diagnosis hardening: the reference lookup can never
break a verification run (missing key, network/HTTP failure, or
malformed response all degrade to no references, with the reason
sealed into the record as `reference_error` and rendered by the
evidence web view); transport injected for zero-network tests.
0.6.0 — hosted Judge-Mode demo (`demo/`), clean-tree rehearsal script
(`scripts/rehearse.sh`, all six steps green from a `git archive`
export), MIT `LICENSE` file, and publication readiness (see
`PUBLICATION_CHECKLIST.md`).
