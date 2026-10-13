# Operating local research

## Fresh initialization and capacity

Install from the [LabGoblin GitHub checkout](https://github.com/tupini07/LabGoblin),
using `python -m pip install -e .` there with the prepared environment's
interpreter. This onboarding does not assume a published PyPI distribution.
Return to the intended campaign directory before initializing it.

Run `labgoblin init --agent copilot` in a dedicated project, or omit `--agent` for
Claude as the initial research provider. Terminal init uses a Copilot SDK
conversation, regardless of that research-provider choice. The SDK is a core
dependency; install/authenticate the standalone CLI first with `copilot login`.
Missing prerequisites give an explicit error and `--non-interactive` guidance,
not a fallback or a partially created campaign.

Use `init --non-interactive` or `init --json` for starter defaults with no SDK,
login check or model call. Non-terminal invocation needs one of those flags.
`--setup-model MODEL` selects the setup model; `agent.model` configures research.
The conversation has no harness usage/time cap and is not resumable.

Describe intent, evidence and constraints, then optionally select a local path
with `/context PATH`. This grants bounded metadata access, not blanket sharing.
Each non-secret text file requires separate approval; dataset bodies, credentials
and runtime logs are unavailable. Named readiness probes require approval and
cannot install, build or run experiments. Checks not performed remain unverified.
Use `/review` to inspect exact document/config changes and initial versioned
protocol/constraints, then type the displayed Apply phrase or continue correcting.
`/cancel` or EOF leaves the campaign untouched. No experiment code is generated,
no machine capacity is created, and initialization does not start research.

Applied setup creates schema-4 state and a shared owned section in `CLAUDE.md`,
preserving unrelated text. `instructions --target copilot` explicitly updates
the owned section in `.github/copilot-instructions.md` when that file takes
precedence. Nothing changes global provider settings.

For a project-local Windows virtual environment, use
`.\.venv\Scripts\labgoblin.exe` without changing PATH. To use bare `labgoblin`
instead, activate that environment or prepend its `Scripts` directory to PATH
in the current terminal. Managed research sessions receive that interpreter's
scripts directory on PATH automatically; no global PATH edit is required.

Config loading never creates state or migrates old installations. After an
explicit quiescent `reset --confirm ID`, `init --existing-config --non-interactive` creates fresh
state without overwriting the existing configuration/goal. Reset archives the
old directory, not the ledger, and never starts research.

Publication uses a `.labgoblin.initializing` ownership marker until all approved
files and initial state/provenance are complete. Concurrent edits invalidate the
preview. Errors roll back only unchanged attempt-owned writes; a crash or unsafe
rollback leaves a diagnostic marker and blocks ordinary campaign entry points.
Inspect its exact file hashes/error and preserve concurrent human edits rather
than blindly deleting state or treating the campaign as ready.

### Applying configuration changes

Every controller run loads one TOML snapshot after recovering existing work.
Provider/model/effort, runners, inputs, environments, storage, resources and all
budgets stay fixed for that run, even if TOML is edited, removed or malformed.
`validate` and `doctor` inspect a **candidate**, not the active settings.
Status/dashboard budgets show the loaded snapshot. Submissions and builds issued
by separate processes use it too; ownership changes during preparation fail
explicitly instead of mixing configurations.

To apply edits without recreating research: interrupt the controller with Ctrl+C
(which pauses admission), edit TOML, run `labgoblin validate --json`, then
`labgoblin run` to load the new snapshot while paused. Use `labgoblin resume`
and `labgoblin run` to continue. If no controller was running/paused, the next
`run` loads the file directly. Invalid startup configuration requires another
run after correction; it is not retried automatically. `reconcile` remains
available without valid TOML.

Do not use `stop`, `reset` or `reopen` merely to load configuration. Restart
retains campaign identity, generations, evidence, grants and cumulative usage.
Admitted work keeps its frozen settings/deadline. Incompatible queued runner,
input/environment/source/storage or resource requests are reported as not started, retaining their original spec;
submit a new compatible request explicitly. An old still-running research
packet cannot submit under a different newly loaded configuration.
Dashboard chat has its own process-start settings; restart the dashboard to
apply its TOML edits.

The shared ledger defaults to `%LOCALAPPDATA%\labgoblin\resources.db`
on Windows or `~/.local/state/labgoblin/resources.db` elsewhere. Tests can select an isolated
ledger with `init --ledger PATH` or `LABGOBLIN_RESOURCE_DB`. Production campaigns
sharing a workstation must coordinate through the same ledger; a new filename
is not permission to ignore work in an older ledger.

Configure capacity explicitly with `machine configure --cpus 2 --memory-mb 4096
--headroom-mb 2048`. Reconfiguration refuses active/uncertain grants.
Reasoning, experiments and maintenance compete fairly for capacity; a one-slot
deployment alternates rather than keeping a permanent reasoning reservation.
The oldest eligible satisfiable request gets a drain-to-fit barrier.

## Names and existing installations

LabGoblin is derived from [Roger Creus Castanyer's xgenius](https://github.com/roger-creus/xgenius),
but is **not drop-in compatible with the original cluster harness**. The product,
distribution/import and command are `LabGoblin`, `labgoblin` and `labgoblin`.
Version 2.0.0 retains all four schema/protocol versions at 3, but **old names are
not supported**. There is no compatibility package, executable alias, automatic
path lookup, environment alias or browser-preference migration.

Fresh `init` writes `labgoblin.toml`, `.labgoblin/labgoblin.db`, the external
`.labgoblin.lock` and LabGoblin-branded agent instructions. Managed sessions and
payloads expose only `LABGOBLIN_*` variables, including project/turn identity,
`LABGOBLIN_OUTPUT_DIR`, `LABGOBLIN_ATTEMPT_ID` and `LABGOBLIN_INPUT_NAME`.
Python consumers use `LabGoblinConfig` and `labgoblin` imports.

Create a new campaign in a dedicated directory. Renaming an old campaign's
files is not a supported migration: its instructions, snapshots, packets and
frozen runtime may still require the old installation. Preserve that installation
and its state separately if recovery or historical access is needed. Never
rewrite admitted envelopes, receipts or ownership records to make them look new.

Only `LABGOBLIN_RESOURCE_DB` or explicit `--ledger PATH` overrides the default
machine-ledger location. Old environment variables and app-data directories are
not searched. Before configuring this default on a previously used workstation,
establish quiescence with the old runtime. Back up the old ledger; if reusing its
schema-3 capacity, explicitly move the inactive ledger or select its existing
path. Do not create a second capacity pool while old work could still consume
resources. Campaigns and envelopes use their recorded ledger path and identity;
a missing or mismatched bound ledger remains an error.

Use a fresh environment for LabGoblin, or explicitly uninstall the old
distribution before installing from this checkout. An editable installation
must be reinstalled after entry-point changes. Installing LabGoblin does not
rename any existing campaign or start research.

Frozen helpers require an explicit `labgoblin` namespace and exact manifest/
bootstrap hashes. Missing or old namespaces are rejected. Ownership names,
Docker labels and temporary build tags use `labgoblin`; SQL application IDs
remain unchanged. Missing/mismatched ownership stays uncertain and retains its grant.
`CITATION.cff`, the original citation, copyright and historical reports describe
the original software, not a renamed historical method.

## Configuration

Init records the Python executable that ran it. Use a persistent prepared
environment. Add only the runners needed for the investigation; no fallback is
performed. `doctor --runner NAME --provider` checks the selected runner and
model-free provider help, not all optional runners or a billable canary.

```toml
schema_version = 4

[project]
name = "local-study"
research_goal = "research_goal.md"

[execution]
default_runner = "native"
source_files = ["experiment.py"]

[runners.native]
kind = "native"
python = 'D:\research-env\Scripts\python.exe'

[runners.ubuntu]
kind = "wsl"
distro = "Ubuntu"
python = "/usr/bin/python3"

[runners.container]
kind = "docker"
context = "default"
image = "python:3.11-slim"
python = "python"
network = false

[campaign]
cpus = 2
memory_mb = 4096
gpus = []
max_jobs = 1
max_gpu_hours = 0
max_seconds = 3600
max_invocations = 10
max_ai_credits = "unlimited"

[agent]
provider = "copilot"
command = ["copilot", "--allow-all"]
model = ""
reasoning_effort = ""
timeout_seconds = 1800
transport = "sdk"
idle_timeout_seconds = 600
shutdown_grace_seconds = 30
retries = 1
sandbox = false

[agent.resources]
cpus = 1
memory_mb = 2048

[storage]
log_bytes = 16777216
tail_bytes = 65536
snapshot_bytes = 268435456
capture_bytes = 1048576
metrics_bytes = 262144

[storage.volumes.project]
path = "."
min_free_mb = 2048

# [inputs.observations]
# path = 'D:\approved-data\observations.json'
# identity = "observations-v1"
# prompt_access = false
# sha256 = "64 hexadecimal characters for an explicitly bounded file"
# verification_bytes = 67108864
# assurance = "checked"
```

All fields are strict; unknown or inapplicable options fail. Newly generated
configs always include editable `model` and `reasoning_effort` fields under
`[agent]`, for both Claude and Copilot. Empty strings (or omitted fields in a
handwritten config) preserve the provider default/unknown, not a claimed
effective model. Set these fields rather than adding model/effort flags to
`agent.command`. `init --existing-config` preserves the supplied file unchanged.
Explicit choices must be supported by installed provider help. Commands are
argv arrays, not shell strings. Claude defaults to `claude --dangerously-skip-permissions`
and its child environment removes `ANTHROPIC_API_KEY` for subscription auth.
Windows batch shims must be replaced by a direct executable argv.

### Large artifacts and bounded reads

`storage.capture_bytes` defaults to **17179869184 (16 GiB) per artifact**.
New experiments freeze a total immutable-retention allowance of four times
that value (**64 GiB per attempt** by default), including metrics. Increase
the setting for larger videos, datasets or checkpoints; zero is not unlimited.
For example:

```toml
[storage]
capture_bytes = 17179869184
```

Captures, exact file exports and dashboard downloads stream in bounded chunks;
artifact size does not become model-context size or an equally large memory
allocation. Metrics keep their separate 256 KiB default. Exceeding an allowance,
missing output, changed source or insufficient destination space is reported
explicitly. Free-space watermarks still apply. Native programs can write more
than the retention allowance: this is not a filesystem quota.

Use `labgoblin evidence observation --id ID --export PATH --json` to export a
whole verified artifact to a new file outside campaign state. Existing different
files are never overwritten. Without `--export`, `--offset` and `--bytes` select
a base64 byte page (at most 1 MiB); inspect `returned_bytes` and `has_more`.
SDK `research_read` operation `artifact` provides pages of at most 16 KiB;
operation `observation` still returns metadata. A page read verifies the whole
object's SHA-256, so repeated reads of large objects incur full verification
I/O; export once for local analysis rather than paging an entire dataset
through the model. Downloads preserve exact binary bytes; inline report
figures and previews remain small.

Limits are frozen at preparation. TOML changes require controller restart and
apply to new experiments, not already prepared jobs or historical observations.
Old unmanaged references do not become captured evidence after an upgrade;
explicit recollection creates a separate observation under the original
attempt's limits. New packets state the available artifact-storage allowance.

### Research-turn timeouts

`agent.timeout_seconds` defaults to **1800 seconds** and remains a hard bound
on the entire owned research/maintenance operation, not on separately submitted
experiments. Copilot defaults to `transport = "sdk"`: the installed CLI still
provides its normal tools and instructions, but a frozen, resource-owned SDK
driver observes response starts, completed responses, streamed reasoning/text
and tool execution. Claude uses `transport = "cli"`; Copilot can explicitly use
that transport for custom CLI-only adapters, without SDK activity or graceful
usage capture. Existing admitted snapshots retain their original transport.
The SDK explicitly preserves a configured `--allow-all`/`--yolo` permission
mode; otherwise unanswered permission requests are denied, not left waiting
for a terminal. Narrower command-line permission rules (`--deny-tool`,
`--allow-tool`, URL/path overrides, assisted approval) currently require
`transport = "cli"` and are rejected by the SDK adapter rather than ignored.

SDK `idle_timeout_seconds` defaults to 600. An inactive session with no response
or tool in progress is cancelled after that interval without meaningful progress.
A pending response or long-running tool is **not** proof of a hang, and is allowed
to continue up to the hard deadline. A quiet pending operation is reported as a
warning, not silently granted unlimited time. Log writes and transport heartbeats
do not reset the progress clock or extend the hard deadline.

`shutdown_grace_seconds` defaults to 30 (at least 5, and less than the hard limit).
The driver reserves this time **inside** the hard deadline to abort, wait for
provider idle, retrieve final usage and close the runtime. Operator cancellation
also permits at most this grace, bounded by the original deadline. The native
owner still force-retires the process tree at the hard limit. A crash, unresponsive
provider or failed final metrics query can still leave usage unknown; no timeout
heuristic proves a complete bill. Dashboard observer timeouts are unchanged.

## Budgets and control

`max_seconds = 0` is an unlimited campaign admission horizon;
`max_invocations = 0` is unlimited managed campaign invocations. Starter values
are finite. `max_gpu_hours = "unlimited"` explicitly removes the cumulative
GPU-hour cap; numeric zero GPU-hours/no devices still means no GPU work.
Finite GPU-hour limits include already used time and active job reservations.
Unlimited GPU hours retain usage/reservation accounting, explicit device
selection, shared-device ownership and finite job deadlines. Budget/status and
the dashboard label unlimited GPU hours explicitly. Resource, storage and
per-operation limits remain strictly positive.

`research.task_max_seconds = "unlimited"` removes the total worker-task lifetime
cap, not the individual job/reasoning deadlines or task invocation limit. The
default is 7200 seconds from task creation, including waits. New task charters
inherit this setting unless they request a smaller allowance; old charters and
admitted launches keep their original limits. See the
[research-loop guide](research-loop.md) for multi-hour managed jobs.

Elapsed time begins with the first admitted campaign operation, includes
shutdown/pauses, never decreases on clock rollback, and does not reset on
reopen. Expiry gates new admission, not an already admitted operation's deadline.
Changing TOML limits takes effect at controller restart; it does not reopen work.

For Copilot research, `campaign.max_ai_credits = 1000` sets a cumulative soft
credit allowance. Omit it or use `"unlimited"` to disable the cap; zero is invalid.
The CLI must advertise both `--max-ai-credits` and `--usage-output-file`.
Claude does not support this Copilot-credit setting. Credits are not premium
request multipliers, tokens or invocations.

Each launch receives its allowance after existing commitments and finishing
reserves. Only actually prepared/ready reasoning scopes share the unreserved
pool; idle configured slots do not shrink a lone researcher's call.
Copilot requires at least 30 credits per session. Final analysis reserves at
least 30, using observed accepted completion costs cautiously when enough
comparable history exists; `budget --json` exposes the reserve's basis and
uncertainty. Remaining stopping-coach/reflection steps and sandbox canaries
also retain their minimums. These are planning reservations, not spent credits.
Insufficient remaining
credits close admission rather than being rounded up. The provider accounts for its
subagents and credit-consuming compaction. Responses already in flight can exceed
the cap; this is not a guaranteed billing ceiling.

An explicit SDK session-limit event without an acceptable root handoff blocks
further admission rather than purchasing a smaller retry. The journal retains a
model-free operational checkpoint, not an invented scientific assessment.
Inspect retained outputs, receipts and accounting, then use explicit operator
`resume` if continuation is appropriate; merely restarting cannot clear this
blocker. Other recovery and accounting blockers must still be resolved. A valid
handoff is not rejected simply because its session overshot a soft allowance.

Authoritative usage is retained in owned receipts, including failed invocations,
and is counted exactly once across retries, controller restarts and generations.
Missing or invalid `totalNanoAiu` is unknown, never zero: a capped campaign blocks
further admission unless an operator explicitly approves an estimate as described
below. Raising the limit does not erase unknown usage. Inspect the
owned receipt/diagnostics; explicitly choosing `"unlimited"` removes that protection,
it does not repair the accounting. A proven non-start consumes no credits.
Already-admitted jobs retain their frozen execution and are not cancelled by a
credit limit. `budget --json`, status and the dashboard distinguish reported,
reserved and unknown usage. Limits cover managed provider activity, not unrelated
sessions or model services called outside the harness.

SDK launches write identity-bound `activity.json`, `usage-progress.json` and,
only after the provider has settled, `usage.json` in their owned launch directory.
Partial usage is a known lower bound, not a final total; the budget includes it
in reported spend but keeps the invocation unknown and blocks capped admission.

If final authoritative metrics are recovered later, an operator can append them:

```powershell
labgoblin reconcile-credits --id INVOCATION_ID --file FINAL_USAGE_JSON --confirm CAMPAIGN_ID --reason "Recovered final provider metrics" --json
```

The file must be a final `usage.json`-shaped record: top-level `totalNanoAiu`,
and `_labgoblin` containing the exact `campaign_id`, `invocation_id`, `nonce`,
`envelope_digest`, provider `session_id`, `complete: true` and
`basis: "provider-session-metrics"`. For a legacy CLI launch, an operator must
obtain and bind genuinely final provider metrics; do not relabel a partial sum
as complete or substitute an estimate. The command requires a terminal owned
invocation, refuses conflicting replays or totals below retained partial usage,
and retains the input bytes, receipt digest and operator reason as a separate
immutable accounting source. It neither rewrites receipts nor accepts a handoff,
restarts research, changes the allowance or erases an unresolved usage gap.

If final usage cannot be recovered, an operator can instead explicitly approve a
budget estimate for that invocation:

```powershell
labgoblin estimate-credits --id INVOCATION_ID --credits 1000 --confirm CAMPAIGN_ID --reason "Operator approved a conservative estimate for the interrupted invocation" --json
```

This is a distinct, audited `credit_estimate` source, not fabricated provider
usage. It requires a quiescent executed invocation without final metrics, cannot
be below retained partial usage, and refuses conflicting replays. The estimated
total includes any known partial usage; only its additional portion appears as
`estimated_used`. `used` stays reported usage, `accounted_used` includes the estimate,
and the existing cap is unchanged. The unknown record remains visible, but no
longer blocks admission when covered by explicit operator approval. Other unknown
invocations still block. Remaining allowance is approximate because the actual
charge could differ. Later final reconciliation replaces the estimate in budget
calculations while preserving both audit records. This command neither restarts
research nor changes scientific authority; dashboard restart loads the updated
estimate display.

Invocation accounting covers research, retries, report, compact, final analysis
and model-executing sandbox canaries. A canary/main bundle reserves both calls
before the first; uncertain armed launches retain their commitment. A finite
open generation reserves one final analysis plus any required canary. This is a
managed-process count, not an API-call/token/money cap.

`pause` gates new admission; an armed turn may finish and retain its action.
`resume` restores eligible progression, including a deferred finalize decision.
`stop` retires queued work and drains admitted work without a final model call.
`cancel --id ID` targets one owned attempt. `reopen` requires quiescence and
starts a new generation with old evidence/accounting intact.
Controls accept `--request-id` and `--expected-revision`; replaying a request
returns its original response and cannot undo newer intent.

`status --json` is observational. `reconcile --state-dir PATH` performs recovery
without requiring usable current TOML, providers or unrelated runners. A missing,
replaced or incompatible recorded ledger is an error, never an empty ledger.
Keep uncertain records/grants; do not fabricate receipts or restart their work.

## Experiments and evidence

```json
{
  "key": "baseline-001",
  "experiment_id": "baseline",
  "runner": "native",
  "argv": ["python", "experiment.py", "--offset", "0"],
  "source_files": ["experiment.py"],
  "cpus": 1,
  "memory_mb": 256,
  "gpus": [],
  "seconds": 60,
  "artifacts": ["metrics.json"]
}
```

Use `validate --spec work.json`, `submit --spec work.json`, or
`batch-submit --file batch.json`. A batch preserves individual failures and
returns nonzero if any item fails. Reusing a key with the identical request is
idempotent; changing the request requires a new key.

`labgoblin schema work --json` lists all manifest fields, types and a minimal
example without loading configuration or campaign state. Resources are the
top-level `cpus`, `memory_mb` and `gpus` fields, not a nested `resources` object;
`gpus` is an array of physical device IDs, never a numeric count.
`labgoblin schema research-update --json` similarly documents publications and
inline handoff updates, including the required stable `request_id` and exact
revision `base`. These structural descriptions do not replace runtime checks
of resources, paths, ownership or evidence identities.

`python`/`python3` select the runner's configured interpreter. Argv preserves
spaces, empty strings, quotes and Unicode. Explicit `source_files` are copied
with an enforced byte cap and hashes; Git identity alone is not a snapshot.
No environments, datasets, caches or dependencies are automatically copied or
installed. Payloads inherit essential PATH/home/temp variables and explicit
non-secret overrides, not the controller's complete credential environment.

Write artifacts under `LABGOBLIN_OUTPUT_DIR`. A `metrics.json` artifact is an
object of finite numeric values, not booleans or strings. Optional
`input_validators` and `validators` are arrays of argv arrays and share the
operation's resources, deadline and cancellation; include their source files.
Successful execution and invalid output remain distinct facts.

Work creating a new hypothesis supplies both `hypothesis_id` and a scientific
`hypothesis_description`, not its own opaque ID. For an existing hypothesis,
omit the description to reuse its exact stored statement. Validation and
submission share this lookup; explicitly conflicting descriptions fail before
source copying. Validation uses read-only campaign state when available and
still checks the disk candidate configuration, not admission eligibility.
Evaluated statements cannot be
changed. Use a new ID and optional `hypothesis set --supersedes OLD_ID` for a
revised claim. Support/setup/replication work need not invent a hypothesis.

Declared inputs use `LABGOBLIN_INPUT_NAME`. `declared` does not certify content;
`checked` requires an explicit bounded hash policy; pre/post checks do not prove
no mutation during execution. `stable-consumption` prepares only a bounded small
file and requires supported Windows read leases or read-only Docker access.
Unsupported guarantees are refused. `prompt_access` records disclosure policy
but cannot constrain a trusted provider's independent tools.

Small exact observations are captured once and retrieved with
`evidence observation --id ID`; downloads serve those captured bytes.
Large unmanaged artifacts are qualified references, not historical byte claims.
Recollection creates a new observation instead of changing an old report.
`logs --id ID` and `errors --id ID` return bounded tails and truncation metadata.

## Owned memory, maintenance and closure

Each turn gets a bounded immutable packet and one result path. Its delta handoff
records observations/support work, what changed and why, the governing next
step, evidence dispositions and a continue/wait/blocked/finalize decision.
Handoff acceptance, exact acknowledgements and the journal projection are one
transaction. Writing an unrelated checkpoint cannot substitute for it.
Accepted `goal_progress` assessments keep original deliverables and unresolved
gaps visible before the local plan. Update them only when assessments change.
Branch/milestone `commitment` entries explain decision relevance or useful
prerequisites, not predicted credit costs; ordinary continuation needs no form.
Both are revisable researcher judgments, not new operator authority.

`steer --text TEXT` records attributed operator constraints; `--supersedes ID`
explicitly replaces one. `source set --kind goal|protocol --file PATH` versions
small authority. Observed manual edits are retained as imports, with observation
time distinguished from edit time. `journal search --query TEXT` searches a
bounded prefix coverage; `journal entry --id ID` retrieves retained original
text. Zero lexical matches do not establish the absence of evidence.

`compact` and `report` request fixed maintenance at an eligible safe point; they
do not launch a second provider from inside a research turn. An explicit operator
request may run after quiescent closure without reopening research. New control
revisions fence pending maintenance. Archive growth alone does not trigger paid
compaction because fresh packets already bound context. Explicit compaction is
deduplicated by source revision and produces at most 4096 UTF-8 summary bytes;
it cannot delete authority or repeatedly spend on a non-shrinking summary.
Reports are also explicit, not automatic periodic inference.

A failed branch is not a campaign stopping criterion. Root stopping proposals
distinguish `goal_satisfied`, `operator_needed` and `discretionary`; discretionary
partial closure needs an explicit operator decision even if a coach accepts it.
Use `research coach --id EPISODE --decision continue|accept_stop --reason TEXT`
for a settled unresolved review. See [research-loop.md](research-loop.md) for
the policy and reference contract.
With coaching enabled, selected commitment/evidence boundaries can also produce
nonbinding strategy advice. This cannot stop, veto, prescribe experiments or
require a separate reflection. Unavailable advice does not block normal research.

Within an owned turn, prefer SDK `research_complete`, or
`research complete --file DRAFT.json`, to validate and write the owned handoff
in one action. Owned IDs may be omitted; conflicting IDs are rejected.
`schema handoff --json` exposes the shape. End the turn after successful
recording; controller acceptance still requires the exact completed receipt.
Manual result files can still use `research preflight --file HANDOFF.json` (or SDK
`research_preflight`) to check a complete handoff without accepting it. SDK final
analysis can correct structure/references once within its original allowance
and deadline, preserving its draft and error. It cannot change conclusions,
run new experiments or reopen the inventory. Uncorrected output remains
unassessed; reopening requires the operator's normal `reopen` command and
preserves all prior generations, evidence and cumulative spend.

`report --no-agent` always offers a deterministic inventory without inference.
Reports retain a complete cutoff denominator, exact source versions and selected
observation reasons. HTML/Markdown/JSONL outputs never overwrite previous reports.
Numeric claims are checked against captured metrics; prose and scientific
interpretation are not certified.

Finalize names the stopping criterion and limitations. The sealed cohort
includes unperformed queued work and every admitted attempt, including late
replications, failures and invalid measurements. One additional analysis at most
can assess the complete inventory. No allowance, failure, timeout or a more-work
decision gives an explicit incomplete/unassessed/needs-more-work outcome.
Later edits/directives mark historical assessment staleness without silently
expanding or rerunning it. Fully completed means assessed closure **and**
operational quiescence.

## Prepared backends and explicit Docker builds

WSL needs an explicit development distro with Python 3.11+ and Linux pidfd
support, not Docker Desktop's internal distro. Guest helpers are frozen copies
and write receipts, never host SQLite. Derived path/validator mappings are hashed
and verified at the guest entry point. Losing `wsl.exe` does not prove guest death.
Unavailable guest inspection keeps capacity reserved.

Docker execution requires a prepared existing Linux image and local Unix socket
or Windows named-pipe endpoint; remote contexts are rejected. It executes the
resolved image ID with `--pull=never`, no restart, read-only source/inputs/helpers,
finite CPU/RAM and explicit GPUs/network. It does not expose credentials, the
Docker socket, broad host mounts or privileged mode. A payload receipt alone does
not prove container quiescence. Completed owned containers remain inspectable.

An image build is an **explicit operator operation**, not automatic work in
`run`, and uses the optional `docker-build` extra. Install it from the LabGoblin
checkout with the campaign's prepared interpreter:

```powershell
python -m pip install -e ".[docker-build]"
```

Then, from the campaign's reviewed build-context directory:

```powershell
labgoblin build --runner container --context . --include experiment.py --seconds 600
```

The Dockerfile is always included. Repeat `--include` for every additional
relative file: this explicit allowlist, not `.dockerignore`, defines the reviewed
context. The copied context is bounded, hashed and retained. Existing local base
images become bare full IDs; dynamic/external stage references, ONBUILD bases,
ADD, heredocs and BuildKit syntax are refused. No base/front-end image is pulled.

The adapter uses the local classic Engine build API (`version=1`, `pull=false`),
not an unowned buildx daemon/plugin. The engine must support it. CPU placement
and memory/swap bounds apply to each build container, and `network=false`
disables RUN networking. Build vCPU IDs are not native host placement identities.
The grant additionally reserves one CPU and 256 MiB for its owned API client.
Engine bookkeeping/container layers remain covered by headroom and soft
volume monitoring, not a hard disk quota.

A complete engine response, native-client shutdown and no running owned build
containers are required for release. A killed client or lost response retains
uncertain daemon work and its grant; `machine reconcile` ingests only matching
terminal consumer receipts. There are no automatic retries, pushes, daemon
restarts, builders with elevated privileges or backend fallback.

## Reading the research dashboard

`labgoblin dashboard --open-browser` opens **Brief**, the research-first catch-up
view. It leads with a short root-researcher supervisor memo and separate live
controller/accounting facts. The detailed goal, accepted rationale, lifecycle,
recent changes and full allowance remain under expandable sections. Recovery
blockers are promoted; closed/old-generation memos are explicitly historical.
Brief shows an unknown unreserved balance when final billing is missing, not
zero or an available allowance; its other research and recovery details remain readable.
Missing, invalid and pending memos are visible and never invalidate accepted
science. History > Supervisor memos retains prior versions and evidence links.
Request an update with `labgoblin research request-memo --id REQUEST_ID --reason
"Explain progress" --json`; it accompanies an ordinary root turn rather than
starting another agent or resuming blocked research. See
[the memo contract](research-loop.md#human-supervisor-memos). Operator feedback
still uses explicit directives/controls, not observer chat or implicit approval.

**Evidence** connects hypothesis statements to explicitly referenced assessments
and observations. Execution, collection and validation are separate; a completed
process is not an accepted finding. Choose up to four measurements on one page
for side-by-side inspection. This is not a paired scientific estimator: values
keep their recorded keys, and no comparator, unit equivalence or aggregate
effect is inferred. Invalid measurements remain visible with their reasons.

**Work** separates active work and recorded problems from recovery requiring
inspection. Counts and their linked lists use the same filters. Recovery cards
identify work and offer copyable read-only status/log commands, not retry,
reconcile or release actions. Missing receipts do not prove an owner is dead.
Limits explain admission time, invocation commitments/reservations, unlimited
allowances and shared capacity without implying token or monetary budgets.
Machine requests are paginated; capacity totals include every granted
reservation, not just the current page.

**Work > Live monitor** samples the recorded controller and in-flight host
supervisors by PID plus creation time, without backend commands or recovery.
It shows up to 20 launches, preparation age, frozen operation timeouts, latest
nonempty log-write times, the last accepted handoff, resources and credit usage.
SDK research adds the provider phase, response-in-progress flag, active tool
count, last response timestamp, checkpoint age and quiet-operation warnings.
These are local observations of a specific launch, not a scientific verdict.
An alive supervisor does not prove payload health; a quiet log does not establish
a stall. Follow each work link for bounded main and supervisor diagnostics.
The credit meter shows reported spend only, not reserved launch allowances.

**History** holds generation outcomes, paginated/folded journal entries,
registered reports and source-change intervals. Reports open as safe,
digest-verified Markdown, with bounded byte pages for large outputs and explicit
errors for missing or changed files. Raw HTML and images are disabled. Historical
inventory links retain the same frozen attempt and observation context; switching
to current state is explicit. Failed, invalid, unselected and unperformed work
stays in the inventory denominator.

**Mark caught up** saves the displayed source/event sequence cutoffs in this
browser's local storage, scoped by campaign and generation. Navigation, reload
and server restart retain that preference at the same browser origin; it is not
a research acknowledgement or a claim about which changes were important.
**Forget checkpoint** clears it. Change lists show complete interval counts and
bounded pages, including newly ingested sources with older recorded timestamps.

**Check for updates** checks every 15 seconds while visible and not editing.
It announces recorded source, event, control, recovery and work-state changes;
it does not replace the inspected page, advance the checkpoint or probe a live
process. **Refresh** updates explicitly, retaining journal folds and reading
position where possible. Read times describe the page's recorded-state snapshot;
shared-ledger reads are separate. Ordinary browsing does not invoke a provider,
mutate campaign records, reconcile work or acknowledge events.

The dedicated **Live monitor** instead auto-refreshes its operational samples
every 15 seconds while visible and not editing. Uncheck **Auto-refresh monitor**
to hold a sample for inspection. Historical and research-reading pages retain
the nonreplacing update behavior; monitor samples are observations, not retained
scientific evidence or an automatic intervention policy.

### Dashboard Copilot observer

The SDK is installed with LabGoblin, but ordinary dashboard pages do not use it.
Chat is enabled by default; a broken SDK installation leaves pages usable and
shows explicit repair guidance rather than silently installing packages. Opening a
page never starts inference. Use `dashboard --no-chat` to disable the observer,
or set `enabled = false` below. Explicit `--chat`/`--no-chat` flags override the
configuration. Invalid configuration disables chat while retaining read-only
pages. It remains a fresh empty-mode
SDK session per question with curated read-only tools, not the research session.
Prior answers are hints, not evidence. Tools expose revisions, retrieval times,
pagination and searched coverage; unavailable historical IDs never redirect
silently to current summaries. Raw files/logs/datasets and research writes are
not available.

The context chip names the page you are asking about. Each submitted question
retains that page's read time and relevant source, observation or historical
view IDs even after you navigate. Context is an untrusted retrieval hint, not an
atomic snapshot of later tool calls. The observer must resolve exact references,
preserve historical validity and distinguish fresh recorded state. Retrying
the same uncertain request preserves its original question and context.

```toml
[dashboard.chat]
enabled = true
model = "auto"
reasoning_effort = ""
timeout_seconds = 120
cpus = 1
memory_mb = 2048
max_invocations = 20
max_ai_credits = "unlimited"
```

These are separate from `[agent]`. The positive invocation allowance applies to
the current dashboard process; usage does not spend campaign invocations.
Set `max_ai_credits = 100` for a separate chat soft cap. Chat credits are shared by
dashboards for this campaign and persist across dashboard restarts in the machine
ledger, without writing research state. Reservations prevent concurrent dashboards
from receiving the same allowance. Unknown usage after an interrupted answer
blocks further capped chat; SDK session limits have the same post-response caveat.
Setup is not charged to either campaign allowance. Waiting is visible
and cancellable. Native ownership includes the SDK and its descendants, and
failed shutdown keeps the grant. No silent model retry occurs.

Chat/sidebar/maximized state survives navigation/reload, not dashboard restart.
Only Close hides chat, and New chat clears it. Escape restores a maximized view
without discarding the conversation. Dashboard refreshes do not launch inference.
Only loopback is supported; Host/Origin/CSRF/CSP guards remain in force.

## Storage, isolation and recovery limits

`storage inventory` is a bounded cold-path walk, not a recursive hot-loop scan.
`storage retention --dry-run` only identifies explicitly marked uncommitted
preparations with proven-dead owners; unknown/unmarked/referenced objects remain.
There is no delete/apply mode.

Named-volume watermarks block admission/copy and monitor owned output roots
periodically. Unnamed locations, temp directories, native writes and container
layers are not subject to a hard quota. If a volume cannot persist a receipt or SQLite write,
missing data remains uncertainty, not an invented ENOSPC/terminal outcome.

Windows uses nonoverlapping supported native CPU sets, assigned Job Objects and
windowless independent launches. Affinity is not CPU-time quota. WSL uses
process-session monitoring rather than hard RAM enforcement. Unsupported
processor topology, unavailable ownership and unsupported sandbox policy fail
closed. The ledger does not control external programs or hostile same-user code.
