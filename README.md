# LabGoblin

**An ordinary Copilot researcher, with a small execution harness and optional
stopping review.**

LabGoblin gives one persistent Copilot SDK session a research question, short
instructions and a normal workspace. The researcher writes code, runs experiments,
keeps notes and produces a report. There are no hypothesis-registration forms,
research handoffs, publication transactions, separate reflection agents or
mandatory final-analysis calls.

An optional read-only coach reviews the proposed stopping point. Useful feedback
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
exhaustion of credible feasible avenues, not merely an honest bounded negative
report. Unresolved controls and promising untried approaches deserve scrutiny;
exhausted ideas are not proof of impossibility.
The harness owns resources, cumulative spending, cancellation and retained
execution evidence; it does not decide what constitutes scientific truth.

An optional `labgoblin_consult` tool lets the researcher or coach ask a fresh,
read-only scientific adviser about related work, assumptions or alternative
approaches. It returns a cited brief, not a new plan or experiment. The researcher
still owns the notes and scientific choices; there is no continuous sidecar.
With consultation enabled, the coach requests a targeted **claim review** when
stopping depends on a consequential, insufficiently checked scientific claim.
The adviser challenges assumptions and looks for counterexamples; objections
return to the same researcher. This is not a compulsory reviewer at every stop,
an independent experimental reproduction, or scientific certification.

An on-demand **read-only inspector** provides local browser chat, cited evidence,
images and factual execution/spending status. It never attaches to or steers the
researcher. Browsing is model-free; questions require an explicitly authorized,
separate cumulative inspection allowance.

Python 3.11+, native Windows and native Linux are supported. Copilot CLI must be
installed and authenticated separately. Native execution is trusted, **not a
hostile-code sandbox**.

## Start

Install from this checkout into a persistent environment; these instructions do
not assume a published PyPI release:

```powershell
python -m pip install -e .
labgoblin init .\my-research --goal-file .\question.md --model auto --credits 1000
labgoblin machine configure --cpus 2 --memory-mb 4096 --headroom-mb 2048
labgoblin run .\my-research
```

`init` is deterministic and never calls a model. Without `--goal-file`, edit the
generated `research_goal.md` before running. Existing goal and instruction files
are preserved. Review `labgoblin.toml` and `.github\copilot-instructions.md`;
starting `run` authorizes the configured research execution and its credit pool.
The machine example is a capacity choice, not a recommendation for every host.

The researcher runs alone by default. Add `--coach` during initialization, or set
`coach = true` before a new run, to enable stopping review. The default execution
deadline is 90 minutes, including shutdown and any stopping reviews. A controller
restart does not reset spending or extend already-admitted work.
`max_stalled_reviews = 10` controls the consecutive no-progress ceiling, not
the total number of reviews; set it in TOML or with `init --max-stalled-reviews`.

## Ordinary research, small controls

The researcher works with `research-notes.md`, `report.md`, code and `artifacts\`.
Keep large read-only source datasets in `inputs\` or outside the workspace;
the harness does not duplicate them into result snapshots.

| Command | Meaning |
|---|---|
| `init PROJECT` | Create a fresh project without inference or overwriting existing state. |
| `run PROJECT` | Start the first execution, or adopt its still-running independent supervisor. |
| `run PROJECT --resume` | Explicitly start another conversation from retained files, using the remaining cumulative allowance. |
| `status PROJECT` | Read execution state, loaded configuration and accounting; never reconcile or start work. |
| `inspect PROJECT --open` | Browse research evidence locally, without model calls or research changes. |
| `stop PROJECT` | Request bounded cancellation of exactly this project's owned execution. |
| `reconcile PROJECT` | Retire only proven-dead owned executions; never replay research. |
| `record-usage PROJECT ...` | Append explicitly confirmed final usage or a separately labeled operator estimate. |
| `machine status` | Inspect shared resource capacity and reservations. |

Commands produce JSON and return nonzero on failure or an incomplete run.
Ctrl+C requests cancellation; abrupt controller death instead leaves the
independent worker to finish within its original deadline. A new `run` adopts that
worker rather than starting a duplicate. `--resume` after a settled execution
starts a new provider conversation, not a promised resurrection of an interrupted
SDK session.

## Inspect without steering

```powershell
labgoblin inspect .\my-research --open
# Optional paid questions: choose an installed/enabled model and a separate allowance.
labgoblin inspect .\my-research --open --credits 100 --model MODEL_ID --reasoning-effort medium
```

The first command is view-only, even if chat was authorized previously.
The second authorizes a **cumulative** inspection soft cap, not another 100
credits on each restart. No model runs until you submit a question. Each answer
uses a fresh restricted session, bounded previous chat and freshly read evidence.
It can explain findings and show captured PNG/JPEG images, but cannot run code,
retrieve external literature, edit files or restart research.

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

Consultations are separately opt-in: use `init --consultant` or set
`consultant = true` before a new execution. This authorizes workspace evidence
reads and public literature retrieval, not uploads or arbitrary agent spawning.
Researcher, coach and advisers all share the same project pool. Consultation
headroom is reserved before assigning caller limits: 10% of the remaining
allowance, at least 30 and at most 300 credits (300 for an unlimited project).
The reserve is shared across consultations, not renewed for each call.
Each consultation has at most five minutes, within the original run deadline.
See the operating guide for exact bounds, failure behavior and retained artifacts.

Each execution keeps frozen instructions/configuration/helpers, bounded provider
events and logs, qualified spending records, a terminal receipt and an immutable
workspace snapshot. Snapshots retain regular source/output files, including
multi-gigabyte artifacts, within explicit byte/file-count limits. Excluded inputs,
environments, common credential paths (including `.env*`), state, caches, links
and special files are listed. These exclusions are not a general secret scanner.
Working files remain editable; this is not a database of every scientific interpretation.

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
report/compaction/final-analysis jobs are retired from this runtime. There is no
second legacy orchestrator hidden behind a simple-mode flag. No automatic image
pulls, environment installations, remote execution, uploads or unrestricted
nested model sessions are part of the new loop. Only the explicitly enabled,
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
