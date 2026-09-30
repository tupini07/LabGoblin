# Local research campaigns

## Operating model

Run the controller on Windows. Each project owns a SQLite database, journal,
queue, and agent budget. Independent native supervisors run experiments locally,
in an explicitly selected WSL distro, or in Linux Docker containers. All three
share one per-user reservation ledger: WSL and Docker are not additional machines.
There is no automatic backend fallback or remote staging.

The legacy SLURM adapter remains available through legacy configuration and
`init --backend slurm`. A local configuration does not permit remote runners.
Old configuration files are never rewritten on load. `init --force` is an explicit
replacement, preserves the original TOML in a uniquely named backup, and refuses
active or unresolved local work. It is not an automatic configuration translator.

`init` defaults to local execution and Claude; add `--agent copilot` to select
Copilot. Both CLIs read the managed section of `CLAUDE.md`. User-written sections
are preserved. New configuration uses an argv array in `[agent].command`;
legacy configuration retains `[watcher].trigger_command`. Do not configure both.

## Configuration

The generated native runner records the interpreter used during initialization.
Run it from a persistent installation, not an environment you intend to delete.
An explicitly referenced interpreter/environment must already be prepared. Workers
never install or synchronize dependencies and set `PYTHONDONTWRITEBYTECODE=1`.

```toml
schema_version = 2

[project]
name = "local-study"
research_goal = "research_goal.md"

[execution]
default_runner = "native"
source_files = ["experiment.py"]

[runners.native]
kind = "local"
python = 'D:\research-env\Scripts\python.exe'

[runners.ubuntu]
kind = "wsl"
distro = "Ubuntu"
python = "/usr/bin/python3"

[runners.container]
kind = "docker"
context = "desktop-linux"
image = "python:3.11-slim"
python = "python"
network = false

[campaign]
cpus = 2
memory_mb = 2048
gpus = []
max_jobs = 1
max_gpu_hours = 0
max_seconds = 3600

[agent]
command = ["copilot", "--allow-all"]
max_turns = 10
timeout_seconds = 600
retries = 1
sandbox = false

[inputs.observations]
path = 'D:\approved-data\observations.json'
identity = "observations-v1"
prompt_access = false
# sha256 = "an optional known SHA256 for a bounded file"
# wsl_path = "/explicit/guest/mapping/observations.json"
```

Only configure runners that are actually prepared; `doctor` checks every named
runner and input. WSL uses a copied standard-library helper, not a Linux install
of xgenius or a guest connection to the host SQLite database. Guest access paths
are checked explicitly; junction resolution does not rewrite original identities.
Windows-hosted scratch is the default. Do not point campaign state at a shared
producer tree or move/junction `.xgenius` outside the campaign.

Docker requires an existing approved image and a local engine endpoint.
Submission never pulls an image. Image IDs, container IDs and ownership labels
are recorded. Code and declared inputs are read-only mounts. Containers have
CPU/RAM/device limits, no restart policy, and no network unless explicitly enabled.
Finished containers remain available for inspection; remove only their recorded
IDs after reviewing the durable outputs.

`xgenius build --runner container` explicitly authorizes a local Docker build,
without a registry push or SIF conversion. Review the Dockerfile and `.dockerignore`
before invoking it: Docker builds send the selected project build context to the
local engine, and missing base images can require acquisition. Prepare base images
separately when offline operation is required. Test images through queued jobs.

## Experiments

```json
{
  "key": "baseline-001",
  "experiment_id": "baseline",
  "hypothesis_id": "h001",
  "hypothesis_description": "Relational constraints reduce invalid arrangements without lowering coverage.",
  "runner": "native",
  "argv": ["python", "experiment.py", "--label", "with spaces"],
  "source_files": ["experiment.py"],
  "cpus": 1,
  "memory_mb": 512,
  "gpus": [],
  "seconds": 60,
  "artifacts": ["metrics.json", "plot.png"],
  "input_validators": [["python", "check_inputs.py"]],
  "validators": [["python", "check_outputs.py"]]
}
```

Include validator scripts in `source_files`, or omit the optional validator
arrays. Input validators run before the workload; output validators run after it.
All share the attempt's deadline, resources, input references, and cancellation.

Submit with `xgenius submit --spec experiment.json --json`; `--runner NAME` can
select the runner if it does not conflict with the manifest. Arrays preserve empty
arguments, quoting, Unicode and native Windows paths. There is no implicit shell.
Use runner-relative paths inside argv; only declared input/workspace paths are
translated. `python`/`python3` select the runner's configured interpreter.

Record the claim being tested with `hypothesis_description`, not only a shorthand
`hypothesis_id`. The description is optional for compatibility, but when supplied
it must be nonempty, have an ID, and not repeat an autogenerated placeholder.
Submission creates the hypothesis or fills its missing/placeholder description
without replacing motivation, status, or conclusions. Reusing an ID with a
different statement is rejected atomically; intentionally revise an existing
statement with `xgenius db hypothesis-update --id h001 --description "The revised claim"`.
Old manifests still work, but a newly created hypothesis without a description
is explicitly undescribed rather than treating its ID as its scientific meaning.

Use a stable idempotency key for retries of the same request. Changing a request
under an existing key is an error; a deliberate new attempt needs a new key.
Only explicitly selected files are copied, including selected untracked files.
Git revisions alone are not snapshots. Do not include secrets, caches, large
datasets or shared environments. Snapshots record file hashes, selected runner and
interpreter, input identities, resource requests, environment overrides and argv.
Dependencies remain the responsibility of the explicitly prepared environment.

Payloads receive PATH/home/temp essentials and explicit string environment
overrides, not the controller's complete credential environment. Do not put secrets
in environment overrides: non-secret overrides are persisted in manifests.
`XGENIUS_INPUT_OBSERVATIONS` identifies the example input; `XGENIUS_OUTPUT_DIR`
is the only published output root. Inputs are not automatically included in model
prompts. `prompt_access` documents the approved disclosure policy; it cannot
constrain a trusted agent's own tools.

Artifacts are contained relative paths. Collection checks required files, hashes
and optional `metrics.json` (a finite numeric object, not booleans or strings).
A successful process and invalid output remain distinct: status can be completed
with a validation-failure event. Scientific acceptance is never inferred.

## Capacity and budgets

Run `machine configure` once with an explicit CPU/RAM/headroom/GPU envelope.
The ledger is `%LOCALAPPDATA%\xgenius\resources.db`; `XGENIUS_RESOURCE_DB` is an
explicit isolation override for tests. Campaigns must use the same ledger to
coordinate. It is not a multi-user scheduler or protection against external apps.

GPU requests use physical `GPU-...` UUIDs, not runner-local ordinals. Whole
devices are reserved. Admission conservatively waits while `nvidia-smi` reports
external compute processes; it never evicts them. Host available RAM is rechecked
with configured headroom and outstanding reservations. Oldest eligible fitting
requests are preferred; requests outside capacity are rejected.

Windows payloads use Job Objects and CPU affinity. Docker sets CPU/RAM limits.
Windows supervisors, payloads, agent sessions, and background probes use windowless
consoles, so their ordinary child processes do not open terminals or steal focus.
Logs still go to their existing files or captured CLI output. Independent
supervisors retain separate process groups and permitted Job Object breakaway.
A parent policy that prohibits breakaway is a launch error, not permission to
silently weaken crash-survival behavior. This does not prevent an experiment from
explicitly opening its own GUI or requesting a new console.
Linux/WSL uses process groups, affinity and memory monitoring, not a kernel-hard
memory quota. `hard_memory_limit=true` is rejected for monitored runners. Native
and WSL trusted processes are not a hostile-code boundary: deliberately detached
Linux descendants or unrestricted host tools require stronger isolation.

The campaign's elapsed limit starts on its first run and includes paused time.
It stops new admission rather than killing admitted experiments. Each attempt
has a separate supervised walltime. GPU budgets reserve requested maximum duration
and account for terminal outcomes, including failures and cancellation. Running
usage is an estimate; unknown/lost execution retains conservative reservations.
Agent research, report and compact sessions count toward `max_turns` and have
independently supervised deadlines. Provider usage is explicitly unknown (`null`);
no token-to-dollar conversion or hard financial/subagent cap is claimed.

## Lifecycle, evidence and recovery

| Command | Behavior |
|---|---|
| `run` | Preflight, reconcile, initial research turn, dispatch, completion turns |
| `run --no-agent` | Run only already queued experiments |
| `run --once --no-agent` | Dispatch one cycle; independent workers continue |
| `pause` | No new dispatch/turns; current jobs and turn finish |
| `stop` | Cancel queued work, drain running work, retain unanalyzed events |
| `cancel --job-ids ID` | Cancel only the selected owned attempts |
| `resume` | Reconcile the same attempts and reconnect, never resubmit |
| `steer "directive"` | Append a journal directive and durable event |
| `results attempts --json` | Registered outcomes and artifact summaries |
| `results export` | Atomic `results/attempts.csv` projection; leaves manual CSVs intact |
| `report` | New historical `reports/ID` snapshot, requiring Markdown and HTML |
| `compact` | Serialized journal compaction with original backup |
| `reset` | Refuse active/unresolved work; archive old state under `.xgenius-archives` |

Each turn acknowledges only its assigned event IDs, references an updated
`.xgenius/journal.md`, and returns continue/wait/blocked/complete with a reason.
Invalid output leaves events unacknowledged and retries only within configured
bounds. Waiting with no work or pending events blocks. A wait decision still
triggers another turn when completions arrived during the previous turn, even
if the last job has already finished. Explicit completion cancels undispatched
work and drains admitted work; unacknowledged events remain recorded.

Independent worker/agent supervisors persist identity and completion receipts.
Controller termination does not cancel experiments. On resume, receipt ingestion
is idempotent; PID reuse does not establish ownership. Engine/distro loss, missing
launch handles or unprovable liveness become `recovery_required`, retain capacity,
and block the campaign. Inspect the exact attempt directory, supervisor logs,
backend handle and original engine/distro. Restore access and reconcile. There is
intentionally no force-release-on-stale-heartbeat command; do not delete the ledger
or reset state to bypass an unresolved reservation.

Linux/WSL recovery checks the recorded workload process session and validator
sessions, not only the guest supervisor PID. An orphaned live session keeps its
reservation; missing launch identities remain unresolved rather than being
assumed dead. If the guest supervisor itself is lost, its deadline/cancellation
monitor is also lost. `cancel` records the request but reports that recovery is
required; inspect and stop the identified owned workload before reconciling.
Controller loss alone does not have this limitation: the supervisor continues.

Local schema upgrades are transactional and backed up. Historical legacy records
are not replayed as new local events. New SLURM jobs have cluster-qualified tracker
IDs; bare scheduler IDs are rejected when ambiguous.

The loopback dashboard (`xgenius dashboard`, default port 8765) is read-only.
Its overview distinguishes campaign state/stop reason, in-flight experiments,
validation/recovery failures, elapsed/turn limits, and the latest agent decision.
Search and paginate experiments/artifacts; inspect individual attempts, source
hashes, numeric metrics and captured logs. Agent activity separates turn decisions
from pending/acknowledged events. Resource views show the shared reservation
ledger, not measured desktop utilization or a guarantee that stored handles live.
Displayed limits come from the current config file; a running controller may
have loaded earlier values.

Hypothesis lists lead with the recorded statement and show the stable ID
secondarily. Details render the full statement, motivation, expected outcome,
conclusion, and notes when recorded. ID-only or automatically generated
submission descriptions are labelled **Hypothesis statement not recorded**.
Related journal excerpts match the exact hypothesis, attempt, or experiment
identifiers; they are labelled context, not invented definitions. This lookup
searches the latest 128 KiB using up to 200 recent experiments and shows at most
six excerpts of 3,000 characters each, with truncation notices. Dashboard reads
never backfill or otherwise edit research records.

Journal, goal and debug documents render locally as Markdown (raw HTML escaped).
The journal groups the append format's timestamps into collapsible entries, not
every nested heading. It shows the newest 20 entries first, with the first entry
open; older/newer paging and case-insensitive search cover the whole current
journal, including entries beyond the former document-tail limit. A compact sticky
toolbar provides **Jump to latest**, **Expand page**, and **Collapse page**.
Each entry has its timestamp, headline, source view, older/newer entry links, and
a bookmark link that remains on the same entry as new entries are appended.
Compaction or replacement can invalidate those links; historical backup files
are not part of this reader.

The journal is indexed without retaining all entry bodies in memory, and only
one page is rendered. Individual entry previews are limited to 16 KiB, with a
larger 128 KiB view when opening an entry link. Oversized entries have explicit
notices rather than silently losing content. Untimestamped/compacted Markdown
is retained as a notes entry with a bounded 128 KiB tail. Source changes during
a read produce a visible refresh error rather than a mixed snapshot. Manual
refresh preserves existing open/closed entries and the visible reading position;
the journal never auto-refreshes or closes the Copilot sidebar.

Goal/debug documents over 128 KiB are explicitly truncated, using the beginning
of the goal and the latest portion of the debug log; log views show at most the
latest 64 KiB per stream. Full files remain on disk.
Registered artifacts download as attachments, never active
HTML. The dashboard does not serve arbitrary workspace files or load CDN assets
or remote Markdown images. Optional 15-second refresh updates operational pages;
document/detail views refresh on request so reading is not interrupted.

Dashboard reads never reconcile jobs, release reservations or acknowledge
events. Keep lifecycle control in the CLI. `--json` commands keep provider
chatter in per-turn logs. `blocked` run results return a nonzero exit code.

### Dashboard Copilot observer

The optional `dashboard-chat` extra installs the supported Python Copilot SDK.
It requires a separately installed Copilot CLI and your existing login; xgenius
does not download a runtime, copy credentials, or change global Copilot settings.
Enable chat with `xgenius dashboard --chat`, or add this to `xgenius.toml`:

```toml
[dashboard.chat]
enabled = true
model = "auto"
reasoning_effort = ""
timeout_seconds = 120
cli_path = ""
```

An empty `cli_path` searches PATH for `copilot`. `model` and `reasoning_effort`
are independent of the research agent's command. Empty effort uses the runtime
default; supported effort levels depend on the selected model. Invalid or
unavailable provider settings produce an error, not a different-model fallback.
The deadline must be between 5 and 600 seconds; settings are loaded when the
dashboard starts. Missing SDK/runtime prerequisites leave ordinary dashboard
pages usable and show an actionable chat-unavailable message.

For an already-running campaign, use a separate dashboard environment rather
than installing packages into its controller or experiment environment. For
example, from the campaign directory, with `uv` and a local xgenius checkout:

```powershell
uv run --no-project --python 3.11 --with-editable "D:\path\to\xgenius[dashboard-chat]" xgenius dashboard --chat --port 8766
```

Replace the checkout path; choose an unused port. This starts only a dashboard,
not a controller or experiment. An existing dashboard must be restarted to load
upgraded code; the research controller does not need restarting.

The observer creates a fresh restricted SDK session for each question, supplying
bounded recent chat history and fresh recorded evidence. It does **not** attach
to, message, or share instructions with the autonomous research session. Its only
tools read campaign summaries, recent experiments, a specific experiment's
registered numeric metrics, recent agent decisions/events, and the journal or
research goal (up to 16,000 bytes each). Shell, arbitrary file/SQL access,
skills, MCPs, extensions, subagents, steering, submissions, cancellation of
experiments, and campaign edits are unavailable. Managed Copilot settings remain
enabled. Recorded handles are not proof that a process is still alive.

**Privacy and usage:** sending a question shares the question, recent chat, and
requested evidence with your configured Copilot service. Local research does
not mean offline inference. Raw logs, execution environments, dataset inputs,
private targets, and artifact file bodies are not available through these tools.
However, journal/goal text, names, reasons, and metrics can themselves contain
sensitive material; curate those records before enabling chat. This is an
explicit evidence allowlist, not content redaction.

Opening or refreshing the dashboard never starts inference. Only one answer
runs at a time per dashboard server, with at most 12 evidence-tool calls and
64,000 answer characters. Conversations allow 20 questions, retain at most six
successful exchanges/24,000 characters as model history; at most eight
conversations are retained. Capacity limits reject new work rather than
automatically discarding existing conversations.
**Cancel answer** aborts the observer only. Timeouts and limits retain partial
text with a failure/cancellation notice, never as a successful answer. Requests
are not automatically retried; retrying an uncertain submission in the same
page reuses its ID to avoid duplicate inference.

Chat history is held in dashboard memory, not the research DB/journal. A tab
stores an opaque conversation handle, open/closed state, and maximized/sidebar
size preference in session storage. Navigation and page reloads restore the same
conversation, including an answer in progress, and leave the sidebar as you chose.
**Maximize** fills the browser window, with larger text and a centered reading
column rather than excessively long lines on wide monitors. **Restore** or Escape
returns to the sidebar; neither clears the conversation, interrupts an answer,
nor discards the current draft. Resizing keeps your place in the visible paragraph
where possible, or stays at the bottom if you were following the latest answer.
The maximized view keeps keyboard focus inside chat and prevents scrolling or
interacting with the covered dashboard. The conversation area itself can receive
keyboard focus for scrolling. Restoring or closing makes the dashboard accessible
again. Closing/reopening and **New chat** also retain your size preference.
Only **Close** hides chat; Escape never closes it. Closing the sidebar does not clear
history or cancel an answer. **New chat** explicitly discards that conversation
without closing the sidebar; idle time does not clear it. Restarting the dashboard
still loses all chats. Normal completion/cancellation closes the owned runtime and
deletes its SDK session. A hard crash may leave SDK session records behind.
Token usage is provider-reported, separate from research-turn budgets, and
**not a monetary cap**. Multiple dashboard processes have independent limits.

## Optional Copilot sandbox

Trusted mode is the default. It does not disable existing managed Copilot policy.
For sandbox mode, separately provision/authenticate a dedicated `COPILOT_HOME`
inside this campaign's `.xgenius`, set `agent.copilot_home`, and set
`agent.sandbox=true`. Never copy authentication files or change global settings.
Its `settings.json` must explicitly set `sandbox.enabled=true` and
`sandbox.allowBypass=false`.

Preflight temporarily adds an exact denied canary and allowed scratch grant,
runs a pre-created shell probe, checks its challenge receipt, and restores the
profile's original settings bytes. Configured WSL/Docker IPC is also exercised.
An exclusive profile-local lock prevents overlapping preflights; inspect a stale
lock's recorded owner before removing it after an interrupted preflight.
Failure refuses startup; there is no unsandboxed fallback. Windows requires the
BaseContainer/PowerShell host capabilities documented by the installed CLI;
Linux has additional namespace/network prerequisites.

This is shell-policy preflight, not proof of complete isolation. Built-in file
edits are best-effort; remote MCPs and harness workers are outside that boundary.
Network restrictions must be reviewed in the provisioned profile; the harness
does not install proxy certificates or authorize network/credential bypasses.

## Validation

Dashboard HTTP/observer coverage uses fake inference without agents or workers:
`python -m pytest tests\test_dashboard.py tests\test_dashboard_chat.py -q`.
SDK policy cases require the optional extra. The optional desktop/mobile
browser case requires Playwright and a prepared Chromium installation. Set
`XGENIUS_BROWSER_TESTS=1` to include it; `XGENIUS_BROWSER_EXECUTABLE` can select an
existing Chromium executable instead of Playwright's default. It checks navigation,
Markdown, refresh/error behavior and downloads, and saves synthetic screenshots
under its pytest temporary directory. It never installs a browser automatically.
Observer browser coverage includes streaming, navigation continuity, maximized
reading/keyboard behavior, cancellation, and idempotent retry after a lost response.
Set `XGENIUS_LIVE_CHAT=1` to include
the real, billable SDK case against synthetic evidence; it verifies evidence-tool
use, a known metric, unchanged campaign state, and owned runtime/session cleanup.

```powershell
python -m pytest tests -q
$env:XGENIUS_INTEGRATION = "1"
python -m pytest tests\test_local.py -q
$env:XGENIUS_SYSTEM_E2E = "1"
python -m pytest tests\test_system.py -q -k "not live_copilot"
$env:XGENIUS_LIVE_AGENT = "1"
python -m pytest tests\test_system.py -q -k "live_copilot"
```

The opt-in suite requires prepared Ubuntu WSL2 and local Docker Desktop with
`python:3.11-slim`; it never pulls images. No real cluster or dataset is needed
for automated coverage. CUDA execution requires a separately prepared environment
and an available GPU; successful CPU execution or device enumeration is not a
CUDA validation.

Full-system tests exercise the actual CLI, shared reservations, controller and
backend faults, artifacts, dashboard downloads, partial batch errors, cancellation,
and a tiny local Docker build using that existing base image. They clean up only
their owned processes/containers/test images and retain logs under pytest's
temporary directory. Live-agent tests additionally require authenticated Copilot
and consume provider usage for research, report, and compaction turns. They use
only synthetic inputs; no real research data or remote compute is involved.
