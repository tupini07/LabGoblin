# LabGoblin

**An ordinary Copilot researcher, with a small execution harness and opt-out
coaching and scientific review.**

LabGoblin gives one persistent Copilot SDK session a research question, short
instructions and a normal workspace. The researcher chooses a useful investigation,
carries it out, examines the evidence and uses it to choose the next investigation.
Notes and reports document meaningful checkpoints, not automatic endpoints.
There are no hypothesis-registration forms, research handoffs, publication
transactions, separate reflection agents or
mandatory final-analysis calls.

Give it the high-level goal, relevant context and actual constraints; let it choose
the scientific formulation, methods and sequence of work. Do not turn a broad
research question into an imposed prototype or pilot deliverable. Intermediate
tools and scoped findings support the investigation, not a smaller finish line.
The default is to keep working while consequential feasible questions remain,
without asking whether to continue or ending with a future-work list. This is
continuation within the authorized execution, not indefinite spending or an
automatic restart.

A read-only coach, enabled by default, reviews the proposed stopping point. Useful feedback
returns to the **same researcher conversation**. By default, ten consecutive
follow-up reviews without meaningful progress end in an unfinished checkpoint;
evidence-grounded progress resets that counter, not time or spending limits.
Initial approval is provisional: the first follow-up supplies a neutral
evidence-grounded challenge when the first coach would accept stopping.
The challenge is never repeated within an execution. Later reviews can accept
or request useful further work; coach-off runs remain one ordinary conversation.
`needs_operator` requires a concrete missing permission, input or scope decision.
Approaching the deadline or repeating an unproductive stopping discussion instead
produces `checkpointed`, not scientific completion or a new human research question.
The stopping standard is the original goal's evidence requirements or supported
exhaustion of credible feasible avenues, not merely a working prototype or an
honest partial report. Unresolved controls and promising untried approaches deserve scrutiny;
exhausted ideas are not proof of impossibility.
The harness owns resources, cumulative spending, cancellation and retained
execution evidence; it does not decide what constitutes scientific truth.

On longer coached runs, a fresh coach also checks goal alignment about every
three hours (`goal_review_interval_seconds = 10800`; `0` disables this).
It reads actual researcher activity and evidence, then returns one or two
nonbinding observations to the same conversation. Staying the course is valid.
Reviews wait for a tool boundary, share the research credit pool and do not
interrupt experiments or count as stopping reviews. Long tools or reasoning can
delay delivery past the deadline. A separate once-only advance reminder encourages
saving partial work and resume pointers; neither it nor ordinary checkpoint
guidance guarantees a saved checkpoint.

Opt-in [scheduled supervisor meetings](docs/local-research.md#scheduled-supervisor-meetings)
request a cooperative checkpoint at a local calendar time, then leave research
stopped for human review. They wait for owned experiments and background follow-up
turns rather than killing work to meet the appointment. The existing read-only
inspector supports the discussion; scheduling itself buys no review call.
Continuation is explicit, starts a fresh conversation from retained files, keeps
cumulative spending and uses the full configured execution timeout.

The default-enabled `labgoblin_consult` tool lets the researcher or coach ask a fresh,
read-only scientific adviser about related work, assumptions or alternative
approaches. It returns a cited brief, not a new plan or experiment. The researcher
still owns the notes and scientific choices; there is no continuous sidecar.
With consultation enabled, the coach requests a targeted **claim review** when
stopping depends on a consequential, insufficiently checked scientific claim.
The adviser challenges assumptions and looks for counterexamples; objections
return to the same researcher. This is not a compulsory reviewer at every stop,
an independent experimental reproduction, or scientific certification.

Independent criticism of **ending the investigation** is also enabled by default
(`closure_critic = true`), alongside coaching and consultation. Before final acceptance, a fresh
adversarial critic searches for the strongest evidence-backed reason to continue,
without being told the coach approved stopping. Its critique goes directly to
the persistent researcher for one response/investigation, then a **fresh read-only
adjudicator** using `coach_model` decides whether to continue or accept.
Required report corrections must be applied before acceptance, not deferred as
conditions on exit. Withholding product release does not by itself justify ending
scientific investigation. The critic
can inspect bounded PNG/JPEG images but cannot run experiments or veto stopping.
Failed criticism is not acceptance; hard limits and operator stops still prevail.

An optional **paired evaluation kit** supplies executable checks outside the
researcher's own experiment code. LabGoblin captures the operator-reviewed
evaluator, baseline and cases, compares exact candidate bytes, spends confirmation
cases once, and replays earlier failures as explicitly non-fresh regressions.
Only the researcher can run checks; reviewers and the inspector read retained
results. It adds no agents, progress-writing requirement or automatic promotion.
See [the evaluation example and protocol](examples/evaluation/README.md).

New campaigns use **GPT-6.1 Sol for research and GPT-6 Astra for reviews**, all at
**high** reasoning effort. Override `coach_model`, `closure_critic_model`, `advice_model` and
`claim_review_model` in TOML, or the corresponding `init --coach-model`,
`--closure-critic-model`, `--advice-model` and `--claim-review-model` options.
Each omitted override defaults to Astra; use `"inherit"` to follow the campaign
`model`, never the caller's model.
All roles retain the shared `reasoning_effort`, credit pool and deadlines;
model overrides do not enable otherwise disabled roles. Named models are checked
for availability and compatible effort/image support before research starts.
Changes require a new execution; there is no automatic model rotation or fallback.

An on-demand **campaign chat** provides cited evidence, images and factual
execution/spending status. The research inspector can also prepare idea drafts;
only your explicit Publish action sends captured files to the operator inbox.
It cannot edit research, attach to the researcher or change execution controls.
Browsing/publication are model-free; messages use a separate approved allowance.

Python 3.11+, native Windows and native Linux are supported. Copilot CLI must be
installed and authenticated separately. Native execution is trusted, **not a
hostile-code sandbox**.

## Start

Install from this checkout into a persistent environment; these instructions do
not assume a published PyPI release:

```powershell
python -m pip install -e .
labgoblin init .\my-research --goal-file .\question.md --credits 1000
labgoblin run .\my-research
```

`init` is deterministic and never calls a model. Without a supplied or existing
goal, it writes an editable `research_goal.md` brief with optional prompts for the
objective/intended use, context/inputs and genuine permissions/boundaries. Replace
those prompts before running; a free-form question is also valid. The evolving
research plan belongs to the researcher in `research-notes.md`, not to setup.
`--goal-file` copies your brief unchanged; existing goal and instruction files
are preserved, and conflicting goals are refused. Init does not generate or
semantically review a scientific plan. Review `labgoblin.toml` and
`.github\copilot-instructions.md`;
starting `run` authorizes the configured research execution and its credit pool.
No machine-capacity setup is needed. Runs share local hardware under normal OS
scheduling. CPU/RAM limits default to `"unlimited"`; set `--cpus` and `--memory-mb`
at init (or edit TOML before running) for optional per-run safeguards. GPU access
still requires an explicit `--gpu` device UUID; it is nonexclusive and has no
VRAM quota. Process ownership, cancellation, deadlines and credit caps remain.

New projects also prepare/reuse an isolated `.venv`, separate from LabGoblin's
own installation. The researcher may manage its scientific dependencies, download
public license-compatible models, and perform local inference, training and
fine-tuning. These are examples of broad local research autonomy, not a technique
whitelist. Initialization downloads nothing; explicit project restrictions,
data privacy and admitted resources still govern. Use `init --no-research-venv`
for a manually prepared environment with permissions specified in the goal.
Upgrading LabGoblin does not grant these new permissions to existing projects.

Init also creates Git history storage or reuses the containing repository, adding
local ignore rules without staging, committing or changing Git identity. Use
`init --no-git` to opt out. Researchers choose meaningful checkpoints, safe
selected-file rollback and artifact organization; there is no commit schedule.
Git does not protect ignored/untracked evidence or replace an independent backup.

Coaching, on-demand consultations/claim review and closing criticism are enabled
by default. Use `init --no-coach --no-consultant` for a researcher alone, or
`--no-closure-critic` to retain coaching and advice without the closing critic.
At initialization, disabling either prerequisite also disables the default critic;
explicitly requesting a critic without both prerequisites is an error. When editing
an existing TOML, set `closure_critic = false` alongside any disabled prerequisite.
Explicit settings and already-frozen executions remain unchanged; omitted candidate
settings use the new defaults at the next execution. No configuration is rewritten.
The default execution deadline remains 90 minutes, including shutdown and stopping
reviews; three-hour advisory check-ins become relevant for longer configured runs. A controller
restart does not reset spending or extend already-admitted work.
`max_stalled_reviews = 10` controls the consecutive no-progress ceiling, not
the total number of reviews; set it in TOML or with `init --max-stalled-reviews`.

## Ordinary research, small controls

The researcher works with `research-notes.md`, `report.md`, code and self-organized artifacts.
Keep large read-only source datasets in `inputs\` or outside the workspace;
the harness does not duplicate them into result snapshots.

| Command | Meaning |
|---|---|
| `init PROJECT` | Create a fresh project without inference or overwriting existing state. |
| `run PROJECT` | Start the first execution, or adopt its still-running independent supervisor. |
| `run PROJECT --resume` | Explicitly start another conversation from retained files, using the remaining cumulative allowance. |
| `status PROJECT` | Read execution state, loaded configuration and accounting; never reconcile or start work. |
| `inspect PROJECT --open` | Browse research evidence locally, without model calls or research changes. |
| `evaluations PROJECT --run RUN_ID` | Read paired-check history; add `--check CHECK_ID` for case discrepancies. |
| `stop PROJECT` | Request bounded cancellation of exactly this project's owned execution. |
| `reconcile PROJECT` | Retire only proven-dead owned executions; never replay research. |
| `record-usage PROJECT ...` | Append explicitly confirmed final usage or a separately labeled operator estimate. |
| `machine status` | Inspect execution claims and retained legacy capacity. |

Commands produce JSON and return nonzero on failure or an incomplete run.
Ctrl+C requests cancellation; abrupt controller death instead leaves the
independent worker to finish within its original deadline. A new `run` adopts that
worker rather than starting a duplicate. `--resume` after a settled execution
starts a new provider conversation, not a promised resurrection of an interrupted
SDK session.

## Campaign chat and operator ideas

```powershell
labgoblin inspect .\my-research --open
# Optional paid questions: choose an installed/enabled model and a separate allowance.
labgoblin inspect .\my-research --open --credits 100 --model MODEL_ID --reasoning-effort medium
```

The first command is view-only, even if chat was authorized previously.
The second authorizes a **cumulative** inspection soft cap, not another 100
credits on each restart. No model runs until you submit a question. Each answer
uses a fresh restricted session, bounded previous chat and freshly read evidence.
It can explain findings, show captured PNG/JPEG images and prepare idea drafts.
Review their exact text/attachments and click **Publish idea** to send them.
It cannot run code, retrieve external literature, edit research or restart it.
The researcher receives short pointers at startup or a stopping boundary, not
whole documents injected into its prompt. It chooses whether/how to investigate.
Later edits require new revisions; earlier publications and replies are retained.
Late ideas do not restart a closed execution or extend its limits.

For an already written document, the model-free operator command uses the same inbox:

```powershell
labgoblin ideas publish .\my-research --file .\idea.md --title "A useful control" --id control-idea
labgoblin ideas status .\my-research
```

Use `--attachment FILE` for explicitly selected supporting files, and `--base
REVISION` when updating the same `--id`. Editing a source file does not silently
republish it. Publication captures exact bytes and rejects concurrent changes.

Use the exact loopback URL printed by the command; its fragment authorizes that
browser tab. History and captured citations live outside the research workspace.
Completed executions prefer immutable snapshots; live reads and truncated
session evidence are labeled. Ctrl+C closes the inspector and cancels its own
pending answer, not research. See the operating guide for bounds and recovery.

## Spending and evidence

`max_ai_credits` is one project-wide **soft cap**, including every researcher,
coach and adviser session across explicit resumes. It defaults to 1000; `"unlimited"` is an
explicit alternative, not the meaning of numeric zero. Reported usage comes from
`totalNanoAiu`, not premium-request multipliers. In-flight responses can overshoot.
Unknown final usage blocks another admission; partial checkpoints do not become
final bills. Approved estimates stay separate from reported usage.

When coaching is enabled, the researcher and coach share that pool. The initial
researcher leaves the provider's 30-credit minimum for a stopping review.
Unused allowance is reassigned
from **observed spending**, including an in-place update of the idle researcher's
cumulative session ceiling before any continuation. There are no task-cost
forecasts or per-role credit settings.

Consultations are available by default but called only when useful; disable them
with `init --no-consultant`. The capability authorizes workspace evidence
reads and public literature retrieval, not uploads or arbitrary agent spawning.
Researcher, coach and advisers all share the same project pool, with no separate
consultation quota. At an owned consultation tool boundary, the harness first
reduces the blocked caller's ceiling to its observed spending plus 30 credits
for its response. The adviser can use the remaining pool except protected review
funds. Unused allowance returns to the caller after final adviser settlement,
before releasing the tool result. Unlimited projects have no hidden adviser cap.
With closure criticism enabled, the host may repartition unused project allowance
at the settled, idle stopping boundary, after reducing the coach's commitment.
The critic may use the unspent pool except protected coach/researcher allowance;
it has no separate credit or evidence-operation cap. After settled criticism,
unused allowance funds the researcher response and fresh adjudicator;
no session's previous spending is reset.
The default combination needs at least 90 credits; coaching-only or
consultation-only needs 60, and a researcher alone needs 30. Defaults do not
increase the project's allowance. Reviews can consume a substantial share of it
and do not certify scientific validity.
Ordinary advice and claim reviews default to at most 60 minutes including shutdown
(`consultation_timeout_seconds = 3600`); periodic goal reviews default to 30 minutes
(`goal_review_timeout_seconds = 1800`). Both are configurable ceilings, not target
durations, and cannot extend the campaign deadline.
The critic has at most two hours, bounded by the campaign deadline with ten
minutes retained for the researcher response and another ten for adjudication.
The response can use more available time but must leave the adjudication reserve.
The coach itself has no separate time cap. These limits are host-enforced;
routine model prompts do not repeat countdowns or balances. Individual evidence payloads stay bounded.
See the operating guide for exact bounds, failure behavior and retained artifacts.

Each execution keeps frozen instructions/configuration/helpers, bounded provider
events and logs, qualified spending records and a terminal receipt. Full workspace
archives are **off by default**; select `init --archive-workspace` or set
`archive_workspace = true` before a new execution if needed. Snapshots retain regular source/output files, including
multi-gigabyte artifacts, within explicit byte/file-count limits. Excluded inputs,
environments, common credential paths (including `.env*`), state, caches, links
and special files are listed. These exclusions are not a general secret scanner.
Working files remain editable; this is not a database of every scientific interpretation.
Resume uses those working files, not archive restoration. Archive failures are
reported separately and never replace the research outcome or discard settled usage.
Exact paired-evaluation captures remain independent of this option.

Original SDK events are split into bounded segments with a host-built sequence,
prompt and tool-call index. The inspector can filter excerpts and page full
retained events without asking the researcher to narrate progress.
Retention defaults to **8 GiB per session**, including indexes; configure
`max_event_log_bytes` (positive bytes, up to 64 GiB). A best-effort 512-MiB
disk-headroom guard and explicit missing-tail records preserve bounded storage.
Primary stopping reasons and phase-specific shutdown errors are retained separately.

After separately approving inspection credits, `labgoblin run PROJECT --handoff`
opts that execution into an automatic dated end report. A fresh read-only session
reads retained logs, reviews, evaluation evidence and any qualified terminal
archive, outside researcher context. Uncaptured historical files remain explicit
coverage gaps; the reporter cannot substitute today's mutable files.
Reports use the **separate inspection pool**, not research credits. Read the growing
history with `labgoblin handoffs PROJECT` or the inspector; no model runs on reads.
`labgoblin handoff PROJECT --run RUN_ID` explicitly requests a report for an older
terminal run. Reports and failures are retained outside research, never overwrite
researcher notes, and cannot authorize a restart.

**A completed process or an accepting coach is not independent validation.**
Read the report, inspect decisive artifacts and check any domain-specific
evaluation before adopting a scientific result.

## A deliberately breaking simplification

Version 3 uses configuration/project format **5**, an internal execution format
**4**, and the shared machine ledger's existing **3** exchange format. There is
no scientific campaign database and no campaign migration.

The legacy implementation is preserved locally at commit
`9efa22bc88fd95ac23a3b4685a218ff913031c9c`.
Keep its compatible installation for existing campaigns. This replacement rejects
old state without modifying it. It does not upgrade old helpers or receipts,
replace a live ledger, or clear somebody else's reservations.

Claude, managed WSL/Docker backends, assisted setup, delegated research scopes,
the old dashboard/observer orchestration, structured publications, supervisor memos and dedicated
research report/compaction/final-analysis jobs are retired from this runtime. Independent
opt-in terminal handoffs belong to inspection instead. There is no
second legacy orchestrator hidden behind a simple-mode flag. No automatic image
pulls, shared/system installations, remote execution, uploads or unrestricted
nested agent/provider sessions are part of the new loop. Explicit initialization
may prepare the separate research `.venv`; its recorded authorization permits
researcher-managed scientific dependencies and local models. Only the explicitly enabled,
host-owned consultation tool can start an adviser inside research. The independent
on-demand inspector does not restore that orchestration or change the research loop.

See the [operating guide](docs/local-research.md), [research and coaching
guide](docs/research-loop.md), [execution contract](docs/runtime-protocol.md) and
[small example](examples/local-synthetic/README.md).

## Development and attribution

```powershell
python -m pip install -e .
python -m pytest tests -q
```

Ordinary tests use model-free SDK doubles, including a real local stdio protocol
fixture and actual native process trees. Paid evaluations require separate,
explicit authorization and a persisted allowance outside the evaluated project.

LabGoblin is derived from
[Roger Creus Castanyer's xgenius](https://github.com/roger-creus/xgenius), not a
drop-in replacement for its SLURM harness. The original [Atari
report](examples/auto-cleanrl-report/report.html), [Craftax
report](examples/auto-craftax-report/report.html), figures and generators are
preserved historical material, not validation or runtime tools for this edition.

The original software citation remains:

```bibtex
@software{creus2026xgenius,
  title = {xgenius: LLM-Oriented Autonomous Research Platform for SLURM Clusters},
  author = {Creus Castanyer, Roger},
  year = {2026},
  url = {https://github.com/roger-creus/xgenius},
  doi = {10.5281/zenodo.19038735},
  version = {1.0.0}
}
```

MIT license; see [LICENSE](LICENSE) and [CITATION.cff](CITATION.cff).
