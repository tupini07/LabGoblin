# Contributor guidance

Shared guidance for Claude Code and GitHub Copilot CLI. If a project has a
higher-precedence Copilot instructions file, update its owned section explicitly;
do not assume `CLAUDE.md` wins or overwrite unrelated instructions.

## Product and development

LabGoblin is a local autonomous research harness, not a general workflow engine.
Python 3.11+, native Windows, prepared WSL2 and local Linux Docker are supported.
The schema-3 configuration, campaign DB, machine ledger and worker protocol are
separately versioned. There is no old-schema reader or campaign migration.
`paths.py` resolves only LabGoblin names; there are no old-name aliases or frozen
helper compatibility readers. Never rewrite admitted envelopes or receipts.
Do not reintroduce cluster commands, implicit image pulls or environment installs.

```powershell
python -m pip install -e .
python -m pytest tests -q
```

Use the selected environment's executable when `python` is not on PATH.
Runtime dependencies are declared in `setup.py`. Optional `dashboard-chat` and
`docker-build` extras are pinned adapters. Do not add tools or modify shared
environments merely to make a test pass. Real provider calls always need explicit
authorization and a persisted invocation allowance outside the test campaign.

## Ownership of state and modules

| Module | Authority |
|---|---|
| `config.py`, `protocol.py` | Strict typed configuration, versioned records, finite/unlimited limits. |
| `db.py`, `state.py` | Campaign transactions, exact IDs, control revisions, generations and accepted research. |
| `scheduler.py` | Shared one-time grants, fairness, native placement and separate consumer receipts. |
| `campaign.py` | Recovery-first progression; no direct unguarded lifecycle writes. |
| `processes.py`, `worker.py`, `payload.py`, `backends.py` | Owned trees, frozen helpers, exact envelopes/receipts and bounded streams. |
| `agent.py`, `agent_policy.py`, `agent_worker.py` | Uniform admitted Claude/Copilot invocations and fixed maintenance. |
| `workspace.py`, `evidence.py` | Bounded source copies, input assurance and immutable captured evidence. |
| `briefing.py`, `journal.py` | Exact bounded packets, one owned delta, protected directives and retained history. |
| `results.py`, `reporting.py` | Read-only projections, complete source inventories and immutable reports. |
| `cli.py`, `dashboard*.py`, `static` | Structured local commands and read-only dashboard/observer. |

## Invariants for changes

- Never hold a campaign writer transaction while entering the ledger or probing
  a backend, nor a ledger writer while querying a campaign.
- Grants and armed launch incarnations are one-time. Unknown ownership is not
  death, unused budget, permission to retry, or permission to release.
- Workers consume frozen envelopes/helpers, not current TOML. Guest payloads
  write qualified receipts, never host SQLite.
- Windows launches use `processes.background_options()` and assigned Job Objects
  before payload resume. Keep independent breakaway and windowless descendants;
  `DETACHED_PROCESS` is not an equivalent substitute.
- Status/dashboard reads never initialize, migrate, reconcile, acknowledge or
  mutate research. Archival reset requires quiescence and exclusive reader access.
- Only campaign admission time and managed invocations accept `0` as unlimited.
  Ordinary work cannot consume a finite generation's final-analysis reserve.
- Accepted handoff, exact evidence dispositions, next action and journal entry
  commit together. Human text or unrelated file changes cannot satisfy a turn.
- Evaluated hypothesis statements, captured observations and report sources are
  immutable. A new statement/recollection gets a new identity.
- Summaries are derived hints. Never delete retained sources, active operator
  authority or governing rationale during compaction.
- Closure seals its denominator. At most one additional analysis, no automatic
  new experiments/retry cycle, and no equation of process success with research.
- Observer sessions are fresh empty-mode SDK sessions, with curated read-only
  tools, separate allowance and owned machine admission. No steering, arbitrary
  commands/files, research-session attachment, or research-state writes.
- Bounded reads/spools and explicit truncation are shared conventions. Stream
  pressure must not disable cancellation or finite deadlines.

See `docs/runtime-protocol.md` for the detailed contract and
`docs/local-research.md` for supported user operations. Preserve attribution and
the original historical reports; do not treat their generators as runtime tools.
