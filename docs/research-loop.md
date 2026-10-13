# Reflective research, coaching and flat delegation

The researcher owns scientific choices. LabGoblin owns admission, accounting,
evidence identities and role boundaries. Longer runs, more hypotheses and more
reflection are not success metrics.

This feature requires a **fresh schema-4 campaign**. Do not migrate the stopped
benchmark, rewrite its receipts or replace its compatible editable installation.
The machine ledger and frozen-worker protocol retain version 3.

## Workspace: drafts versus accepted history

Use `research\knowledge`, `research\ideas`, `research\hypotheses` and
`research\reflections` for freely editable drafts. Creating or editing files
does not publish them. General literature and useful facts can remain unlinked.

Publish a small JSON draft:

```json
{
  "id": "orientation-question",
  "request_id": "orientation-question-v1",
  "base": null,
  "kind": "question",
  "title": "A coordinate-frame question",
  "body": "Does the choice of frame change generalization?",
  "activity": "active",
  "links": [],
  "provenance": {"citation": "Researcher's own question"}
}
```

```powershell
labgoblin research publish --file research\ideas\orientation.json --json
labgoblin research search --query frame --json
labgoblin research read --id orientation-question --json
labgoblin research export --id REVISION_ID --output research\exports\revision.json --json
```

The CLI and SDK `research_publish` tool use the same transaction. Each publication
requires a unique request ID and the expected current `base` revision. Identical
replays return the original revision; conflicting content or stale bases fail.
File publications retain the exact draft digest. Exported accepted views are
immutable projections, not a second writable master.

Kinds are `knowledge`, `question`, `hypothesis`, and `reflection`. Promote a
question by publishing a hypothesis revision with the same record ID. Revise,
park, retire or restore by publishing another revision; activity is separate
from `unassessed`, `supported`, `refuted`, `inconclusive`, or `blocked` outcome.
Splits and merges create records linked to exact predecessor revisions using
`derived_from` or `supersedes`. Retired ideas remain in search and history.

Links have `target` (an exact source, observation, attempt, event or task ID) and
`relation`: `context`, `informs`, `supports`, `refutes`, `supersedes`, or
`derived_from`. Context is not evidence of support. Scientific assessments
commit in the accepted researcher's `research_updates` handoff, not from a
partially completed provider's ordinary publication tool. Operator publications
are attributed separately.

An experiment manifest declares `purpose`: `evaluation`, `exploration`, or
`support`, plus exact `research_refs`. Evaluation requires a published hypothesis
revision; support/intake and exploratory questions need no invented hypothesis.
Omitted purpose is labeled support, not silently treated as an evaluation.
Changing a hypothesis does not change already pinned attempts or observations.
Later interpretations do not retroactively become preregistration.

## Continuity between fresh turns

Use the existing handoff's `rationale` for concise direction, why, unresolved
decisions and conditions for reconsidering alternatives; `next_step` supplies
the next useful action. Select exact `research_refs` in priority order. There
is no additional memory document, compulsory prose template, summarizing agent
or periodic compaction call.

Packets preview the accepted rationale, summary and next step. Focus is qualified
against generation and governing authority; a worker receives its own checkpoint,
qualified against its current charter, plus explicitly attributed root background.
Old or stale focus is labelled historical, not adopted as current strategy.
Root background never amends a worker charter. Sealed final analysis uses the
sealed authority and rationale, not a subsequent live head.

`research_workspace.selected` previews exact revisions selected by applicable
accepted focus or the worker charter, including parked/retired and older records.
Revisions published by that handoff's accepted `research_updates` follow its
explicit `research_refs`, so a new assessment accompanies, rather than replaces,
the evaluated revision. Do not forward-reference an unpublished revision in
`research_refs`; the existing acceptance contract is unchanged.
Newer heads do not replace them. Selection means relevant context, not scientific
support or root endorsement. Ordinary publications can survive a rejected turn:
the recent same-scope `records` list is **discovery only**, not accepted strategy.
Worker-local records do not automatically become root focus. Sealed final
analysis does not receive the live recent-record list.

Operator authority, sealed inventory and pending event headers take space first.
Optional working context adds at most 12 KiB within the existing 64-KiB packet
limit, with at most twelve selected revisions and twelve recent headers. UTF-8
preview lengths, truncation, applicability, omitted previews, counts and further
coverage are explicit. Retrieve the exact focus source for omitted selected
references; read records by `source_id`, not by a mutable record name. Original
sources and packet replay remain unchanged. Context projection does not publish
research, reset review limits or schedule another call.

The optional root handoff `goal_progress` carries a compact assessment of the
**original** deliverables: `coverage` and one to eight `items`, each with
`deliverable`, `status` (`unresolved`, `partial`, `supported`), `assessment` and
exact `references`. Partial/supported claims need references; these are
researcher interpretations, not independently certified facts. Keep the entire
assessment within 4096 UTF-8 bytes and retain details in existing research records.
Supply it initially and when assessments change, not every turn. Its source is
an accepted handoff, not another editable document. Later ordinary turns carry
that exact assessment forward; rejected/stale handoffs cannot replace it.
Authority/generation changes label it historical. Final analysis uses the sealed
head. Goal gaps take optional context space before the local next-step preview;
authority and pending evidence still take precedence, with explicit omitted-view
coverage when space is unavailable.

At a branch, refinement batch or milestone, the optional root `commitment`
contains `branch` (a stable name), `boundary` (`branch`, `refinement`, `milestone`),
`goal_connection`, `decision_value` and `revisit_when`, within 2048 UTF-8 bytes.
Explain a decision different outcomes could change or a concrete prerequisite
enabled; no numerical task-cost forecast or invented value-per-credit score is
required. Necessary engineering and negative results count. Routine work within
the same batch needs no repeated commitment, and one step need not finish the
whole goal. Neither field is a new scientific acceptance threshold.

The packet's `work` section samples current-generation attempt/task states from
the database, and `budgets` uses existing campaign accounting, including credits,
GPU hours and resource commitments. These are timestamped facts, not process
liveness or scientific conclusions. Successful execution/collection is separate
from a scope's evidence disposition; pending includes deferred evidence. Worker
completion is separate from root integration. Use fresh paged reads as needed:

```powershell
labgoblin research work --limit 12 --offset 0 --json
```

The same SDK service is `research_read` with `operation="work"`, `offset` and
`limit` (maximum twenty per section). Root sees all current-generation scopes;
workers see only their own work. Attempt and task sections each have their own
counts, `next_offset` and `has_more`; pages are fresh samples, not a frozen
historical view. Retrieve task details and collection events by their exact IDs.
Budgets remain available through `labgoblin budget` / `research_read` status.

Search covers published bodies at a stable cutoff, including retired records,
with explicit pagination. It does not search unpublished files or raw datasets.
Zero matches and a short index do not prove absence. These continuity mechanisms
do not establish a measured improvement in research quality.

## Human-supervisor memos

The root researcher authors short updates during ordinary research, reflection
or final-analysis turns. The harness coalesces accepted assessments, hypothesis
retirements, published strategy/reflection changes, operational blockers and
operator requests. Ordinary notes and journal entries do not each demand a memo.
Retirement does not imply refutation. Workers return findings to the root; they
do not publish campaign-wide supervisor conclusions.

The dashboard **Brief** leads with the latest memo and a separately sampled
controller/accounting panel. Detailed internal rationale remains intact under
expandable working context. **History > Supervisor memos** retains all versions,
timestamps, generations and evidence/provenance links. Missing, invalid and stale
updates are explicit; old-generation memos never become new plans.

The packet supplies `supervisor_memo.path`, a prior memo pointer, bounded pending
requests and an exact format. Write the optional JSON sidecar there, not inside
the scientific handoff. Fields are `turn_id`, `packet_id`, `bottom_line`,
`changes`, `uncertainty`, `next_step`, `decision_needed` and `references`.
Use plain language, roughly 150-250 words (maximum 300 across narrative fields,
8192 UTF-8 bytes total), and up to twelve exact underlying evidence IDs. Say
explicitly when no operator decision is needed. Do not replace evidence with
another memo or claim a snapshot describes live process health or spending.

The provider captures the sidecar independently into its owned receipt.
Publication requires an accepted root handoff, unchanged governing authority
and delivered/retrieved references. Invalid or absent memos leave valid science
accepted and reporting pending; no new provider call, automatic report job,
budget or approval gate is created. A successful memo covers only delivered or
retrieved requests within its packet cutoff, plus milestones from its own turn.
Later requests and unread pages remain pending. Retrieve further pages through
`research_read` operation `memos`, or the corresponding CLI:

```powershell
labgoblin research memos --json
labgoblin research memos --offset 8 --limit 8 --json
labgoblin research memos --id MEMO_SOURCE_ID --json
labgoblin research request-memo --id supervisor-update-1 --reason "Explain progress and remaining uncertainty" --json
```

The last command is operator-only and idempotent by ID and generation. It queues
communication for the next eligible ordinary root turn; it does not wake a
blocked/closed campaign or launch a reporting turn. Replies, scope changes and
exceptions use existing explicit operator directives/controls. Silence is not
approval, and dashboard observer chat remains read-only.

Memo sources and requests are derived communication, not scientific progress,
new evidence or a reason to reset coaching/compaction guards. Existing scientific
handoff bytes/digests, schema-4 campaign state and previously frozen work remain
valid. No live campaign is upgraded simply by installing a newer development tree.

## Reflection and coaching

Root handoffs can request `"reflection": true` with a continuation decision or
`"coaching": true` when enabled. Reflection uses a fresh owned researcher turn
with the same evidence access, not a separate domain expert. It cannot submit
heavy jobs or commission workers during that reflection turn. It may continue,
pivot, revisit an old idea or substantiate a limitation without fabricating an
experiment. It is distinct from archive compaction.

Root `blocked`/`finalize` proposals name `stop_basis`: `goal_satisfied`,
`operator_needed`, or `discretionary`. A missing basis in a legacy result is
treated as discretionary, without rewriting historical receipts. Retiring a
branch is an ordinary continuation/reflection decision, not campaign closure.
Goal satisfaction requires the original criterion and exact delivered evidence
or published research revision references; a classifier is not proof of success.

With coaching enabled, a discretionary stop or genuinely empty `wait` is a
proposal, not immediate approval. Live owned experiments/tasks settle first.
Completion evidence arriving after the packet was prepared wakes normal
research instead of invoking a coach; repeating an empty wait without new
evidence still enters review. Explicit access/authority dependencies go to the
operator with the specific missing permission/input, not a speculative workaround.
A fresh empty-mode coach can only retrieve retained evidence and return one of:

| Decision | Meaning |
|---|---|
| `request_reflection` | Ask at most four process questions; the researcher chooses the next scientific action. |
| `accept_stop` | Permit an evidence-linked `goal_satisfied` proposal to enter finalization, not certify scientific success. |
| `needs_operator` | Preserve an unresolved decision explicitly. |

The coach distinguishes constraints from assumptions and challenges
generalization from one failed branch. It asks what an incremental step could
teach, not whether one experiment solves every remaining deficiency. It must not choose experiments, models,
hypotheses or new scope. Prompt constraints do not prove perfect compliance:
behavioral evaluation must check actual output.

`coach_decide` runs shared structure, episode and reference validation before
recording its verdict. Missing references name the exact IDs and supported
`research_read` actions, so the coach can correct the error within the same
owned session and existing allowance/deadline. Preflight is read-only: it does
not accept a verdict, seal research, consume another review round or register
evidence as retrieved. The controller repeats validation against the completed
owned result receipt; preflight does not guarantee later acceptance.

The packet distinguishes delivered evidence (`packet.references`, plus the
coach's registered retrievals) from citations merely mentioned inside the
researcher's proposal or another source. Those nested IDs are not automatically
authorized references. The coach can retrieve underlying evidence before direct
citation, or cite a delivered handoff as a reported claim with explicit coverage
limitations. It need not duplicate the final scientific assessment. Changed
ownership, authority or review snapshot is not an in-session citation repair.

A stopping episode allows at most two coach calls. Repeated unchanged-evidence
disagreement requires an operator decision. Malformed, failed, stale or missing
review is never acceptance. Exact generation, authority, configuration, evidence
and result identities are retained. Operator stop, unknown accounting and safety
limits take precedence. Successful closure still needs the sealed inventory's
ordinary final assessment; blocked is not success.
Only changed authority/configuration, attempts, captured observations or retained
journal imports establish a new review basis. Rewording hypotheses, reflection,
strategy or summaries cannot reset the two-review bound. The full review snapshot
also fences published research revisions while a coach is running.

The service does not let a coach approve discretionary partial closure, uncertain
marginal value or "no single step completes the goal." Such acceptance becomes
`needs_operator`; disabling coaching does not bypass this stopping authority.
Hard limits and operator stops remain independent. No minimum experiment/pivot
count, compulsory delegation or requirement to spend the full budget is imposed.

```powershell
labgoblin research status --json
labgoblin research coach --id EPISODE --decision continue --reason "Operator decision" --json
```

Operator resolution only applies to an exact unresolved, settled episode.
`accept_stop` rejects a changed evidence/authority snapshot. A researcher cannot
authorize its own operator resolution.

### Nonbinding strategy advice

When `research.coaching` is enabled, new root commitments can also request
automatic **advisory** review during continued research. This uses the existing
owned, read-only coach, but a separate purpose and verdict: only `advise`, a
short reason, zero to two questions, references and limitations. It cannot
approve a stop, veto work, require reflection, select a method or change scope.
The researcher considers advice in its next ordinary turn; agreement and
continued prerequisite work are valid outcomes.

The selector considers an accepted branch change, a milestone, or three
successive refinement commitments on the same branch with distinct assessed
evidence boundaries. Initial planning alone cannot trigger a review. At least
one assessed experiment/task-result event is needed initially, and two newly
assessed result events are needed between later reviews. This is a bounded
reconsideration heuristic, not a claim that stagnation was detected, a fixed
turn/time cadence or a requirement to run that many experiments. Rewording,
archive growth, satisfied waits and advice alone cannot trigger another review.
The coach receives the original goal, goal-gap assessment and bounded previews
of up to four accepted commitments, with exact retrieval pointers.

Reviews coalesce in the existing episode mechanism; a stopping proposal takes
precedence. Strategy episodes do not consume or renew stopping-review rounds.
Stale advice is retained as superseded, without applying its questions. A failed
preparation, failed call or unaffordable review is explicitly unavailable and does not block normal
research; unknown usage and hard limits still apply. Advice/status records do
not count as scientific progress or independently wake inference. `/tasks`
distinguishes advisory reviews from stopping decisions.

### Preflight, final correction and memory overhead

Prefer SDK `research_complete`, or `labgoblin research complete --file draft.json`,
to validate and write the owned result in one action. The shared typed schema is
available from `labgoblin schema handoff --json` without state or inference.
Completion fills omitted turn/packet IDs from the owned invocation, rejects
conflicting supplied IDs, performs the same rolled-back acceptance preflight,
then publishes only that invocation's result file. It never acknowledges
evidence or accepts science itself. Invalid calls cannot replace a previously
recorded result; equivalent calls are idempotent, including a valid manually
written handoff with different formatting. Its existing bytes are preserved.
After success, end the turn.
Optional supervisor memos remain separate and cannot reject valid science.

SDK `research_preflight` and `labgoblin research preflight --file HANDOFF.json`
validate the calling turn's complete proposed handoff through the acceptance
path in a rolled-back transaction. They do not publish records, acknowledge
events, capture worker artifacts, accept a result or seal admission. Actual
acceptance still requires the exact owned completion receipt. Assessed
`research_updates` require their own `links`, while `research_refs` names only
exact published research revisions.
Each update also needs `id`, a stable unique `request_id`, `title` and `body`.
Creating a record requires `kind`; revising one requires `base` naming its exact
current revision. `labgoblin schema research-update --json` exposes the shared
publication fields and an assessment example without state access or inference.

For SDK final analysis, a parseable invalid final draft gets at most one
automatic structure/reference correction in the **same session, allowance and
deadline**, before result capture. Original drafts and validation errors are
retained. The correction cannot change scientific text, outcomes, dispositions,
limitations or remove assessments. Evidence must already be retained; the sealed
inventory cannot change. Invalid JSON, stale ownership, exhausted allowance,
cancellation or another invalid result ends honestly unassessed. CLI transports
use explicit preflight; they do not gain an extra provider invocation.

Archive size no longer triggers automatic paid compaction: fresh packets already
bound focus/summary previews and offer exact retrieval. Operator or researcher
requests remain available, deduplicated by source revision. New compaction
packets omit the redundant full current handoff and cap the derived summary at
4096 UTF-8 bytes, retaining originals and retrieval pointers. Previously admitted
packets retain their frozen limits; unstarted automatic requests are cancelled.

The iterative-loop wording takes inspiration from
[karpathy/autoresearch's program](https://github.com/karpathy/autoresearch/blob/master/program.md):
compare, experiment, inspect actual results, retain or discard an idea, and
choose the next question; revisit near misses and prefer simplicity when
supported. LabGoblin does **not** adopt an unconditional "never stop" rule,
fixed five-minute jobs, a single metric/file, destructive Git resets or relaxed
permissions. Long managed jobs, scientific uncertainty and operator limits
remain first-class.

## Adaptive direct work or flat workers

Direct research remains appropriate for tightly coupled work. Delegate separable
objectives rather than forcing one agent per hypothesis. Use SDK `research_task`
or a JSON operation with `labgoblin research task --file operation.json`.

```json
{
  "operation": "create",
  "charter": {
    "request_id": "independent-diagnostic-v1",
    "objective": "Investigate the specified diagnostic independently",
    "deliverables": "Return exact evidence, limitations and any tested code changes",
    "source_files": ["diagnostic.py"],
    "research_refs": [],
    "dependencies": [],
    "max_invocations": 3,
    "max_seconds": 3600
  }
}
```

The charter pins source bytes in `research\tasks\TASK\work`. The worker owns that
workspace, local records and queued experiments, not shared project files or
global authority. It submits experiments with campaign-relative `cwd` and
`source_files` inside its task workspace. Each worker yields its provider grant
after submission and resumes on scoped result events. The root does not poll
workers inside a live provider session. Native provider task/fleet tools are
disabled in the SDK path; workers cannot create descendants.
Preparation failures retain a marked, unpublished directory rather than
pretending a task was admitted. A replay uses the original database task;
interrupted or competing preparation directories are never scheduled implicitly.

Tasks use the campaign's runner/input/resource settings. Their elapsed allowance
defaults to `research.task_max_seconds` (7200 seconds in starter settings).
Set it to `"unlimited"` to remove the aggregate lifetime ceiling, including
dependency/capacity waits and time spent in long compute jobs. A charter can omit
`max_seconds` to inherit that setting, or request positive seconds or `"unlimited"`
within the configured ceiling. Zero and explicit null requests are invalid;
persisted unlimited charters/configuration snapshots use JSON null.

A finite task allowance starts at creation, including waits. Submissions reject
jobs exceeding the remaining allowance. Provider and experiment envelopes retain
that absolute deadline, enforced by the payload owner even without a running
controller. Unlimited tasks have no aggregate deadline, but every job and reasoning
turn still needs a positive finite deadline. Long jobs (for example, a six-hour
compile) run under their own deadline while the worker yields and resumes on the
completion event; waiting requires no live LLM call or polling invocations.
Existing charters and frozen launches keep their original limits across restarts.
Dependencies
must already exist in the generation, making cycles impossible. Failed/cancelled
dependencies converge to an explicit cancelled task. Task cancellation requests
owned launch retirement but never releases uncertain grants.

Operations also include `message` (bounded root/worker clarification), `amend`
(root-only objective/deliverable amendment with a charter fingerprint `base`,
after owned work settles), `cancel`, and `integrate`. Amendments do not enlarge
task allowances or overwrite the original charter's retained source manifest.
Root integration records adoption, rejection or uncertainty with original
evidence; it does not promote worker summaries into independent observations.

Workers return `task_artifacts` with workspace-relative `path` and SHA-256
`digest` in the owned handoff. Captures are immutable. Integration optionally
selects those paths and a successful owned `validation_attempt` whose source
snapshot contains exactly those returned bytes. The researcher remains
responsible for choosing relevant tests. Changed root preimages cause explicit
conflicts rather than overwrite. Protected inputs, authority, configuration and
other task areas cannot be integration targets.

Multi-file integration retains an `applying` intent before writing. It is not
a cross-filesystem atomic transaction: interruption can leave a partial set,
which remains visibly incomplete. Replaying the same decision resumes from
matching captured bytes; competing edits cause a conflict. No blind rollback
erases user changes.
Concurrent API integrations serialize preimage checks with each file write;
unmanaged native editors are not covered by a filesystem compare-and-swap.

Service checks are **application-level ownership, not a native filesystem
sandbox**. A trusted full-mode agent with shell access can perform unsupported
actions outside the API. Input isolation depends on the selected runner.

## Configuration and accounting

Starter settings keep coaching/delegation disabled. Enabling them requires
Copilot SDK transport; ordinary Claude/CLI research retains the shared CLI
publication interface. Set the total research allowance and concurrency/time limits deliberately:

```toml
[campaign]
max_ai_credits = 20000

[research]
coaching = true
max_parallel_agents = 2
max_tasks = 4
task_max_invocations = 4
task_max_seconds = 7200
```

These are example ceilings, not authorization to spend on an existing campaign.
`max_tasks = 0` disables delegation; `max_parallel_agents` includes root/coach
and workers. CPU/RAM and the shared ledger can serialize work even with a higher
agent limit. `campaign.max_ai_credits` is the only research credit setting.
Tasks have no separate credit ceiling; their usage is still reported individually.
Every canary, worker, reflection, coach, report and compaction draws from that pool.

The controller automatically reserves provider-call allowances transactionally.
After protecting final analysis and remaining stopping-coach/reflection steps,
it divides available credits among actually prepared or ready owned scopes,
bounded by available reasoning slots. Hypothetical idle slots do not divide a
lone researcher's allowance. Already reserved calls are not allocated twice.
A canary gets only the provider's 30-credit minimum. Reservations
are temporary commitments, not spending or independent per-task budgets; returned
unused allowance can fund later turns of any task. Unlimited campaign credits
do not introduce a hidden per-task or ordinary-call cap.
Unused proven reservations return; missing usage stays
unknown and cannot be automatically estimated. Credit caps remain post-response
soft caps, not guaranteed billing caps.

The final-analysis planning reserve is at least 30 credits. With at least three
comparable accepted root completions it uses the median of up to seven recent
reported costs under the loaded configuration, rounded up to nano-credit precision.
`budget.ai_credits.finishing`
exposes the sample count, excluded unmetered history, reserve and uncertainty.
This is neither a promise that final analysis will fit nor spending or an
operator-approved accounting estimate. Failed calls, incomplete usage and
operator estimates cannot substitute for completed-cost samples. With little
history, the provider minimum is explicitly an uncertain fallback. Tight
admission can move to final assessment; it cannot lower scientific standards.
Continuous execution also reaches the reserved assessment when ordinary
admission seals the generation at the end of a cycle; an idle research queue
does not mean a pending final analysis is finished.

The SDK retains explicit `session_limits_exhausted` events and the qualified
`session.info` limit-reached notice independently of
whether final spend exceeded a soft allowance. A valid owned handoff remains
eligible for acceptance even after a soft overshoot. A quota-only session error
can finish successfully only after the existing handoff passes preflight;
final usage settlement, shutdown and controller receipt checks still apply.
Usage updates and final-response/tool-disable warnings alone are not exhaustion.
This does not excuse another provider error (including one preceding the quota
event) or start a correction request.
If an explicitly limited
root session has no acceptable completion, it is **not retried with a smaller
allowance**: a `provider_limit` blocker and model-free operational checkpoint
preserve the actual failure, result-capture status and retained-work counts.
Inspect the original evidence/accounting before an explicit operator `resume`.
A controller restart or a later experiment result does not clear that blocker;
resume cannot bypass another recovery or unknown-accounting blocker. No
scientific handoff, final usage or successful closure is invented.

Remove the former `research.coach_credits`, `task_max_credits` and
`invocation_credits` fields from TOML, and `max_credits` from new task requests.
They produce explicit configuration/request errors instead of silently ignored
limits. Historical snapshots remain readable without rewriting them; already
reserved invocations and positive ceilings in old task charters remain binding.
New tasks are marked as using the campaign pool.

All TOML changes require controller restart. Existing armed envelopes remain
unchanged. Dashboard observer/setup usage remains separate from research roles.
The read-only dashboard shows published records/revisions at `/research` and
tasks, integration, advisory/stopping reviews and role costs at `/tasks`. Drafts are
explicitly not published state. Neither route starts inference.

## Evidence and evaluation

Human coaching/debrief meta-analyses motivate hypotheses about this design; they
do not establish an AI effect size or prove that encouragement improves research:
[workplace coaching](https://doi.org/10.3389/fpsyg.2023.1204166),
[executive-coaching RCTs](https://doi.org/10.3389/fpsyg.2023.1089797), and
[debriefs](https://doi.org/10.1177/0018720812448394).
The debrief reference was verified through its abstract/metadata, not a full-text
methodological review.

[Controlled agent-topology research](https://arxiv.org/html/2512.08296v3) and
[Anthropic's engineering account](https://www.anthropic.com/engineering/multi-agent-research-system)
show reasons to measure coordination overhead, not assume more agents help.
Recursive managers remain an evaluation decision, not a production capability.
Consider a bounded prototype only if disjoint multi-workstream tasks benefit
at equal total credits without unacceptable drift, duplication or integration
cost; include tightly coupled controls and repeated runs.

Model-free ownership/provenance tests are not evidence of improved research.
Live ablations require separate explicit spending authorization and should
compare direct research, workspace/reflection, fixed process reflection, coaching
and adaptive flat workers at equal campaign budgets and model settings.
Score output/evidence correctness, appropriate continuation and stopping,
violations, hypothesis reuse, retrieval, duplicate work, rework and total cost.
Include negative findings and uncertainty; do not use spending the cap as success.

### Reproducible ablation preparation

`tests\research_loop_acceptance.py` prepares synthetic cases without inference
by default. Supply an **existing isolated ledger**; the helper never creates or
expands machine capacity. For example, from the source checkout:

```powershell
python -m tests.research_loop_acceptance --output D:\scratch\loop-evaluation `
  --ledger D:\scratch\evaluation-machine.db --variant workspace --variant fixed `
  --variant coach --variant flat --scenario optional-branch --scenario disjoint `
  --seed 1 --seed 2 --seed 3 --credits 1000 --model YOUR_RESEARCH_MODEL
```

Select any of the ten scenarios explicitly, including `exploration`, `knowledge`,
`draft`, `revisit`, `blocked`, `completion`, `worker-failure` and `coupled`.
The optional-branch case is a synthetic structural regression for the premature
stopping concern; it does not assert that the stopped placement run could
necessarily have succeeded.

The `direct` ablation additionally requires `--baseline-source` pointing to a
preserved pre-workspace runtime. All variants use equal total campaign credit
ceilings and declared model/effort/resource settings. Runtime and configuration
hashes distinguish the older baseline from the implementation. Credit ceilings
remain soft limits; actual spend may differ or overshoot.

Actual inference additionally requires `--live --approve-total-credits N`, with
N exactly matching all selected campaigns' combined ceilings. No live evaluation
is part of this implementation. Preparation failures are retained per case and
do not skip the remaining cases. Output includes preregistration, exact answer
scores, raw usage/cost records, role counts, packet bytes, retained campaign
paths, an HTML comparison and an unfilled human-grading template.

Grade evidence correctness and behavior independently of the exact numerical
answer. Report paired results across seeds, uncertainty and all overhead; missing
grades remain unassessed. Recursive managers are **not justified for rollout
without those results**. Consider a separately authorized bounded prototype only
if repeated disjoint-workstream benefits survive equal-budget controls, no
material coupled-task regression appears, and evidence review identifies a
remaining coordination need rather than mere pressure to use more agents.
