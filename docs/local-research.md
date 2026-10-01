# Operating local research

## Fresh initialization and capacity

Run `xgenius init --agent copilot` in a dedicated project, or omit `--agent` for
Claude. It creates schema-3 state and a shared owned section in `CLAUDE.md`,
preserving unrelated text. `instructions --target copilot` explicitly updates
the owned section in `.github/copilot-instructions.md` when that file takes
precedence. Nothing changes global provider settings.

For a project-local Windows virtual environment, use
`.\.venv\Scripts\xgenius.exe` without changing PATH. To use bare `xgenius`
instead, activate that environment or prepend its `Scripts` directory to PATH
in the current terminal. Managed research sessions receive that interpreter's
scripts directory on PATH automatically; no global PATH edit is required.

Config loading never creates state or migrates old installations. After an
explicit quiescent `reset --confirm ID`, `init --existing-config` creates fresh
state without overwriting the existing configuration/goal. Reset archives the
old directory, not the ledger, and never starts research.

The shared ledger defaults to `%LOCALAPPDATA%\xgenius\resources.db` on Windows
and the user's local state directory elsewhere. Tests can select an isolated
ledger with `init --ledger PATH` or `XGENIUS_RESOURCE_DB`. Production campaigns
sharing a workstation must coordinate through the same ledger; a new filename
is not permission to ignore work in an older ledger.

Configure capacity explicitly with `machine configure --cpus 2 --memory-mb 4096
--headroom-mb 2048`. Reconfiguration refuses active/uncertain grants.
Reasoning, experiments and maintenance compete fairly for capacity; a one-slot
deployment alternates rather than keeping a permanent reasoning reservation.
The oldest eligible satisfiable request gets a drain-to-fit barrier.

## Configuration

Init records the Python executable that ran it. Use a persistent prepared
environment. Add only the runners needed for the investigation; no fallback is
performed. `doctor --runner NAME --provider` checks the selected runner and
model-free provider help, not all optional runners or a billable canary.

```toml
schema_version = 3

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

[agent]
provider = "copilot"
command = ["copilot", "--allow-all"]
model = ""
reasoning_effort = ""
timeout_seconds = 600
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

## Budgets and control

`max_seconds = 0` is an unlimited campaign admission horizon;
`max_invocations = 0` is unlimited managed campaign invocations. Starter values
are finite. Zero GPU-hours/no devices means no GPU work. Resource, storage and
per-operation limits remain strictly positive.

Elapsed time begins with the first admitted campaign operation, includes
shutdown/pauses, never decreases on clock rollback, and does not reset on
reopen. Expiry gates new admission, not an already admitted operation's deadline.
Changing TOML limits is recorded at the next admission; it does not reopen work.

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

`python`/`python3` select the runner's configured interpreter. Argv preserves
spaces, empty strings, quotes and Unicode. Explicit `source_files` are copied
with an enforced byte cap and hashes; Git identity alone is not a snapshot.
No environments, datasets, caches or dependencies are automatically copied or
installed. Payloads inherit essential PATH/home/temp variables and explicit
non-secret overrides, not the controller's complete credential environment.

Write artifacts under `XGENIUS_OUTPUT_DIR`. A `metrics.json` artifact is an
object of finite numeric values, not booleans or strings. Optional
`input_validators` and `validators` are arrays of argv arrays and share the
operation's resources, deadline and cancellation; include their source files.
Successful execution and invalid output remain distinct facts.

Hypothesis-associated work supplies both `hypothesis_id` and a scientific
`hypothesis_description`, not its own opaque ID. Evaluated statements cannot be
changed. Use a new ID and optional `hypothesis set --supersedes OLD_ID` for a
revised claim. Support/setup/replication work need not invent a hypothesis.

Declared inputs use `XGENIUS_INPUT_NAME`. `declared` does not certify content;
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

`steer --text TEXT` records attributed operator constraints; `--supersedes ID`
explicitly replaces one. `source set --kind goal|protocol --file PATH` versions
small authority. Observed manual edits are retained as imports, with observation
time distinguished from edit time. `journal search --query TEXT` searches a
bounded prefix coverage; `journal entry --id ID` retrieves retained original
text. Zero lexical matches do not establish the absence of evidence.

`compact` and `report` request fixed maintenance at an eligible safe point; they
do not launch a second provider from inside a research turn. An explicit operator
request may run after quiescent closure without reopening research. New control
revisions fence pending maintenance. Automatic compaction is deduplicated by
source revision; it cannot delete authority or repeatedly spend on a
non-shrinking summary. Reports are explicit, not automatic periodic inference.

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
`run`, and uses the optional `docker-build` extra:

```powershell
python -m pip install "xgenius[docker-build]"
xgenius build --runner container --context . --include experiment.py --seconds 600
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

`xgenius dashboard --open-browser` opens **Brief**, the research-first catch-up
view. It shows the retained question, latest accepted rationale, qualified next
step and independent execution/research facts. Recovery blockers are promoted
when present; a closed generation shows its recorded outcome instead of old
instructions as though they were current. Missing interpretation, assessment,
report or action-owner records are stated explicitly.

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

### Dashboard Copilot observer

Ordinary dashboard pages need no SDK. Chat is enabled by default and requires
the optional `dashboard-chat` extra to answer on-demand questions; without it,
pages remain usable and chat shows an explicit dependency message. Opening a
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
```

These are separate from `[agent]`. The positive allowance applies to the current
dashboard process; usage does not spend campaign invocations. Waiting is visible
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
