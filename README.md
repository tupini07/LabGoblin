# xgenius

**Local autonomous research with Claude Code or GitHub Copilot CLI.**

xgenius runs a long investigation as fresh, bounded research sessions and
independently supervised experiments. It preserves why decisions were made,
shares CPU/RAM/GPU admission across campaigns, recovers owned work after
controller exit, and distinguishes successful execution from assessed research.
Native Windows, an explicitly prepared WSL2 distro, and local Linux Docker are
supported. No backend or provider is silently substituted.

**Version 2 is a clean break.** Configuration, campaign state, the machine ledger
and worker protocol use schema 3. Old formats are rejected, not migrated.
Cluster/SSH/SBATCH/Singularity operations are removed. Never replace an old
machine ledger while its workers might still be running; establish quiescence
with the old installation before setting up a fresh runtime.

## Start a local investigation

Use Python 3.11+ in a persistent environment. Install and authenticate the
standalone `copilot` CLI or Claude Code separately.

```powershell
python -m pip install .
New-Item -ItemType Directory my-research
Set-Location my-research
xgenius init --agent copilot
```

Plain `init` selects Claude. Edit `research_goal.md` to define the question,
evaluation protocol, permitted data and environments, stopping criterion, and
what negative or inconclusive findings would mean. This is an autonomous
investigation, not an instruction to stop after writing a proposal.

Review `xgenius.toml`, then configure the shared machine envelope explicitly.
This example permits two managed CPUs and 4 GiB of reserved RAM, plus 2 GiB of
unallocated headroom; it is not a recommendation for every workstation:

```powershell
xgenius machine configure --cpus 2 --memory-mb 4096 --headroom-mb 2048
xgenius doctor --provider --json
xgenius run
```

The envelope includes research and maintenance reasoning, not just experiments.
The generated agent allowance is a starter estimate, not a measured provider
requirement. Existing incompatible ledgers block admission rather than hiding
older reservations behind a new file.

The starter campaign limits are **one hour and ten managed provider invocations**.
For a long investigation, review them deliberately. `max_seconds = 0` and
`max_invocations = 0` mean unlimited admission time and unlimited managed
invocations respectively; no other zero means unlimited. Every operation still
has a finite deadline, and an open generation reserves its final analysis.
Managed invocations are not API-call, token, or spending caps.

See the [operating guide](docs/local-research.md) for complete configuration,
budgets, provenance, Docker builds, and recovery. The
[synthetic example](examples/local-synthetic/README.md) runs without a dataset,
GPU, or model call when used with `--no-agent`.

## Control and observe

| Command | Meaning |
|---|---|
| `run` | Recover compatible owned work, then continue eligible research. |
| `run --no-agent` | Run/recover submitted experiments without inference. |
| `pause` / `resume` | Gate new admission / resume an open generation. Armed work can finish. |
| `stop` | Stop admission, retire queued work, drain admitted work, retain a partial handoff. No final model call. |
| `cancel --id ID` | Request cancellation of exactly one owned attempt. |
| `reopen` | Start a new generation after quiescent closure/stop; cumulative budgets remain. |
| `status` / `budget` | Read only: no reconciliation, inference, or state initialization. |
| `reconcile --state-dir PATH` | Recover supported state even when current TOML is broken or missing. |
| `steer --text TEXT` | Record an attributed operator constraint, not an anonymous journal edit. |
| `report --no-agent` | Publish an immutable deterministic HTML/Markdown inventory without inference. |
| `report` / `compact` | Request fixed, resource-admitted safe-point maintenance. |
| `storage inventory` | Inspect bounded owned-file sizes and retained references. |
| `storage retention --dry-run` | Review proven-unowned preparation candidates; never delete. |
| `reset --confirm CAMPAIGN_ID` | Archive supported quiescent state; never erase the machine ledger or auto-restart. |

Commands support `--json` and return nonzero on failure. `--project PATH` selects
the project explicitly. Control requests accept stable `--request-id` and
`--expected-revision` values for retry-safe scripts.

The controller is not the owner of running payload trees. Its exit does not
cancel already admitted work; their independent supervisors retain deadlines
and completion receipts. Unknown ownership keeps its resource reservation.
Do not manually delete a grant, invent a receipt, or retry uncertain work.

## Evidence and research closure

An experiment manifest has an idempotency key, argv array, explicit source files,
resources, a finite deadline, and relative output artifacts. Small metric
documents are captured once: parsing, hashing, historical reports and downloads
use those same bytes. A mutable large file is not advertised as an exact
historical download. Scientific statements become immutable when first admitted;
a changed claim requires a new hypothesis ID.

Each research turn receives a versioned packet and returns one owned delta
handoff. Exact evidence acknowledgements, decisions, and the journal projection
commit together. Operator constraints remain outside model-written summaries,
and historical source revisions survive compaction.

Finalization seals the complete attempt inventory, including failed, cancelled,
unperformed and still-running admitted work. At most one additional analysis
assesses the sealed evidence, including late replications. Missing allowance,
failed analysis, or a request for more work produces an honest incomplete outcome,
not an automatic paid loop. The harness checks reference/value integrity and
explicit coverage; it does **not** certify scientific truth, novelty or adequacy.

## Research dashboard

```powershell
xgenius dashboard --open-browser
```

The read-only loopback dashboard defaults to `http://127.0.0.1:8765`. It shows
operator intent, research progression, recovery blockers, budgets, experiments,
hypothesis statements, exact evidence and historical sources. Journal entries
are folded and paginated with stable revision links and bounded search coverage.
Markdown disables raw HTML and images; assets are packaged locally.
Opening or refreshing a page never reconciles or acknowledges research.

### Dashboard Copilot observer

```powershell
python -m pip install "xgenius[dashboard-chat]"
xgenius dashboard --chat
```

From a checkout, use `python -m pip install ".[dashboard-chat]"`.
**Ask Copilot** starts inference only when you send a question. The observer gets
curated, bounded, read-only tools for recorded state, metrics, historical rationale
and exact source revisions. It cannot steer research, attach to its provider
session, run commands, or read arbitrary files, logs, datasets or artifact bodies.

Chat, drafts, open/closed state and maximized layout survive navigation/reload
while the dashboard process remains alive. **Close** hides it; **New chat** clears
it; **Maximize/Restore** changes the reading layout. Cancellation and visible
machine-capacity waiting are supported. Chat is not persisted across server
restarts. Observer invocation allowance and usage are separate from research,
but its owned SDK processes still acquire machine capacity.

## Boundaries

Trusted native execution is not a hostile-code sandbox. CPU placement is not a
CPU-time quota; WSL memory monitoring is not a kernel-hard memory limit. The
ledger coordinates cooperating xgenius consumers, not every process or OS user.
Explicit free-space monitoring is a soft safeguard, not a filesystem quota.
Optional Copilot sandboxing fails closed when its prepared policy is unavailable.

No automatic image pulls, remote compute, pushes, uploads or shared-environment
installations are part of the research loop. A trusted provider can still use
its own tools: define approved data disclosure and operational constraints.
Do not put credentials in persisted manifests or environment overrides.

## Development and historical work

Install with `python -m pip install -e .`, then run `python -m pytest tests -q`.
See [contributor runtime contracts](docs/runtime-protocol.md) and `CLAUDE.md`.
Guest/browser/build acceptance requires explicit opt-in and already prepared
environments. Ordinary tests never invoke a real research model.

The illustrated [Atari](examples/auto-cleanrl-report/report.html) and
[Craftax](examples/auto-craftax-report/report.html) reports are **historical v1
artifacts**, not supported v2 onboarding. Their original cluster methods,
figures and conclusions are preserved.

## Attribution and citation

This local-runtime edition builds on Roger Creus Castanyer's xgenius.
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
