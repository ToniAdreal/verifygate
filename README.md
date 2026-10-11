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
`--max-rounds` patch attempts. The budget itself is validated like a
spec timeout (v1.22.0): a real int between 0 and 10 — a bool or float
budget previously produced a report that fails its own verification
(and a float silently allowed more rounds than stated), a string
crashed with a `TypeError`, and an absurd budget was accepted verbatim
even though every round is a paid model call; `verify` likewise
rejects a report claiming a budget above the loop maximum. The repair report embeds every attempt's
full evidence record, so each intermediate verdict stays independently
replayable. In real mode the same loop drives Nemotron via Token Factory:
the model proposes diffs, deterministic checks still dispose.

Patch *generation* itself can fail — an outage, a malformed response,
an empty completion — and that failure is just another way the loop
stops (v1.20.0): the report is still delivered with attempt 0's sealed
FAIL record, the failed round counted as used, the failure preserved
as the report's `failed_patch` with `error_kind: generation_failed`,
and `stopped_reason: generation_failed` (the CLI exits 1 — a FAIL
verdict with its report — not 2). Verification re-derives that stop
from the preserved failure, so the generation/rejection distinction
cannot be rewritten after the fact.

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
verdict. The adapter validates every decoded API response field by
field before recording anything (v1.18.0): a malformed shape — a
`message` that is not an object, a non-string `content`, a `usage`
whose token counts are not non-negative ints — is a clean adapter
error (CLI exit 2), never an unhandled traceback and never a nonsense
usage entry sealed into a record. A diff that does not apply cleanly is rejected with the
applier's exact error, which is fed back to the model for the next
round instead of ending the loop. A real FAIL → repair → PASS report
is committed at `samples/evidence_real_repair.json` (Nemotron 3 Super
120B, 2 calls, 5,367 tokens, both attempt chains valid). A real-mode
FAIL record from the Nano model is committed at
`samples/evidence_real_fail.json` (1 call, 1,133 tokens sealed) — it
also shows the Tavily degradation note (`reference_error`) sealed into
the diagnosis entry when no Tavily key is configured. Its successor
`samples/evidence_real_fail_v2.json` (Nano, 1 call, 1,108 tokens
sealed, chain v3) is the first real-mode record sealed since the
diagnosis-resilience change (v1.19.0): its diagnosis entry carries
`diagnosis_error: null` — a healthy model diagnosis — beside the same
sealed Tavily degradation. The same real
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
(finish_reason=length) and never produced a usable diff.

Since v1.15.0 the chain is hardened once more (chain v3 also seals
each record's top-level metadata; see *Verifying an evidence
record*), and the flagship loop is proven under it in real mode
too: `samples/evidence_real_repair_v3.json` is a chain-v3 repair
report from Nemotron 3 Super 120B — attempt 0 FAIL with the model's
own diagnosis, a first diff rejected for a context mismatch and
fed back, a second diff applied, attempt 2 PASS (3 calls, 9,407
tokens sealed; both records carry `chain_version: 3`, both chains
verify, and the report replays end-to-end in Judge Mode).

The JavaScript sample now has the same chain-v3 real-mode proof:
`samples-js/evidence_real_repair_v3.json` is a chain-v3 repair
report from Nemotron 3 Super 120B — attempt 0 FAIL with the model's
own diagnosis (tax rate ignored, no cent rounding), its first diff
applied to a copy of `cart.js`, attempt 1 PASS (2 calls, 5,318
tokens sealed; both records carry `chain_version: 3`, both chains
verify, and the report replays end-to-end in Judge Mode). Real
mode under the newest chain is therefore proven in both bundled
languages. Total sealed real-mode usage across all committed
records: 40,449 tokens.

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

References are also bounded and normalised before they are sealed
(v1.26.0): at most 3 per diagnosis, each exactly
{title, url, snippet, source} with per-field length caps — the
runner normalises whatever any lookup implementation returns on
the one sealing path, so malformed or flooding results can neither
produce a record that fails its own verification nor bloat one,
and the chain's structure check mirrors the same bounds.

The diagnosis text itself is bounded the same way (v1.27.0): at
most 20,000 characters, truncated on the same sealing path with
the truncation marked in the sealed text. Before this, an adapter
returning a 500,000-character explanation sealed a ~500 KB record
that verified intact, and even the bundled mock (which renders per
failing check) could produce ~67,000 characters on a maximum-size
spec. The cap is roughly ten times the longest committed real-mode
diagnosis, so no honest diagnosis is ever cut — and the chain's
structure check mirrors it, so an over-limit record fails
verification however it was sealed.

A spec's own text is bounded too (v1.28.0): the spec name and each
check name at 200 characters, the brief at 10,000, and each check
command at 4,000. Before this, a probe spec carrying a
100,000-character name, a 500,000-character brief, and
100,000-character check names/commands was accepted verbatim —
and the name and check names/commands are sealed into every
record the spec produces, while the brief is embedded verbatim in
every real-mode patch prompt. The parser rejects over-ceiling
text as a usage error before any check runs, the chain's
structure check mirrors the sealed fields' caps, and the same
mirror covers the checks' output tails (already capped on the
sealing path by the sandbox's output limit plus its truncation
marker). The bundled specs' longest fields — a 148-character
brief, 20-character commands — sit far below every ceiling.

The diagnosis and usage entries' metadata is bounded too
(v1.29.0): a diagnosis entry's adapter/lookup names (at most 100
characters — they are code-defined identifiers) and its error
notes (at most 500 — the runner formats its own from exception
text truncated to 200, and a lookup's own note is now truncated on
the same sealing path), and an `llm_usage` entry's calls (at most
50 per entry — one record can seal at most the repair loop's
round budget of patch calls, each at most doubled by the real
adapter's empty-completion retry, plus one explanation; the
committed maximum is 2), each call's adapter/model/purpose/
finish-reason strings within their ceilings. Before this, a
record sealed over 500,000-character adapter and error strings
plus 50 calls carrying 500,000-character model names verified
intact at ~100 MB. The Token Factory adapter also falls back to
its configured model when the API returns an over-long model
string, exactly as it already did for a non-string one, and the
chain's structure check mirrors every ceiling, so an over-limit
record fails verification however it was sealed.

The diagnosis itself degrades the same way (v1.19.0). The LLM
explanation is commentary too, so a failed explanation call — a
network error, a malformed Token Factory response, an empty
completion after the retry — can no longer destroy a verdict the
checks already decided: the runner seals a deterministic fallback
explanation rendered from the real check results, plus a
`diagnosis_error` note in the diagnosis entry, and any usage the
adapter recorded before failing is still sealed. Before this, that
one unguarded call cost a FAILed candidate its entire evidence
record (the CLI reported an adapter error and wrote nothing), aborted
the repair loop on its first attempt, and made replaying a FAIL
record in real mode report "not reproduced" even when every check
reproduced exactly.

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
- **Candidate size bounds** — a candidate may carry at most 10,000
  verified files totalling at most 256 MiB; the bound is checked
  before any copy is made and again in the tree hash, so an
  oversized candidate (a vendored dependency tree, one huge binary)
  is a one-line usage error at every entry point, not a temp
  filesystem filled one copy per repair round. A repair patch is
  held to the same ceilings: the staged result is measured before
  anything is written, and a patch that would grow the candidate
  past them is a clean rejection the loop feeds back — never an
  oversize tree whose re-verification destroys the repair report.
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
1 and 3600 (JSON `true` is not accepted as 1), and a spec may carry at
most 100 checks (`MAX_CHECKS`) — checks run sequentially, each with
its own timeout, and every result is sealed into the record, so the
count is bounded like every other dimension of a run; verification
mirrors the cap, so a record claiming more checks than a valid spec
can carry fails too. A malformed spec is a
usage error — one-line message, exit 2 — never a traceback or a
silently reinterpreted run.

## Evidence records

Each run emits a JSON record binding the spec hash, the candidate tree
hash, and every check result (command, exit code, duration, output tails).
Entries are hash-chained and the record hash is HMAC-signed
(`VERIFYGATE_SIGNING_KEY`; a clearly-labelled public dev key is the
default). `verifygate.evidence.verify_chain()` detects any edit, and the
record's `replay` field is the exact command to reproduce the verdict.

Records sealed by this version carry `chain_version: 3`: every entry's
hash covers its *kind* as well as its content (chain v2), and the
record hash additionally seals the record's top-level metadata —
the `replay` command, the dev-key disclosure, the emitting version,
and the timestamp (v1.15.0; see *Verifying an evidence record*).
Chain v1 hashed the content only, so an entry could be relabelled (a FAIL check re-tagged
as a "diagnosis", which consumers render as commentary) without
breaking the chain; verification now also enforces the record's
structure — kinds, their order, and the fields each kind must carry —
for all chain versions, so a relabelled legacy record fails too,
while every untampered legacy record (including all the committed
real-mode evidence) still verifies exactly as before. The header
entry is hashed over its full content and must carry exactly the
fields the builder emits (v1.11.0 closed an injection hole there:
an extra header field previously authenticated against nothing).
Structure validation also checks *consistency* (v1.12.0): each
check's verdict is recomputed from its exit code and timeout, the
record's verdict from its checks, and a usage entry's total from
its own calls — so a record sealed (or re-chained and re-signed
with the public dev key) claiming PASS over FAIL checks, or
quoting a token total its calls do not add up to, fails
verification even though its chain recomputes.

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
The same holds for the report's narrative (v1.13.0): why the loop
stopped, the round budget, each attempt's patch linkage, the
preserved rejection, and the credited adapter are all re-derived
from the sealed attempts — a PASS report claiming it stopped at
max rounds, an invented rejection, or a renumbered attempt fails
verification, and the web view shows the derived stopped reason.
And every attempt must have been sealed under the same spec
(v1.14.0): the loop never changes the task, so an attempt record
sealed under a different spec — same name and mode, different
brief or checks — spliced into a report fails verification and
joins the web view's mismatch banner.
A record's own top-level metadata is sealed too (v1.15.0, chain
v3): the `replay` command a reader is told to run, the
`signing_key_is_default_dev_key` disclosure, the emitting
`version`, and `created_at` are covered by the record hash, so
replacing the replay string with an arbitrary shell command or
flipping the dev-key flag breaks verification. Legacy (v1/v2)
records, whose metadata predates the sealing, are held to the
consistency rules a genuine record always satisfies instead:
exact field set, semver version, timezone-aware timestamp, the
flag matching the key that verifies the signature, and a replay
string matching one of the two emitted command templates in the
header's mode, with no shell metacharacters in its labels. Labels
that are not bare path tokens — a path with a space, a quote, or a
non-ASCII character — are emitted single-quoted (`shlex.quote`
form, v1.21.0), which the templates also accept; an unquoted label
carrying whitespace or metacharacters is still rejected.
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
JavaScript sample (including its chain-v3 report) and the chain-v2
and chain-v3 Python reports, a
`verifygate replay` reproduction of the rehearsal's repair report
and all five committed real-mode repair reports, and
an installed-package smoke test (`pip install` the export, then run
the `verifygate` console script from a different directory).

## License

MIT — see `LICENSE`.

## Version

1.42.0 — adapter validation: the `llm` and `references`
arguments were the last run parameters taken on trust.
`None`, a string, or a bare object on a PASS candidate
sealed a verdict with no adapter at all (the diagnosis
path is skipped and the usage drain is guarded, so nothing
noticed), and on a FAIL candidate crashed with an
unhandled `AttributeError` on the adapter's `name` after
its guarded advisory call — losing the record the checks
had already decided; in the repair loop the same crash
happened at `llm.name` before the first run, with no
report, and in replay it escaped mid-replay past the
per-record `OSError`/`RuntimeError` handling. Every entry
point — the runner, the repair loop, and replay — now
validates both adapters before any work, through
`llm.validate_llm` (a callable `explain_failure` and
`generate_patch` are required) and
`tavily.validate_references` (a callable `lookup` is
required): a malformed adapter is a `ValueError` (CLI
exit 2). Validation is duck-typed and about shape, not
behaviour — subclassing is not required, `name` /
`last_error` / the usage-log methods stay optional, and
an adapter whose methods raise still degrades under the
diagnosis/generation resilience rules. 114 new tests
(1118 total).
1.41.0 — candidate-directory validation: the candidate
directory itself was the last run parameter converted with
`Path(...)` unchecked. A non-path value (`None`, an `int`, a
list, `bytes`) crashed with an unhandled `TypeError` — in the
runner before its checks, in replay mid-replay (replay
validated `str(candidate_dir)`, a stringification, so a bad
value passed that check), and in `verify`'s candidate binding
inside `tree_hash`. An empty string silently became the
*current* directory (`Path("")` is `"."`): a run verified
whatever tree the caller happened to stand in and sealed its
hash under an unrelated candidate label, while `verify`
hashed the same wrong tree and reported a binding mismatch
instead of a usage error. A regular file raised
`FileNotFoundError` from the workspace copy but
`NotADirectoryError` from the tree hash. And an over-long
path produced a repair report whose stored attempt
directories exceed the 1,000-character cap verification
enforces over them. Every entry point — the runner, the
repair loop, replay, `verify`'s binding, and `tree_hash` /
`prepare_workspace` as backstops — now validates the
directory through `sandbox.validate_candidate_dir` before
any work: a non-string/non-Path, empty, or
over-1,000-character value is a `ValueError`, a missing /
not-a-directory / unstatable value is the one
`FileNotFoundError`, and the ceiling constant is now defined
once, in `sandbox`, with `verify` importing it. 116 new
tests (1004 total).
1.40.0 — label validation: the spec/candidate labels were
the last caller-supplied run parameters with no validation
at all. They are interpolated through `quote_label` into
the record's sealed `replay` string, and `quote_label`
assumed a string: an `int`, `None`, or list label crashed
with an unhandled `TypeError` — in the runner and the
repair loop after every check had already run (no record
delivered), and in replay mid-replay, where the per-record
path catches only `OSError`/`RuntimeError`. An empty label
sealed the quoted empty string as if it were a path, and a
label's length was unbounded: a 500,000-character label
sealed a 500,000-character `replay` field that verified
INTACT, because the metadata rules judged the replay
string only against templates that cap no length. Every
entry point now validates both labels through
`evidence.validate_label` before any workspace is
prepared, any check runs, or any record is replayed (a
non-string, empty, or over-1,000-character label is a
`ValueError`); `quote_label` applies the same validation
as a backstop, and `_metadata_ok` mirrors the bound with
`MAX_REPLAY_CHARS` (16,384 — the largest replay string
validated labels can produce), so a record carrying a
longer replay string fails verification however it was
sealed. 77 new tests (888 total).
1.39.0 — spec-object validation: every spec rule was
enforced only by `parse_spec`, i.e. only on the JSON path —
but `TaskSpec`/`Check` are plain dataclasses, so a library
caller could construct one directly and bypass them all: a
whitespace-only command sealed a vacuous PASS, a bool or
float `timeout_s` ran as 1 / 2.5 seconds, an empty check
list and a 500,000-character name each sealed a record that
failed its own verification, and with `raw` empty (the
dataclass default) the sealed `spec_hash` was the hash of
`{}`, binding none of the fields that actually ran. The
runner, the repair loop, and replay now call
`spec.validate_spec` before any work: the fields must
satisfy the same rules `parse_spec` enforces, and `raw`
must parse back to exactly those fields, so the sealed
hash always binds the spec that ran. 52 new tests
(811 total).
1.38.0 — mode validation: the run mode was validated only
by the CLI's argparse choices — every library entry point
accepted any value verbatim and sealed it into the record
header and the replay string, so mode `bogus`, `judge `
(trailing space), an empty string, `None`, a number, or a
shell fragment (`judge; echo pwned` — a second command
smuggled into the string a reader is told to copy and run)
produced a fully-run PASS record that failed its own
verification the moment it was written, a repair report
full of such records, and a CLI adapter factory that
silently treated any non-`judge` mode — including one read
from a record's own untrusted header on the replay path —
as real mode, selecting paid adapters instead of reporting
a usage error. The runner, the repair loop, replay, and
the adapter factory now validate through one helper
(`evidence.validate_mode`) before any workspace is
prepared, any check runs, or any adapter is chosen: an
invalid mode is a `ValueError` (CLI exit 2), never a run.
82 new tests (759 total).
1.37.0 — patch-input bound: the repair loop applied a
proposal of any size but stored only its first 8,000
characters in the report, so a valid 9,413-character diff
applied, passed, and verified INTACT while its recorded
patch was a truncated fragment replay could not reconstruct
the attempt from — the report's replay promise was false for
exactly the patches too large to store. The patch text is
now bounded at the applier, before parsing, by one generous
ceiling (65,536 characters — a full maximum-token model
response with headroom; committed real-mode patches peak at
571) that is also the stored cap and `verify`'s mirror: an
oversize proposal is a clean `PatchError` rejection (stored
truncated, fed back, retried within the round budget), and
every applied patch is stored in full and replays. A
non-string patch text is likewise a `PatchError`, never a
`TypeError`. 8 new tests (677 total).
1.36.0 — JSON input file bounds: every loader read its file
in full with no size bound, while every field inside those
documents was already capped — a spec file padded to 5 MiB
ran to PASS and an evidence record padded to 70 MiB verified
INTACT, both loaded into memory whole, so a hostile file
could exhaust the verifier asked to judge it before any
field check ran. Spec and record files now load through a
bounded loader that reads at most the cap plus one byte
(spec 4 MiB, record 64 MiB — generous over what a document
at every field ceiling serialises to) and rejects an
oversize file as a clean usage error (exit 2). 13 new tests
(669 total).
1.35.0 — usage sealing normalisation: the runner sealed
adapter-supplied metadata verbatim — an adapter whose name was
empty or 500,000 characters long, or which recorded a usage
call with a 500,000-character model string, an int finish
reason, or a non-object usage (which crashed the record build
outright) — so a record could fail its own verification the
moment it was written, and a 500,000-character *configured*
Token Factory model id reached the same failure through the
fallback path. The sealing paths (the runner for records, the
repair loop for report snapshots and unsealed usage) now
normalise names and usage calls through shared evidence
helpers, and the Token Factory adapter rejects an empty or
over-long configured model id at construction.
1.34.0 — Token Factory finish-reason normalisation: the
real adapter recorded a response's `finish_reason` verbatim —
the one decoded field the v1.18.0 response-shape hardening and
the v1.29.0 model-name cap both missed — so a response carrying
an int, object, list, bool, or 500,000-character finish reason
produced a usable completion whose call was sealed into a
record that failed its own verification (the structure check
requires a string within 100 characters, or None). The adapter
now normalises the field when the response is decoded, exactly
like the model name: a string within the cap is kept verbatim,
anything else is recorded as None, and a record sealed over any
finish-reason shape verifies. 36 new tests (613 total).
1.33.0 — check-result numeric bounds: a check entry's
numbers were the last unbounded values in a record — its
duration was checked only as a non-negative int (a record
sealed over a duration of 10**12 ms verified INTACT, though
a check is killed at its timeout, whose ceiling is one
hour), a timeout entry could carry an exit code (the sandbox
kills a timed-out check before it can exit and seals None), a
completed check could carry none, and an exit code of
999,999 (no POSIX exit status or signal number) all verified
INTACT. The structure check now mirrors the sandbox's
emission contract: duration at most twice the longest legal
timeout in milliseconds, exit code None exactly when the
check timed out, otherwise a real int in -255..255, while
every committed record still verifies. 18 new tests (577
total).
1.32.0 — report shape bounds: a repair report's shape itself
was never judged — a single record's top-level fields are held
to an exact field set, but a repair report (unchained at the
top level) could carry any extra field at any level: a
500,000-character string injected as a new top-level field, a
new attempt-wrapper field, or a new patch-linkage field all
verified INTACT, as did a rewritten `kind`, a
500,000-character `version`, and an attempt list far longer
than the loop's round budget can produce. The narrative check
now holds the report to the loop's field vocabularies
(top-level, attempt, and patch linkage — by subset, so a
legacy report omitting a later-added field is not failed for
the omission), requires `kind` to be `repair-report` and
`version` to be a semver string when present, and caps the
attempt count at the loop maximum (11). 14 new tests (559
total).
1.31.0 — report-level bounds: a repair report's
`unsealed_llm_usage` list was totalled by `verify` but never
bounded — a coordinated edit (inflating the list and the
stated total together) carrying 5,000 calls with
500,000-character model strings still verified INTACT, as
did each attempt's `candidate_dir` inflated to 500,000
characters. The narrative check now bounds the unsealed list
by the same sealed-call ceilings as a patch linkage's usage
snapshot (at most 50 calls, per-field string caps), caps
every attempt's `candidate_dir` at 1,000 characters, and caps
the report's own `spec_name` / `llm_adapter` at the sealed
ceilings as defence in depth, while every committed report
still verifies. 12 new tests (545 total).
1.30.0 — repair-report patch bounds: a repair report's patch
linkage — the preserved `patch_text`, the patch `error`
strings, the `changed_files` lists, and each linkage's
`llm_usage` snapshot — sits outside every hash chain and was
judged only for type, so inflating any of it with
500,000-character strings (the changed-file list and usage
snapshot were not judged at all) still verified INTACT. The
loop's source caps are now named constants (`patch_text`
truncated at 8,000 characters, a stored patch error at
2,000 — an applier message embeds the path the proposal
chose, truncated at the source), and `verify`'s narrative
check mirrors them plus generous ceilings over the lists
(at most the candidate file ceiling of changed files, each
name at most 1,000 characters) and the usage snapshots (the
sealed-call ceilings), while every committed report (patch
texts peak at 571 characters, errors at 92) still verifies.
Found on the way: a patch target whose name component is
over the filesystem limit raised a raw `OSError`
(ENAMETOOLONG) out of the applier's stat calls, escaping the
repair loop's `PatchError` handling — unstatable,
unresolvable, and uncreatable targets are now clean
rejections, raised before anything is staged or written.
14 new tests (533 total).
1.29.0 — diagnosis/usage metadata bounds: a diagnosis entry's
adapter/lookup names and error notes, and an `llm_usage` entry's
calls (their count, and every string they carry), were the last
unbounded text inside a record — a record sealed over
500,000-character adapter/error strings plus 50 calls with
500,000-character model names verified INTACT at ~100 MB. The
structure check now mirrors generous ceilings over all of it
(names 100 characters, error notes 500, at most 50 calls per
entry), the runner truncates a lookup's `last_error` on the
sealing path, and the Token Factory adapter falls back to its
configured model for an over-long API model string, while every
committed fixture (names peak at 20 characters, 2 calls per
entry) still verifies. 21 new tests (519 total).
1.28.0 — spec text bounds: every other dimension of a spec was
capped (check count, per-check timeouts, candidate size) but the
length of its text was not — a probe spec with a 100,000-character
name, a 500,000-character brief, and 100,000-character check
names/commands was accepted verbatim, and a record sealed over
those fields (plus a 500,000-character output tail) verified
INTACT. The parser now caps the spec name and check names at 200
characters, the brief at 10,000, and check commands at 4,000
(generous: the bundled specs peak at a 148-character brief and
20-character commands), rejecting more as a one-line usage error
before any check runs or any patch prompt is built; the evidence
structure check mirrors the sealed fields' caps and the sandbox's
output-tail cap, so an over-limit record fails verification
however it was sealed, while every committed fixture still
verifies. 20 new tests (498 total).
1.27.0 — explanation bounds: the diagnosis text was the last
unbounded text on the sealing path — the runner sealed whatever
`explain_failure` returned, verbatim and at any length (a stub
adapter's 500,000-character explanation sealed a ~500 KB record
that verified intact), and even first-party text could run long:
the bundled mock renders per failing check, so a maximum-size
all-failing spec produced a ~67,000-character explanation, and the
runner's own fallback ~33,000. The explanation now passes through
`runner.normalise_explanation` on the one sealing path — model,
mock, and fallback alike — truncated to
`MAX_EXPLANATION_CHARS` (20,000, roughly ten times the longest
committed real-mode diagnosis) with the truncation marked in the
sealed text; the evidence structure check mirrors the cap, so an
over-limit record fails verification however it was sealed, while
every committed fixture still verifies. 12 new tests (478 total).
1.26.0 — reference bounds: the diagnosis references were the one
unbounded, unnormalised input left on the sealing path. The real
Tavily adapter never sliced its response to the requested
`max_results` (a response carrying 500 results returned all 500,
titles and URLs unbounded), and the runner sealed whatever any
lookup implementation returned verbatim — malformed references
(non-object items, non-string fields, extra keys) produced a
record that failed VerifyGate's own chain verification the moment
it was written, while 500 well-formed references sealed and
verified INTACT. One normalisation helper
(`tavily.normalise_references`) is now shared by the adapter and
the runner: the adapter slices to the requested count (hard
ceiling 10, an invalid request falls back instead of raising) and
truncates every field; the runner — the one sealing path — seals
at most 3 normalised references from any lookup implementation,
and a non-list return degrades to no references with the
malformation noted in the record; the evidence structure check
mirrors the sealing bounds, so an over-limit record fails
verification however it was sealed, while every committed fixture
still verifies. 17 new tests (466 total).
1.25.0 — patch-result bounds: the candidate size ceilings bound a
*submitted* candidate, but a repair patch could grow one past
them — `apply_patch` staged and wrote a diff of any size, and the
patched copy then failed the workspace pre-flight on
re-verification with a `CandidateTooLargeError` that is not a
`PatchError`: it escaped the repair loop, destroyed the whole
report (attempt 0's sealed FAIL evidence included), and surfaced
at the CLI as a usage error (exit 2, nothing written) for a run
whose verdict was already decided (reproduced with a +5,001-byte
creation against a bound 100 bytes above the original candidate).
The patch applier now measures its staged result — the verified
files the containment traversal counts, with staged creations /
modifications / deletions folded over the on-disk baseline —
against the same ceilings (`MAX_CANDIDATE_FILES` /
`MAX_CANDIDATE_BYTES`, overridable per call) before the commit
loop writes anything, so an oversize patch is an atomic
`PatchError` rejection: the repair loop preserves it as a
rejected patch, feeds it back, and delivers its report (CLI exit
1), and replay reports it as a patch that no longer applies. 11
new tests (449 total).
1.24.0 — check-count bound: every other dimension of a run was
capped (a check's timeout, the repair loop's rounds, a candidate's
files and bytes), but a spec could carry an unbounded number of
checks — each runs sequentially, entitled to its full timeout, and
each result is sealed into the evidence record, so a 5,000-check
probe spec was accepted with an aggregate timeout budget of
~208 days and a record sealed over 5,000 checks verified INTACT.
A spec now carries at most 100 checks (`MAX_CHECKS` in `spec.py`;
the bundled samples carry 2, the dogfood spec 1 — fold a suite
into one gate command, as they do), rejected by the parser as a
usage error (exit 2) at every CLI entry point, and the evidence
structure check mirrors the cap, so a record claiming more checks
than a valid spec can produce fails verification for any chain
version. 11 new tests (438 total).
1.23.0 — candidate size bounds: nothing limited how large a
candidate could be — the sandbox copy, the tree hash, and every
repair round re-copied the candidate in full, so a candidate
carrying a vendored `node_modules` tree, build output, or one huge
binary filled the temp filesystem a copy at a time (a 12,000-file
probe candidate was accepted and copied in full; this project has
already filled its 512 MB temp filesystem in practice). Candidates
are now bounded at 10,000 verified files / 256 MiB total
(`MAX_CANDIDATE_FILES` / `MAX_CANDIDATE_BYTES` in `sandbox.py`),
counting exactly the files the containment traversal verifies.
The check is pre-flight in `prepare_workspace` (a rejected
candidate leaves no workspace behind) and in `evidence.tree_hash`,
so run, repair, `verify --candidate`, and replay all reject the
same oversized candidate as a one-line usage error (exit 2). 10
new tests (427 total).
1.22.0 — round-budget validation: the repair loop's `max_rounds`
was checked only for `< 0`, so a bool budget (`True` — an `int`
subclass) or a float (`2.5`) produced a report whose stated budget
fails `verify`'s own non-bool-int narrative rule: the loop emitted
reports that fail their own verification, and a float silently
allowed more rounds than stated (2.5 → 3). A string or `None`
crashed the comparison with an unhandled `TypeError` instead of
the documented `ValueError`, and no upper bound existed —
`--max-rounds 1000000000` was accepted verbatim, each round a paid
Token Factory call plus a full re-verification with a full evidence
record per attempt. The budget is now a real int in `0..10`
(`MAX_ROUNDS`) at the library and the CLI (usage error, exit 2),
and `verify`'s narrative check mirrors the bound, so a report
claiming a budget above the loop maximum fails verification and
the web view flags it in the SUMMARY MISMATCH banner. 19 new
tests (417 total).
1.21.0 — replay-label quoting: the runner interpolated the
caller's spec/candidate paths into the record's `replay` command
raw, while the metadata template only accepts bare path tokens —
so any path containing a space, a quote, or a non-ASCII character
(a checkout under `My Projects`, a Chinese home-directory name)
sealed a record that failed its own verification instantly: `run`
exited 0 with a PASS, `verify` on the record it had just written
reported CHAIN INVALID. Labels now go through
`evidence.quote_label`: bare tokens are emitted unchanged (every
ordinary replay string is byte-identical), anything else is
single-quoted in `shlex.quote` form — inert to the shell, still
copy-pasteable, and the emitted command actually runs (pinned by a
test that executes it, and by one showing a `$(touch …)` path
cannot weaponise it). The metadata templates accept both forms;
an *unquoted* label with whitespace or metacharacters still fails
verification. 12 new tests (398 total).
1.20.0 — generation resilience: the repair loop called
`generate_patch` unguarded — the one advisory call v1.19.0 left
exposed — so a real-adapter generation failure (network error,
malformed response, empty completion after the retry) or a
non-string return propagated out of the loop: attempt 0's
already-decided FAIL evidence was never returned, and the CLI
reported an adapter error (exit 2) with no report written.
Generation failure now ends the loop and the report is delivered:
the failed round counts as used, the failure is preserved as
`failed_patch` with `error_kind: generation_failed`,
`stopped_reason` is `generation_failed`, usage recorded before
the failure is surfaced as the report's unsealed usage, and the
CLI exits 1 with the report written. The preserved failure kind
also distinguishes an applier rejection (`error_kind:
patch_rejected`, deriving `patch_not_applicable`) from a
generation failure in verification: `verify` re-derives the
stopped reason from it, so flipping either field fails the
narrative check; pre-v1.20.0 reports carry no `error_kind` and
derive exactly as before. Unlike a rejected diff, a generation
failure is not retried — there is no proposal to give feedback
on. 15 new tests (386 total).
1.19.0 — diagnosis resilience: the diagnosis is commentary, never a
verdict input, but the runner called `explain_failure` unguarded —
so a real-adapter failure (network error, malformed response, empty
completion after the retry) destroyed the evidence record a
deterministically FAILed candidate had already earned (no record
written; the CLI exited 2 as an "adapter error"), aborted the repair
loop on its first attempt, and made a real-mode replay of a FAIL
record report "not reproduced" when only the words had failed. The
call is now guarded exactly like the Tavily lookup beside it: the
record seals a deterministic fallback explanation rendered from the
real check results plus a `diagnosis_error` field (null on a healthy
diagnosis), usage recorded before the failure is still sealed, and
the web view renders the degradation note. A fresh real-mode record
proves the new shape with a live model: `samples/
evidence_real_fail_v2.json` (Nemotron 3 Nano, FAIL, 1 call, 1,108
tokens sealed, `diagnosis_error: null` beside the sealed Tavily
degradation). One pre-existing test corrected — the v1.18.0
runner-propagation test pinned the old contract this change
removes; it now pins the degradation into the record instead.
10 new tests (371 total).
1.18.0 — Token Factory response-shape hardening: the real adapter
validated its decoded API response by indexing into it, so several
plausible malformed shapes (a `message` returned as a bare string
or `None`, `usage` as a string or list, a non-string `content`)
escaped as unhandled `AttributeError` tracebacks — crashing
`run` / `repair` in real mode instead of the clean adapter error
(exit 2) the CLI contract promises — and a `usage` dict carrying a
string, negative, float, or bool token count would have been
recorded verbatim and sealed into a record whose own consistency
rules fail that record's chain. `_chat` now validates the response
field by field (choice/message objects, string-or-absent content,
token counts non-negative ints or absent, a non-string `model`
falls back to the configured model) and raises one `RuntimeError`
per malformed shape, before anything is recorded. 43 new tests
(361 total).
1.17.0 — chain-v3 real-mode proof for the JavaScript sample: the
JS sample's only committed real-mode report was still a legacy
chain-v1 record, so the language-agnostic claim and the newest
chain had never been proven together with a live model —
`samples-js/evidence_real_repair_v3.json` (Nemotron 3 Super 120B:
FAIL → PASS in one round, 2 calls, 5,318 tokens sealed, both
records `chain_version: 3`) is pinned by tests and verified +
replayed in the clean-tree rehearsal; a test also pins the v3
property on the committed JS record itself (rewriting its replay
command or flipping its dev-key disclosure breaks its chain).
The hosted-demo landing page states the new sealed total.
Cumulative sealed real-mode usage across committed records: 39,341
tokens. 2 new tests (318 total).
1.16.0 — chain-v3 real-mode proof: the flagship repair loop is now
proven under the newest chain with a live model, not only in Judge
Mode — `samples/evidence_real_repair_v3.json` (Nemotron 3 Super
120B: FAIL → a rejected diff fed back → PASS, 3 calls, 9,407 tokens
sealed, both records `chain_version: 3`) is pinned by tests and
verified + replayed in the clean-tree rehearsal; a test also pins
the v3 property on the committed record itself (rewriting its
replay command or flipping its dev-key disclosure breaks its
chain). The hosted-demo landing page states the new sealed total.
Cumulative sealed real-mode usage across committed records: 34,023
tokens. 2 new tests (316 total).
1.15.0 — record metadata sealing (chain v3): a record's top-level
fields sat outside the hash chain entirely — the `replay` command
could be replaced with an arbitrary shell command, the
`signing_key_is_default_dev_key` disclosure flipped to hide that
the labelled public dev key signed the record, and `version` /
`created_at` rewritten at will, all verifying INTACT while the
web view printed the rewritten values (9 probes reproduced).
Chain v3 seals those fields into `record_hash`; the repair loop's
one post-seal replay correction re-seals through a dedicated
helper. Legacy v1/v2 records get metadata consistency rules:
exact field set, semver version, timezone-aware ISO timestamp,
dev-key flag equal to the verifying key, bool `chain_version`
rejected, and a replay string matching an emitted command
template in the header's mode with no shell metacharacters in
its labels. 15 new tests (314 total).
1.14.0 — cross-attempt spec binding: a repair report's attempts
were cross-checked on spec name and mode, but their sealed
`spec_hash` values were never compared with each other, so an
attempt sealed under a different spec (same name and mode,
different brief or checks — re-sealed with the public dev key)
could be spliced into a report and still verify INTACT.
Verification now requires every attempt to share one spec hash
(the loop never changes the task), and the web view flags a
divergent report in its SUMMARY MISMATCH banner. 9 new tests
(299 total).
1.13.0 — repair-report narrative consistency: a repair report's
unchained narrative was taken at face value — a PASS report could
claim it stopped at max rounds, a clean report could grow an
invented rejected patch, attempt rounds could be renumbered past
`max_rounds`, and the adapter credited with the repair could be
rewritten, all while `verifygate verify` reported the report
INTACT (8 probes reproduced). Verification now re-derives the
narrative from the sealed attempts: attempt rounds start at 0 and
strictly increase, each attempt's patch linkage names its round
and an applied patch, a preserved rejection occupies a round no
attempt claims, `last_patch_error` equals that rejection's error,
`stopped_reason` is the one the loop's exit conditions produce,
rounds never exceed `max_rounds`, and the credited adapter is the
one the sealed diagnoses and usage calls name. The web view shows
the derived stopped reason and flags narrative disagreement in
its SUMMARY MISMATCH banner. 21 new tests (290 total).
1.12.0 — record consistency: verification now recomputes a record's
internal consistency, not just its chain and shape. A record's
verdict must be the one its checks imply (PASS stamped over a FAIL
check verified INTACT before), each check's verdict must be the
one its exit code / timeout implies, an `llm_usage` entry's stated
total must equal the sum of its own calls (a forged token bill,
re-chained and re-signed, verified before), and field values must
be ones a real run can emit (negative or non-numeric durations,
`exit_code: true`, a non-existent mode, non-hex header hashes, a
non-string diagnosis all verified before). 51 new tests (269 total).
1.11.0 — tamper robustness: three reproduced holes closed. (1) The
header entry was the one entry not hashed in full — verification
recomputed it from a four-field subset, so an extra field injected
into a header verified INTACT on both chain versions; the header is
now hashed over its full content and must carry exactly the emitted
field set, while every untampered committed record still verifies.
(2) A sealed usage entry rewritten as a bare string crashed
`verifygate verify` and the web view with an `AttributeError`
traceback; malformed usage now totals as malformed and the record
fails cleanly. (3) A patch targeting a binary (non-UTF-8) file
raised a raw `UnicodeDecodeError` that escaped the repair loop's
`PatchError` handling and crashed the run; it is now a clean
`PatchError` rejection with the tree unchanged. 12 new tests
(218 total).
1.10.0 — patch-application atomicity: a multi-file diff now applies
all-or-nothing. Every file patch is validated against an in-memory
staged copy first, and the workspace on disk is written only after
the whole diff has applied cleanly in staging — before this, files
were written one by one, so a diff whose second file failed left
the first file already rewritten (a half-applied patch, contradicting
the applier's "never partially forced in" contract for any caller
that did not itself discard the whole workspace). Sequential
patches to the same file still apply in order, against the staged
state; on any rejection the tree is byte-for-byte and
mode-for-mode unchanged. 7 new atomicity tests (206 total).
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
