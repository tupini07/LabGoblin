# Operating the simplified researcher

## Prepare explicitly

Use Python 3.11+ in a persistent environment, an installed authenticated Copilot
CLI, and a separate research workspace. Install LabGoblin from the checkout with
`python -m pip install -e .`. The pinned Copilot SDK is a core dependency; the
harness never downloads the CLI, installs experiment dependencies or chooses a
different backend. Windows also uses pywin32 for owned Job Objects.

```powershell
labgoblin init .\research --goal-file .\question.md --model auto --credits 1000
labgoblin machine configure --cpus 2 --memory-mb 4096 --headroom-mb 2048
labgoblin run .\research
```

Initialization makes `labgoblin.toml`, a goal if absent, short instructions if
absent, and `.labgoblin` state. It preserves existing goal/instruction content and
refuses existing configuration/state. An incomplete initialization is not a valid
project. No model is called during initialization.

Machine configuration is a separate explicit action. It cannot replace or
reconfigure capacity while a grant is held or uncertain. Use the same machine
ledger for cooperating consumers; pointing a project at a second ledger is not a
way to bypass existing reservations.

## Configuration

The complete small configuration is flat TOML:

```toml
version = 5
model = "auto"
reasoning_effort = "medium"
max_ai_credits = 1000
timeout_seconds = 5400
cpus = 2
memory_mb = 4096
gpus = []
coach = false
consultant = false
ledger = ""
max_snapshot_bytes = 68719476736
```

Coaching is opt-in (`init --coach` or `coach = true`). `model` and
`reasoning_effort` are passed to Copilot. Choose an explicit model for
matched evaluations. The SDK transport and ordinary researcher tools remain the
same with coaching on or off. Skills, extensions, MCP servers, automatic custom
instruction discovery, memory, scheduled prompts and unrestricted nested agents are disabled;
the reviewed project instructions are supplied explicitly.

`max_ai_credits` is cumulative across all executions of this project. Use at least
30 credits without coaching or at least 60 with coaching. `"unlimited"` explicitly
removes the cap; zero does not. The cap is post-response and therefore soft.
There are no separate coach, task or report budgets within research. The independent
inspector has its own explicit allowance; research configuration does not enable it.
Consultations are also opt-in (`init --consultant` or `consultant = true`).
They require at least 60 remaining credits without coaching or 90 with coaching.
Omitting the new optional flag in a format-5 configuration keeps it disabled;
already-frozen configurations and receipts are not rewritten.

`timeout_seconds` is the finite deadline for the entire owned execution, including
the persistent researcher, optional reviews, consultations, continuation and shutdown. It must
be at least 60 seconds. The final 30 seconds are reserved for bounded settlement.
Normal progress and output flooding never renew the deadline.

`max_stalled_reviews` defaults to 10 and must be a positive integer. It counts
consecutive follow-up stopping reviews without meaningful progress, not all
reviews in the project. The initial review establishes the baseline. The coach
compares each follow-up with the previous review and cites inspected evidence;
meaningful progress resets only this counter. Report polish, renamed questions
and inconsequential artifacts do not qualify. This is a fallible review judgment,
not a machine-certified progress metric. `init --max-stalled-reviews` sets it for
a new project; editing TOML takes effect only on a new execution.

CPU placement and RAM admission cover the owned provider/experiment process
tree. On Windows, an assigned Job Object enforces CPU affinity and job RAM before
the suspended payload resumes. On Linux, affinity and monitored process-session
RSS are used; RSS monitoring is not a kernel-hard memory quota. Linux requires
pidfd support for identity-safe shutdown. CUDA-visible GPU IDs must be explicitly
configured, visible and unoccupied at admission. This coordinates cooperating
consumers, not every process or user on the machine.

`ledger = ""` selects the normal local machine ledger, or the explicit
`LABGOBLIN_RESOURCE_DB` override. The first run records its exact path and
identity. Subsequent runs refuse a different ledger; accounting cannot be reset
by changing this setting.

`max_snapshot_bytes` defaults to 64 GiB. The snapshot also has a 100,000-entry
bound and requires 512 MiB of free-space headroom per copied file. These are soft
storage safeguards, not a filesystem quota.

**Every configuration edit requires a new execution.** A running worker consumes
its frozen snapshot, not current TOML. `status` reports the loaded budget and
separately displays candidate configuration or a candidate parse error.

## Research files

The goal and short charter are supplied once to an ordinary, persistent SDK
conversation. Use normal code, foreground experiments and artifacts; no research
publication CLI exists. The researcher is asked to maintain `research-notes.md`
and `report.md`, retain failures and negative evidence, and put important results
in `artifacts`.

The notes also retain promising untried ideas and reasons remaining leads cannot
usefully proceed. Stopping requires the original goal's evidence requirements
to be met or credible feasible avenues to be exhausted for supported reasons;
an honest bounded negative report alone is not enough. Exhausted avenues remain
unresolved, and hard spending, deadline and permission limits still take precedence.

With coaching enabled, initial acceptance is provisional. The first
researcher follow-up supplies either the coach's concrete question or a neutral
closing challenge grounded in the original goal, failed controls and strongest
remaining lead. The researcher may investigate or justify stopping without
inventing extra work; the next coach reviews the updated answer and evidence.
Later reviews can accept or request further useful work in the same conversation.
There is no additional neutral challenge, even after a progress-counter reset.
A genuine operator
dependency stops immediately, and insufficient time or credits preserves an
incomplete execution rather than turning provisional approval into completion.
Neither a partial checkpoint near the deadline nor a completed execution proves
scientific exhaustion. No automatic new execution is started.

Reaching the stalled-review ceiling yields `checkpointed`. A coach may request
that outcome earlier for clear repetition after at least two consecutive
no-progress follow-ups, or during the final 60 seconds before the absolute
deadline, including the 30-second shutdown reserve. The host validates these
boundaries; it does not infer an expired deadline from researcher prose.
Checkpoint reasons and the remaining uncertainty are retained. The ceiling is
not a requirement to perform ten reviews or spend unused credits.

New executions always include the installed package's current research charter;
different project instructions are appended as project-specific guidance.
Initialization and package upgrades do not overwrite existing instruction files.
Review the goal and project instructions when adopting changed guidance, preserving
human-authored constraints and old execution snapshots. Already-admitted work
keeps its frozen instructions and coach code.

Large source datasets belong in read-only `inputs` or outside the workspace.
They are intentionally not copied into every result snapshot. Pin important input
identities and reproducible commands in the scientific report. The harness does
not certify dataset provenance or reconstruct overwritten intermediate results.

The final workspace snapshot excludes `.git`, `.labgoblin`, its lock,
environments, Python/pytest caches, `node_modules`, `inputs`, common credential
paths (such as `.ssh`, `.aws`, `.copilot` and `.env*`), links and special files.
Name exclusions are case-insensitive; they are not a general secret scanner.
The manifest explicitly lists exclusions and hashes every retained regular
file. A growing/changed file, exhausted limit or copy failure is an explicit
retention error, not a truncated success. Immutable execution snapshots coexist
with ordinary editable working files.

## Run, stop and continue

```powershell
labgoblin status .\research
labgoblin stop .\research
labgoblin reconcile .\research
labgoblin run .\research --resume
```

`run` blocks until the execution settles. Its worker is independently supervised;
abrupt controller death does not kill admitted research or extend its deadline.
Calling `run` while that worker is active adopts it. It does not start another
conversation or grant. Only one controller/accounting editor owns a project.

Ctrl+C requests cancellation; `stop` can do the same from another terminal.
Provider abortion and final usage capture receive bounded time before the owned
tree is retired. Inspect the resulting receipt rather than assuming cancellation
was instantaneous or free.

After a settled execution, `run --resume` explicitly starts another conversation
from retained notes/files. It uses the current configuration and remaining
cumulative budget. It does not secretly restore a killed SDK process, replay
uncertain work or retry a quota-exhausted session at a smaller allowance.

`reconcile` does not need readable current TOML. It can consume an existing
matching receipt or recover an exactly dead owner with proven tree quiescence.
Before a supervisor claim, an exactly dead launcher can be fenced as not started.
A racing claim prevents that recovery. Unknown liveness keeps the reservation.
Neither `status` nor `machine status` performs recovery.

Completion means the provider loop and evidence retention finished. A coach's
acceptance is a stopping assessment, not scientific certification. `checkpointed`
means unfinished work at an operational boundary, with `checkpoint_reason` exposed
in status. `needs_operator` instead requires the exact human question, dependency
kind (permission, input or scope), and an explanation of why useful work cannot
proceed within the existing scope. Choosing methods or asking whether unfinished
research should continue is not by itself an operator dependency.
`needs_operator`,
`review_unavailable`, `budget_exhausted`, cancellation, timeout and operational
failure remain distinct in the retained result/receipt.

## Optional research consultation

`consultant = true` supplies
`labgoblin_consult(question, evidence_paths=[], purpose="advice")`
to the ordinary researcher and, when enabled, the stopping coach. It authorizes
a fresh, synchronous read-only adviser, not arbitrary delegation. The adviser
inherits the configured model/effort and stays in the existing owned process
tree. Choose an explicit model when requesting a particular reasoning effort:
the current Copilot CLI rejects an effort setting with model `auto`.

Ask a concrete question about literature, an assumption or a materially different
approach. The host supplies the frozen original goal and scope instructions.
Questions are limited to 4096 UTF-8 bytes and eight ordinary evidence paths.
The initial context includes the first 8 KiB of each selected file; omitted
content has a paging pointer, not a claim of complete inspection.

The adviser has only bounded workspace read/list tools and Copilot's built-in
`web_fetch`. Public bibliographic discovery can use Crossref's metadata API,
followed by primary publisher/arXiv HTML. This is not a general search service
or PDF reader. Access failures, incomplete extraction and unverified leads must
remain explicit. Public queries may contain scientific concepts, never private
dataset contents or secrets. Native execution and content-disclosure rules are
trusted policy, not a hostile-agent sandbox or general secret detector.

Calls are one at a time, without a background queue or recursion. Each allows
at most five minutes including shutdown, 128 evidence reads, 16 KiB text pages
(16384 characters for web pages), and a 16 KiB final brief. The original execution
deadline and cancellation always take precedence. The researcher retains its
ordinary coding tools; neither the adviser nor coach receives a shell.

Before caller allowances are assigned, the harness reserves 10% of the remaining
project allowance for consultation, with a 30-credit floor and 300-credit ceiling;
an unlimited project reserves 300 credits. This is part of the same pool, not
extra spending permission or a new per-role setting. All consultations share
that reserve. Each gets only its unspent portion, further reduced if observed
spending is consuming another role's protected allowance. Below the provider's
30-credit minimum, the tool explicitly declines without starting an adviser.
Unused reserve remains available for later consultations in this execution;
it is not added to an active caller's ceiling. Unspent project credits persist
across explicit resumes. Soft overshoot is retained and can stop the caller.

Exact requests, inspected pages, web results, answers and usage are retained
under the execution's `sessions/consult-N` directory. Successful readable briefs
are also published under `artifacts/consultations` and captured in the final
snapshot. Tool-call replay does not purchase the same consultation twice.
Coach-requested consultation artifact paths accompany any researcher follow-up.
The researcher alone chooses what to adopt and updates `research-notes.md`;
advice is neither an independent validation nor a binding instruction.

### Targeted scientific review before stopping

With both `coach = true` and `consultant = true`, the closing guidance asks the
coach to identify a consequential claim on which stopping depends. When that
claim lacks adequate independent support or has an important unchecked assumption,
the coach requests `purpose="claim_review"` through the same consultation tool.
The question must state the exact claim, scope and decision relevance; at least
one ordinary evidence path is required. The researcher can also request this
mode during ordinary work.

The fresh adviser checks assumptions, quantifiers, primary evidence and possible
counterexamples rather than endorsing the report. Its brief separates supported
steps, gaps, attempted objections and unverified coverage, with a discriminating
check when useful. It has no shell or experiment capability. Neither another
model's agreement nor a successful consultation is scientific certification.

Completed briefs include their purpose and content digest. Coaches receive this
execution's brief index so they can inspect applicable prior reviews rather than
repeat an unchanged question; older executions' artifacts remain browsable.
Stopping records link consultations completed by
that coach, and the same researcher's follow-up receives the artifact pointers
and instructions to address material objections. Ordinary artifact copies can
change; the retained request, response and digest preserve what was delivered.
Reuse requires checking that the reviewed claim, assumptions and evidence still
apply; the host does not automatically certify semantic freshness.

Selection and scientific judgment are model-guided, not a deterministic proof
checker or a mandatory additional call on every stop. The host does enforce
request shape, the read-only tool boundary, settling an active consultation before
recording a stopping decision, ownership and existing accounting. No new budget,
review loop, automatic restart or default enablement is introduced. With the
consultant disabled, no adviser is invoked. Failed or inconclusive reviews remain
explicit limitations, not approval or a new `needs_operator` escape hatch.

Invalid requests and settled adviser failures return explicit tool errors.
Missing final adviser usage blocks further inference and leaves total usage
unknown, not zero. A failed consultation or unsuccessful literature lookup is
not evidence that all scientific avenues have been exhausted.

## Read-only research inspector

`labgoblin inspect PROJECT --open` starts a small loopback browser interface.
It shows harness-computed execution state and spending, ordinary text files,
bounded search, PNG/JPEG images, the original frozen goal, stopping reviews and
qualified public session excerpts. Opening, refreshing, browsing and loading
retained conversation never call a model. A completed execution defaults to its
retained snapshot; select **Working files** explicitly to inspect later edits.
For an active execution, the latest source is the mutable workspace.

To ask questions, supply an explicit model and a **separate finite cumulative**
soft cap of at least 30 credits:

```powershell
labgoblin inspect .\research --open --credits 100 --model MODEL_ID --reasoning-effort medium --timeout 300
```

Replace `MODEL_ID` with an installed/enabled Copilot model. `auto` is rejected
because it does not support the selected reasoning effort. These options do not
edit research TOML or change the researcher's model. Repeating `--credits 100`
preserves prior inspection spending; increasing it is an explicit top-up of the
cumulative ceiling. Omitting `--credits` always starts view-only, even if an old
approval exists. Submitting a question authorizes that one call; no summary
agent or model polling runs in the background.

Each question creates one fresh empty-mode SDK session with only curated
status, execution, file/list/search, image and session-event tools. It has no
shell, general filesystem, web, editing, research control or session-attachment
capability. Up to eight recent exchanges (32 KiB combined) provide conversational
context; omitted history is reported. Each answer refreshes evidence rather than
treating earlier answers as current fact. Clicking an `[E...]` button opens the
exact captured source and, for images, an inline preview. Source IDs are local
to their answer and become available after the inspection settles, never while
their files are being written. Completed captures are verified against their
receipt; interrupted captures without that binding are explicitly unverified.
Statements remain explanations, not independent scientific
certification or verified causal assessments.

Inspection history, approval records, owned session logs and source captures
are outside research: `%LOCALAPPDATA%\labgoblin\inspections\PROJECT_ID` on
Windows, or `~/.local/state/labgoblin/inspections/PROJECT_ID` on Linux.
The printed location is authoritative. They are not injected into researcher
context or research snapshots. The deterministic inspection owner shares the
project's machine ledger but not its research credit pool. Admission requires
one CPU and 1024 MiB without interrupting research; unavailable capacity is an
explicit error. Each question has a finite deadline, at least 60 seconds,
including shutdown. The default is five minutes.

Use the exact printed `http://127.0.0.1:PORT/#TOKEN` URL. The token stays in the
browser tab's session storage after the fragment is removed, so refresh works.
Do not publish it or expose the port through a proxy. Cross-origin requests
are refused; untrusted HTML/Markdown is shown as text, never executed.
Only bounded PNG/JPEG images are rendered, not SVG or research-generated HTML.
The inspector excludes inputs, runtime directories, environments and common
credential names; these exclusions are not a general secret scanner. A question
may send its selected ordinary evidence to Copilot under the operator's normal
data-sharing policy. Browsing alone keeps that evidence local.

Evidence limits are explicit: 16-KiB text pages from files up to 8 MiB; images
up to 4 MiB/16 million pixels; 128 reads/eight images per answer. Literal search
scans at most 64 files/2 MiB per page and retains 32 hits. Narrow a directory
with more than 10,000 entries. No-match or truncated search is not absence;
oversized files are reported as skipped. Session pages select up to 30 public
message/tool events from a bounded scan, not private reasoning. Final usage
records expose retained-event truncation when known. Live file pages reject
changed hashes rather than combine incompatible versions.

Cancel stops only this server's current answer. Ctrl+C also requests owned
answer cancellation and waits up to 45 seconds; it never stops research. If
settlement remains unresolved, the server exits with an explicit error while
the independent worker's deadline and reservation remain. Abrupt inspector
death also leaves that worker under the original deadline. After it settles, or when its
owner is proven dead, explicitly recover only inspection with:

```powershell
labgoblin reconcile .\research --inspection
labgoblin record-usage .\research --inspection --run INSPECTION_RUN_ID --credits 42.5 --reason "Recovered final metrics; source: ..." --confirm PROJECT_ID
```

The second command is needed only for missing final usage, with genuine attributed
evidence; add `--estimate` only for a deliberately approved audited estimate.
Unknown bills block new questions even after a cap increase. Partial lower bounds,
estimates and reported spending remain distinct; original receipts survive.
Recovery and accounting controls are operator CLI actions, not browser/model tools.

## Missing credit usage

Accounting uses integer nano-units: one credit is `1,000,000,000 totalNanoAiu`.
Partial checkpoints are lower bounds. They cannot authorize another run when the
final bill is unknown, including after an apparently successful experiment.

If the operator obtains a final, attributable total, append it without altering
the original receipt:

```powershell
labgoblin record-usage .\research --run RUN_ID --credits 42.5 --reason "Recovered final provider metrics; source: ..." --confirm PROJECT_ID
```

When final usage cannot be recovered, a deliberate audited estimate is separate:

```powershell
labgoblin record-usage .\research --run RUN_ID --credits 60 --estimate --reason "Explicit upper estimate; evidence: ..." --confirm PROJECT_ID
```

An estimate cannot be below already observed usage. It counts against the cap,
stays labeled as estimated, and never becomes reported spend. Later final usage
may supersede it by appending evidence; conflicting final records are rejected.
These commands only apply to exact quiescent executions with missing usage.

## Existing installations and removed features

Old campaign databases and formats are rejected, not migrated. Retain the
compatible legacy installation for their status/recovery. Do not edit their
envelopes, receipts or installed frozen helpers to make the new executable read
them. Moving/cloning project state is not supported as a way to reset identity.

The shared machine ledger retains version 3. New executions use a compatible
v3 exchange envelope/receipt containing the new private execution record, so
legacy installed readers can still observe pending reservations. The new runtime
does not reevaluate another runtime's eligibility, parse its campaign database
or release its grants.

Managed WSL/Docker execution, Claude, assisted initialization, the dashboard and
observer, scientific publications, handoffs, delegated scopes, memos, generations,
scheduled reflection/compaction and separate final-analysis/report agents are
not part of this replacement. There are no old command aliases.
