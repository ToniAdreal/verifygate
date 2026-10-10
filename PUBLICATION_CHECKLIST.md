# Publication checklist — VerifyGate public repository

Status: **READY-TO-PUBLISH** (marked 2026-10-10; deadline for publication
is Oct 27, 2026). Repository creation and push are performed by the main
agent, not by the build worker.

## Repository face

- [x] English README with one-command Judge Mode quickstart (zero keys,
      zero network), real-mode instructions, evidence-record format,
      sandbox-hardening notes, hosted-demo and rehearsal sections.
- [x] MIT license — `LICENSE` file at repository root; `pyproject.toml`
      declares MIT.
- [x] Run instructions verified from a clean tree: `scripts/rehearse.sh`
      exports `git archive HEAD` and runs tests + PASS/FAIL runs + repair
      loop + web view + chain checks, all green.
- [x] Sample evidence committed: passing/failing Judge Mode records and
      the real-mode Nemotron repair report
      (`samples/evidence_real_repair.json`, 5,367 sealed tokens), plus its
      rendered page (`samples/evidence_view.html`, mirrored in `demo/`).
- [x] No secrets in the tree: real mode authenticates via the stored
      Token Factory connector credential; no API key is read from the
      environment, hard-coded, or committed. `.gitignore` covers `.env`,
      `__pycache__`, and stray `evidence-*.json` outputs.
- [x] Demo data is fictional (sample `stringtools` task); no real user
      data, no third-party code.

## Suggested repository settings (for the main agent at creation time)

- **Description:** "Pre-delivery verification harness for coding agents —
  AI proposes, deterministic checks dispose, the hash-chained evidence
  record proves it. NVIDIA Nemotron via Nebius Token Factory."
- **Topics:** `coding-agents`, `verification`, `ai-agents`, `nemotron`,
  `nebius`, `token-factory`, `evidence`, `ci`, `python`, `hackathon`
- **Homepage:** the hosted static demo (`demo/` directory) once deployed.
- **License:** MIT (auto-detected from `LICENSE`).
- **Default branch:** `main` (matches the local build repo).

## After publication (main agent)

- [ ] Create the GitHub repository and push `main` (no history rewrite;
      the build history is part of the originality record).
- [ ] Enable the repository homepage/topics above.
- [ ] Deploy `demo/` to the chosen static host and link it from the
      README's hosted-demo section.
- [ ] Record the public URL in `BUILD_LOG.md` and the Devpost draft
      (submission itself happens Oct 29 by the main agent).
