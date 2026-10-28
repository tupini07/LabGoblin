# Contributor guidance

Shared guidance for Claude Code and GitHub Copilot CLI. Update a higher-precedence
project Copilot instruction file explicitly if one exists; preserve unrelated
instructions.

## Product and development

LabGoblin is a small local research harness, not a workflow engine. Python 3.11+,
native Windows/Linux and an installed authenticated Copilot CLI are supported.
The core transport is the pinned Copilot SDK. Do not restore legacy research
schemas, managed Claude/WSL/Docker backends, dashboards or orchestration under a
second mode. Keep original attribution and historical reports unchanged.

```powershell
python -m pip install -e .
python -m pytest tests -q
```

Use the selected environment's executable when `python` is unavailable. Declare
runtime dependencies in `setup.py`; do not install tools or alter shared
environments to make tests pass. Real provider calls require explicit approval
and a persisted allowance outside the evaluated research project.

## Core invariants

- One persistent ordinary researcher owns scientific choices and working files.
  A coach can inspect evidence and ask a concrete stopping question; it cannot
  prescribe a workflow or edit research. Only an explicitly enabled host-owned
  consultation tool may start a read-only adviser, never arbitrary agents.
- Stopping guidance targets the original goal or supported exhaustion of credible
  feasible avenues, not merely a defensible negative report. Keep alternatives
  in ordinary notes; no method quotas or scientific-completion state machine.
- Stopping reviews may continue in the same researcher session. Ten consecutive
  follow-up reviews without evidence-grounded meaningful progress checkpoint by
  default; progress resets only that counter. Clear repetition can checkpoint
  earlier after two no-progress follow-ups. Do not infer progress from file churn.
  Initial acceptance is provisional and supplies one neutral closing challenge;
  never repeat it after a follow-up or reset. `needs_operator` requires a concrete
  permission/input/scope question, not an operational limit or scientific uncertainty.
  `checkpointed` is unfinished work, not scientific exhaustion. Cancellation,
  deadlines, spending and genuine operator dependencies retain precedence.
  No separate research reflection, compaction or final-analysis calls. Explicit
  terminal handoffs are separate read-only inspection calls, never researcher prompts.
  Normal completion is not independent scientific validation.
- One controller freezes configuration, goal, instructions and helpers per
  execution, after recovery. TOML changes require a new execution. Explicit
  resumes preserve all prior spending and retained evidence.
- Optional coach, closure-critic, advice and claim-review model overrides inherit
  the campaign model independently, never the caller's override. Select advisers
  by explicit purpose, not their `consult-N` operational name. Preserve the same
  configured coach model in a fresh adjudicator session, common effort and one pool.
  Fail unavailable or incompatible selections explicitly; retain applied model
  attribution outside bounded events. No researcher rotation or implicit role enablement.
- Resource grants and supervisor claims are one-use. Observe/probe outside ledger
  writer transactions. Unknown ownership is not death, unused budget, permission
  to release, or permission to retry. Never impersonate an observer/build.
- The shared ledger and its exchange envelope stay v3-readable for existing
  installed consumers. Private execution format 4 is verified inside that
  envelope. Configuration/project format 5 has no campaign database or migration.
  Never rewrite old admitted envelopes, receipts or frozen installations.
- Windows launches use `background_options()` and Job Objects assigned before
  payload resume. Preserve independent breakaway and windowless descendants;
  `DETACHED_PROCESS` is not an equivalent substitute.
- Linux process sessions require pidfd-safe shutdown; monitored RSS is not a hard
  kernel memory quota. Native execution is trusted, not a hostile-code sandbox.
- All executions have finite deadlines, with shutdown grace inside them. Bounded
  spools must keep draining and retain explicit truncation/error information.
- Aggregate integer `totalNanoAiu`, not premium multipliers. All roles share one
  cumulative research project pool. Independent inspection has a separately
  authorized cumulative pool; never borrow research allowance for chat.
  Limit updates reassign only observed remaining allowance at idle boundaries
  or inside the exact host-owned consultation callback that blocks its caller;
  they never renew an exhausted session or reset usage.
- Optional consultations have no independent credit quota. Before adviser
  admission, reduce the blocked caller's ceiling to its observed usage plus
  the provider-minimum response headroom, then allocate the remaining campaign
  pool excluding protected review funds. Return known unused funds before
  releasing the tool result. Failed transfers abort rather than resume inference.
  Keep adviser spending, exact caller/session identities and evidence in the
  existing run receipt. Unknown usage blocks inference; no overlapping limits,
  recursive advisers, parallel research branches or second canonical plan.
  Targeted claim reviews reuse this capability and pool, not another runtime role.
  A coach must await an active consultation before deciding. Review arguments,
  not reviewer agreement; preserve scope/coverage and return objections to the
  same researcher. No compulsory review on every stop or proof-certification claim.
  Explicit closure_critic mode is the exception for discretionary final acceptance:
  retain the proposal, independently challenge it, deliver the critique to the
  persistent researcher, then require fresh read-only adjudication. The proposing
  coach never decides its own objection. Required corrections route to research;
  acceptance cannot contain outstanding required corrections. The critic has no veto.
  Failed criticism, response or adjudication never applies preliminary
  approval; closing-phase transfers require settled idle boundaries.
  Each new coach/adjudicator has one prompt. Extra closing responses require an
  opted-in frozen capability and exact proposal/critique/submission/settlement
  evidence; old execution bounds remain. Judge progress once per review, not
  again for criticism or its response. The critic has a two-hour maximum inside
  the campaign deadline, reserving ten minutes each for researcher response and
  adjudication, and no independent credit/read/image-count cap. Research can
  use more available time but must leave the adjudicator's ten-minute reserve.
  Preserve bounded individual evidence payloads and protected caller allowance.
  Return unused critic funding only after settlement. Ordinary advice and claim
  reviews have a 30-minute maximum within the campaign deadline, including
  shutdown; their evidence-count limits remain unchanged. Unlimited campaigns
  impose no hidden adviser credit quota either.
  Keep routine countdowns, balances and effort forecasts out of model-facing
  guidance. The host enforces limits and exposes actionable refusals/checkpoints.
- Partial usage cannot prove final spending. Unknown usage blocks new admission.
  Operator estimates are explicit, receipt-bound, additive records separate from
  reported usage; later final evidence never rewrites historical receipts.
- Keep normal research files editable and captured execution snapshots immutable.
  Stream large artifacts, enforce bounds, and explicitly list excluded sources.
  Do not silently treat truncation, incomplete retention or process success as
  scientific completion.
- Status and machine reads never initialize, reconcile or mutate research.
  Campaign chat uses fresh empty-mode inspector sessions, curated evidence tools
  and genuine `inspection-run` grants. Keep its history/drafts outside research
  and require a separate finite allowance. Its only writing tool prepares drafts;
  an explicit human Publish action commits exact captured ideas. No arbitrary
  editing, execution control, research-session attachment or background inference.
  Citation captures bind terminal receipts; live/truncated coverage stays explicit.
  `run --handoff` explicitly authorizes one independent terminal report using
  the separately approved inspection pool. Pin the exact terminal execution,
  exclude prior chat and mutable workspace access, and provide no idea-draft tool.
  Retain dated immutable reports outside research. Failure never changes the
  original receipt, resumes research or automatically retries inference.
  Original SDK events use bounded segments and a host-built index, not model-
  authored progress summaries. Retain explicit omissions and legacy read support.
  Do not add automatic image pulls, environment installs, remote execution or
  unrestricted nested provider calls to the research loop. Advisers have only
  curated evidence reads and verified public retrieval; adding a custom tool
  must not change the ordinary researcher's coding permissions.
- Operator ideas are advisory immutable file revisions, not scientific records or
  changed authority. Give the same researcher short file pointers, not injected
  documents, at startup/stopping boundaries, including coach-off executions.
  Retain prepared/submitted/settled-reply distinctions. Extra researcher prompts
  require exact owned delivery records and an opted-in frozen prompt capability;
  old execution bounds remain unchanged. Coach and critic see the same captured
  files/replies. Serialize publication and final closure with a separate short
  inbox lock, never the execution-long controller lock. Unseen publications
  invalidate discretionary closure; post-cutoff submissions wait for a later
  authorized execution. Ideas never reset stalled reviews, credits or deadlines.

See `docs/runtime-protocol.md` and `docs/local-research.md` for the exact boundaries.
