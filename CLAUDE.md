# Contributor guidance

Shared guidance for Claude Code and GitHub Copilot CLI. If a project has a
higher-precedence Copilot instructions file, update its owned section explicitly;
do not assume `CLAUDE.md` wins or overwrite unrelated instructions.

## Product and development

LabGoblin is a local autonomous research harness, not a general workflow engine.
Python 3.11+, native Windows, prepared WSL2 and local Linux Docker are supported.
Configuration/campaign schemas are version 4; machine ledger and worker protocol
remain version 3. They are separately checked; there is no campaign migration.
`paths.py` resolves only LabGoblin names; there are no old-name aliases or
old-name frozen-helper readers. The same-protocol pre-memo helper manifest stays
valid without replacement, as documented in the runtime contract. Never rewrite
admitted envelopes or receipts.
Do not reintroduce cluster commands, implicit image pulls or environment installs.

```powershell
python -m pip install -e .
python -m pytest tests -q
```

Use the selected environment's executable when `python` is not on PATH.
Runtime dependencies are declared in `setup.py`. The pinned Copilot SDK is core;
`docker-build` is the optional pinned adapter. Do not add tools or modify shared
environments merely to make a test pass. Real provider calls always need explicit
authorization and a persisted invocation allowance outside the test campaign.

## Ownership of state and modules

| Module | Authority |
|---|---|
| `config.py`, `protocol.py` | Strict typed configuration, versioned records, finite/unlimited limits. |
| `initialization.py`, `setup_assistant.py`, `setup_runtime.py` | Reviewed publication, foreground scoped conversation and owned stdio runtime. |
| `db.py`, `state.py` | Campaign transactions, exact IDs, control revisions, generations and accepted research. |
| `scheduler.py` | Shared one-time grants, fairness, native placement and separate consumer receipts. |
| `campaign.py` | Recovery-first progression; no direct unguarded lifecycle writes. |
| `processes.py`, `worker.py`, `payload.py`, `backends.py` | Owned trees, frozen helpers, exact envelopes/receipts and bounded streams. |
| `agent.py`, `agent_policy.py`, `agent_worker.py` | Uniform admitted Claude/Copilot invocations and fixed maintenance. |
| `workspace.py`, `evidence.py` | Bounded source copies, input assurance and immutable captured evidence. |
| `briefing.py`, `journal.py` | Exact bounded packets, one owned delta, protected directives and retained history. |
| `research_memory.py`, `research_tools.py` | Explicit draft publication, immutable revisions and common CLI/SDK boundaries. |
| `research_tasks.py`, `coaching.py` | Flat scope ownership/integration and non-prescriptive pre-stop review. |
| `supervisor_memos.py` | Optional owned human communication, coalesced requests and immutable history; not scientific authority. |
| `results.py`, `reporting.py` | Read-only projections, complete source inventories and immutable reports. |
| `cli.py`, `dashboard*.py`, `static` | Structured local commands and read-only dashboard/observer. |

## Invariants for changes

- Never hold a campaign writer transaction while entering the ledger or probing
  a backend, nor a ledger writer while querying a campaign.
- Grants and armed launch incarnations are one-time. Unknown ownership is not
  death, unused budget, permission to retry, or permission to release.
- Workers consume frozen envelopes/helpers, not current TOML. Guest payloads
  write qualified receipts, never host SQLite.
- Each acquired controller activates one configuration snapshot, after recovery.
  Submission/build preparation uses that snapshot and an ownership/config fence;
  all TOML edits require restart. Do not reset accounting or rewrite queued evidence.
- Terminal init uses the core SDK but no research/observer ledger grant. It has
  no setup usage cap, saved draft, arbitrary command or model-side Apply capability.
  Explicit noninteractive/JSON init never starts the SDK. Keep per-file consent,
  fixed approved probes, exact previews and incomplete-publication guards.
- Windows launches use `processes.background_options()` and assigned Job Objects
  before payload resume. Keep independent breakaway and windowless descendants;
  `DETACHED_PROCESS` is not an equivalent substitute.
- Status/dashboard reads never initialize, migrate, reconcile, acknowledge or
  mutate research. Archival reset requires quiescence and exclusive reader access.
- Only campaign admission time and managed invocations accept `0` as unlimited.
  GPU hours use explicit `"unlimited"`; numeric zero still permits no GPU time.
  Unlimited GPU hours do not bypass device grants or finite job deadlines.
  Task lifetime accepts positive seconds or explicit `"unlimited"` (stored as
  null); finite task deadlines remain pinned in provider/experiment envelopes.
  Unlimited task lifetime never removes operation deadlines or invocation limits.
  Ordinary work cannot consume a finite generation's final-analysis reserve.
- Copilot credits use `"unlimited"` or a >=30 soft cap. Aggregate owned receipt
  `totalNanoAiu`, never premium multipliers. Unknown spend blocks capped inference
  unless an operator explicitly approves an audited estimate. Estimates count
  against the cap but stay separate from reported usage and scientific revisions;
  preserve credit usage across restarts and final-analysis/canary reservations.
  Observer credits are a separate campaign-scoped machine-ledger allowance.
- Copilot research defaults to the owned SDK transport and a 1800-second hard
  deadline. Activity distinguishes pending responses/tools from idle sessions;
  the shutdown grace is inside that deadline. Partial usage never proves final
  spend. Credit reconciliation appends attributed evidence, never rewrites receipts.
- Accepted handoff, exact evidence dispositions, next action and journal entry
  commit together. Human text or unrelated file changes cannot satisfy a turn.
- Root supervisor memos use a separately captured optional sidecar, never change
  handoff digests or reject valid science for a memo error. Report issues visibly.
  Cover only delivered/retrieved requests at the packet cutoff and own-turn
  milestones. Memo publication/requests cannot advance scientific progress,
  renew coaching, schedule paid reporting, approve exceptions or wake research.
- Published research revisions, captured observations and report sources are
  immutable. Revise a hypothesis by advancing its head; attempts keep exact
  pinned revisions. Retiring a record never deletes evidence. General knowledge
  need not be attached to a hypothesis.
- Serialize root decisions and allow one active turn per scope. Worker events
  cannot acknowledge root events; worker summaries cannot overwrite root strategy.
  Native agents remain trusted, not filesystem-isolated by these service checks.
- Coaching is campaign-owned, read-only and non-prescriptive. Intercept a stop
  before sealing; missing/stale/failed review is not acceptance. Bounded pushback
  leads to researcher reflection or an explicit operator decision, not endless debate.
  Only evidence-linked goal satisfaction can be coach-approved. Discretionary
  partial closure requires an operator even when coaching is disabled.
  Reworded strategy alone cannot renew the two-review allowance.
- Every role shares campaign credits, invocation limits and machine resources.
  Use one campaign credit setting and derive call reservations transactionally;
  there are no new per-task/coach/call credit settings. Native provider spawning
  is not an alternative to managed delegation. Recursive spawning is rejected.
- Summaries are derived hints. Never delete retained sources, active operator
  authority or governing rationale during compaction. Archive growth alone does
  not authorize paid compaction; bounded fresh packets already limit context.
- Closure seals its denominator. At most one additional analysis, no automatic
  new experiments/retry cycle, and no equation of process success with research.
  Preflight shares acceptance validation but rolls back all state/artifact writes.
  SDK final analysis may correct structure once in its original session; preserve
  drafts/errors and forbid altered conclusions, allowance renewal or inventory changes.
- Observer sessions are fresh empty-mode SDK sessions, with curated read-only
  tools, separate allowance and owned machine admission. No steering, arbitrary
  commands/files, research-session attachment, or research-state writes.
- Bounded reads/spools and explicit truncation are shared conventions. Stream
  pressure must not disable cancellation or finite deadlines.

See `docs/runtime-protocol.md` for the detailed contract and
`docs/local-research.md` for supported user operations. Preserve attribution and
the original historical reports; do not treat their generators as runtime tools.
