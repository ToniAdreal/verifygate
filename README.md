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
120B, 2 calls, 5,367 tokens, both attempt chains valid).

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

## License

MIT (to be added at repository publication; this is a local build repo).

## Version

0.3.0 — real Token Factory adapter wired into the verify/repair loop
(connector-surrogate auth, usage sealed in the evidence chain,
rejection-feedback retries); Judge Mode unchanged and fully offline.
