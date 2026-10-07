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

Automatic packets include complete operator authority, bounded event headers,
revisable current-focus previews and at most twelve recently changed active
records. Search covers published bodies at a stable cutoff, including retired
records, with explicit pagination. It does not search unpublished files or raw
datasets. Zero matches and a short index do not prove absence.

## Reflection and coaching

Root handoffs can request `"reflection": true` with a continuation decision or
`"coaching": true` when enabled. Reflection uses a fresh owned researcher turn
with the same evidence access, not a separate domain expert. It cannot submit
heavy jobs or commission workers during that reflection turn. It may continue,
pivot, revisit an old idea or substantiate a limitation without fabricating an
experiment. It is distinct from archive compaction.

With coaching enabled, `blocked`, `finalize`, or an empty `wait` is a stopping
proposal, not immediate approval. Live owned experiments/tasks settle first.
A fresh empty-mode coach can only retrieve retained evidence and return one of:

| Decision | Meaning |
|---|---|
| `request_reflection` | Ask at most four process questions; the researcher chooses the next scientific action. |
| `accept_stop` | Accept the process justification, not certify scientific success. |
| `needs_operator` | Preserve an unresolved decision explicitly. |

The coach distinguishes constraints from assumptions and challenges
generalization from one failed branch. It must not choose experiments, models,
hypotheses or new scope. Prompt constraints do not prove perfect compliance:
behavioral evaluation must check actual output.

An episode allows at most two coach calls. Repeated unchanged-evidence
disagreement requires an operator decision. Malformed, failed, stale or missing
review is never acceptance. Exact generation, authority, configuration, evidence
and result identities are retained. Operator stop, unknown accounting and safety
limits take precedence. Successful closure still needs the sealed inventory's
ordinary final assessment; blocked is not success.
New attempts, evidence, active claims or explicitly published reflection/strategy
bodies can establish a new episode. Knowledge-note, journal and summary churn
alone cannot reset the two-review bound.

```powershell
labgoblin research status --json
labgoblin research coach --id EPISODE --decision continue --reason "Operator decision" --json
```

Operator resolution only applies to an exact unresolved, settled episode.
`accept_stop` rejects a changed evidence/authority snapshot. A researcher cannot
authorize its own operator resolution.

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
    "max_credits": 200,
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

Tasks use the campaign's runner/input/resource settings; their finite elapsed
allowance starts at creation, including dependency/capacity wait. Provider and
experiment envelopes retain that absolute deadline, enforced by
the payload owner even without a running controller. The per-operation deadline
may end work sooner. Dependencies
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
publication interface. Set finite allowances deliberately:

```toml
[research]
coaching = true
coach_credits = 100
max_parallel_agents = 2
max_tasks = 4
task_max_invocations = 4
task_max_credits = 300
task_max_seconds = 7200
invocation_credits = 300
```

These are example ceilings, not authorization to spend on an existing campaign.
`max_tasks = 0` disables delegation; `max_parallel_agents` includes root/coach
and workers. CPU/RAM and the shared ledger can serialize work even with a higher
agent limit. Per-task/per-call budgets partition the campaign allowance; they
are not additional money. Every canary, worker, reflection, coach, report and
compaction counts. Ordinary admission retains final-analysis and configured
coaching reserves. Unused proven reservations return; missing usage stays
unknown and cannot be automatically estimated. Credit caps remain post-response
soft caps, not guaranteed billing caps.

All TOML changes require controller restart. Existing armed envelopes remain
unchanged. Dashboard observer/setup usage remains separate from research roles.
The read-only dashboard shows published records/revisions at `/research` and
tasks, integration, stopping reviews and role costs at `/tasks`. Drafts are
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
