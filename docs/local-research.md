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
Independent Windows supervisors request detached process groups and permitted
Job Object breakaway. A parent policy that prohibits breakaway is a launch error,
not permission to silently weaken crash-survival behavior.
Linux/WSL uses process groups, affinity and memory monitoring, not a kernel-hard
memory quota. `hard_memory_limit=true` is rejected for monitored runners. Native
and WSL trusted processes are not a hostile-code boundary: deliberately detached
Linux descendants or unrestricted host tools require stronger isolation.

The campaign's elapsed limit starts on its first run and includes paused time.
It stops new admission rather than killing admitted experiments. Each attempt
has a separate hard walltime. GPU budgets reserve requested maximum duration
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
bounds. Waiting with no work blocks; completion cancels undispatched work and
drains admitted work. Events arriving during a turn remain distinct.

Independent worker/agent supervisors persist identity and completion receipts.
Controller termination does not cancel experiments. On resume, receipt ingestion
is idempotent; PID reuse does not establish ownership. Engine/distro loss, missing
launch handles or unprovable liveness become `recovery_required`, retain capacity,
and block the campaign. Inspect the exact attempt directory, supervisor logs,
backend handle and original engine/distro. Restore access and reconcile. There is
intentionally no force-release-on-stale-heartbeat command; do not delete the ledger
or reset state to bypass an unresolved reservation.

Local schema upgrades are transactional and backed up. Historical legacy records
are not replayed as new local events. New SLURM jobs have cluster-qualified tracker
IDs; bare scheduler IDs are rejected when ambiguous.

The loopback dashboard shows campaign/turn states, shared reservations and
downloadable registered artifacts. `--json` commands keep provider chatter in
per-turn logs. `blocked` run results return a nonzero exit code.

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

```powershell
python -m pytest tests -q
$env:XGENIUS_INTEGRATION = "1"
python -m pytest tests\test_local.py -q
```

The opt-in suite requires prepared Ubuntu WSL2 and local Docker Desktop with
`python:3.11-slim`; it never pulls images. No real cluster or dataset is needed
for automated coverage. CUDA execution requires a separately prepared environment
and an available GPU; successful CPU execution or device enumeration is not a
CUDA validation.
