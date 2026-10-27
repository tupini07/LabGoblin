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

The default configuration is flat TOML:

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
max_stalled_reviews = 10
consultant = false
closure_critic = false
ledger = ""
max_snapshot_bytes = 68719476736
```

Coaching is opt-in (`init --coach` or `coach = true`). `model` and
`reasoning_effort` are passed to Copilot. Choose an explicit model for
matched evaluations. The SDK transport and ordinary researcher tools remain the
same with coaching on or off. Skills, extensions, MCP servers, automatic custom
instruction discovery, memory, scheduled prompts and unrestricted nested agents are disabled;
the reviewed project instructions are supplied explicitly.

Four optional model overrides apply to fresh review sessions:

| Setting | Session selected |
|---|---|
| `coach_model` | Every stopping coach and fresh closure adjudicator. |
| `closure_critic_model` | The host-triggered adversarial closure critic. |
| `advice_model` | A consultation with omitted purpose or `purpose="advice"`. |
| `claim_review_model` | A consultation with `purpose="claim_review"`. |

For example, after enabling the required roles:

```toml
model = "gpt-6.1-sol"
reasoning_effort = "high"
coach = true
consultant = true
closure_critic = true
coach_model = "claude-opus-5.5"
closure_critic_model = "gpt-6-astra"
```

This is a configuration example, not a claim that these models are available to
every account or that mixing them improves scientific quality. Omitted advice and
claim-review overrides inherit `gpt-6.1-sol`, even when the requesting coach uses
Opus. Remove an override to inherit again; empty strings are invalid. The matching
initialization options are `--coach-model`, `--closure-critic-model`,
`--advice-model` and `--claim-review-model`. Setting an override does not enable
its role or create any additional session.

The common `reasoning_effort` must be supported by every enabled named model.
Inside the owned runtime, the harness checks all enabled named selections against
Copilot's catalog before the first research prompt, including vision support for
image-enabled coaches and critics. Each fresh session also verifies its applied
model/effort and restricted tool allowlist. A mismatch, unavailable selection or
unsupported capability is an explicit failure, never silent substitution or
approval of an incomplete review. Availability can still change during a long run.
Disabled roles' presets are not queried. `auto` remains Copilot-controlled routing,
not a pinned concrete model; choose explicit IDs for reproducible role assignments.
The current CLI rejects a reasoning-effort setting with `auto`.

Session identity, `model.json`, usage checkpoints and final session records retain
the requested and confirmed selection. Raw SDK usage retains any provider-reported
per-model breakdown; the selection `auto` is not evidence of which concrete model
handled each response. Attribution does not depend on the bounded event spool.
Selections are frozen with the execution, and later prompts must preserve the
same selected model/effort; a fresh adjudicator uses the configured coach model. There is no
researcher rotation. These settings do not affect the independently configured
inspector or change limits, permissions, review counts or cumulative accounting.

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
a fresh, synchronous read-only adviser, not arbitrary delegation. Its model is
selected by purpose from `advice_model` or `claim_review_model`, falling back to
the campaign `model`, not the caller's override. It retains the common
`reasoning_effort` and stays in the existing owned process tree.

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

Calls are one at a time, without a background queue or recursion. Each ordinary
advice or claim-review call allows at most 30 minutes including shutdown,
128 evidence reads, 16 KiB text pages (16384 characters for web pages), and a
16 KiB final brief. The original execution
deadline and cancellation always take precedence. The researcher retains its
ordinary coding tools; neither the adviser nor coach receives a shell.
The time limit is a ceiling, not a target duration. Existing executions retain
their frozen adviser policy; the longer limit requires an updated runtime for
a new execution.

There is no separate adviser credit quota. While the caller is blocked on its
owned consultation tool, the harness reduces its cumulative ceiling to observed
spending plus the provider's 30-credit minimum for a response. Only after that
update succeeds can the adviser receive the remaining campaign allowance,
excluding protected stopping-review funds. Final adviser spending is counted
before unused funds are returned to the caller and its tool result is released.
An unlimited campaign imposes no adviser credit cap.

If the shared balance cannot fund both adviser and caller response, the tool
explicitly declines without starting another session. A failed limit transfer,
unknown final spending or overshoot into protected campaign headroom blocks
further inference; unused credits do not excuse uncertain accounting. Unspent
project credits and all actual spending persist across explicit resumes.

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

### Adversarial closure criticism

`closure_critic = true` requires both `coach = true` and `consultant = true`.
For fresh projects use `init --coach --consultant --closure-critic`. It defaults
off, including in old frozen format-5 records. Enable it before a new execution;
resuming preserves cumulative spending and the completed execution's evidence.

The initial neutral challenge and ordinary coach continuations are unchanged.
When a later coach proposes final acceptance, the host retains that proposal
and starts a fresh adviser with a different mandate: find the strongest
substantiated reason to continue toward the original goal, not help defend the
researcher's stopping case. The critic receives the original goal, scope,
researcher's response and actual remaining time/resources, not the preliminary
coach verdict. It can browse notes, artifacts and prior briefs. No compelling
objection is a legitimate limited result; speculation is not compulsory work.

The proposing coach closes without adjudicating its own proposal. The full
retained critique goes directly to the persistent researcher for one closing
response. The researcher chooses an investigation, correction, evidence-based
rebuttal or genuine limitation, and applies any required report changes.
A fresh read-only adjudicator then receives the original goal, critique, updated
response and evidence tools, but not the proposing coach's approval. It uses
`coach_model`; there is no additional model-selection setting or consultation loop.

Its required `critique_response` addresses the strongest objection, consequence
for the original goal, and inspected evidence supporting continuation or dismissal.
It must explicitly list `required_corrections`; `accept` rejects a nonempty list.
Outstanding required changes need `continue` and a concrete correction request.
The judge must inspect the actual report, not accept a promise of later edits.
This is a declared obligation check, not automated understanding of every claim.
Narrowing a report or deciding against product release does not automatically
justify stopping on an unmet goal. Progress is assessed once against the previous
review, including intervening research and the closing response, not critic prose.
There is no critic veto, repeated critic/judge debate inside this decision, or
automatic restart. A later researcher stopping proposal may receive a fresh
challenge; both roles should inspect prior briefs rather than repeat answered
objections. Semantic freshness and scientific judgment remain fallible.

Critic and enabled coaches also have `labgoblin_read_image(path)`: PNG/JPEG
files each at most 4 MiB, 16 million pixels and 16384 pixels
per dimension. Exact bytes and hashes are retained before SDK image delivery.
SVG/HTML, external paths, credentials and runtime state are rejected. Existing
ordinary advisers remain text-only. Image viewing is not numerical reproduction.
Coaches retain their 128-read/eight-image count limits. The critic has no
read-count or image-count cap; each evidence record remains size-bounded.

Closing-phase allocation requires a verified settled/idle boundary.
The critic can use the unspent pool except a provider-minimum allowance
each for the researcher response and fresh adjudicator; these cannot be dropped
to make an otherwise unaffordable critique start.
It has no separate credit cap, including in an unlimited project. The finite
coach's cumulative limit is reduced before funding the critic. After settled
criticism and proposer shutdown, unused funds go to the researcher response,
then the fresh adjudicator. Ordinary consultation transfers use the exact blocked
tool boundary described above, not an arbitrary waiting session. No concurrent
spending commitments overlap, and all prior spending remains counted.

The critic gets at most two hours including shutdown, with ten minutes retained
for the researcher response and another ten for fresh adjudication inside the
original campaign deadline. The researcher can use more remaining time, but its
closing response must end before the final ten-minute adjudication reserve.
The critic receives its own remaining response time and absolute deadline,
distinct from the campaign time; its evidence tools refresh that countdown.
The coach has no separate time cap.
The two-hour limit is a ceiling, not a target duration. At least 60 seconds must
remain for starting the critic itself. These are operational safeguards, not
task-cost predictions or a guarantee the review can finish. Insufficient time
or funds, failed criticism, response or adjudication produces an explicit
incomplete outcome, not completion. Unknown spend and overlapping soft overshoot stop inference;
cancellation, quota and genuine operator dependencies retain their usual outcomes.

Inspect `review-N-proposal.json`, `sessions/consult-N/request.json`,
`response.json`, retained evidence, the coach's `closure-allocation.json`,
`closure-settlement.json`, revision-qualified `allowance-*.json` records and
`closure/N/{request,submitted,response}.json`, its exact `prompt.txt`,
`sessions/adjudicator-N/closure-adjudication.json`, and final `review-N.json`.
These distinguish a proposal, actual criticism, attempted/submitted/settled
researcher response and a fresh adjudicated outcome. No source, receipt or
scientific conclusion from an earlier execution is rewritten.

## Campaign chat and research inspection

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

Each question creates one fresh empty-mode SDK session with curated status,
execution, file/list/search, image, session-event and operator-inbox tools.
One additional tool can prepare an idea draft outside research; it cannot publish.
There is no shell, general filesystem, web, research editing/control or
session-attachment capability. Up to eight recent exchanges (32 KiB combined) provide conversational
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
context or research snapshots. Only explicitly published idea files and their
attribution cross into the research workspace. The deterministic inspection owner shares the
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

Evidence limits are explicit: 16-KiB text pages from ordinary files up to 8 MiB
(published idea files support streaming reads up to 512 MiB); images
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

### Sending ideas through the same chat

Ask the inspector to prepare an idea, discuss/revise its proposal, then open
**Review idea draft**. The preview shows exact text, attachment identities and
source provenance. Attachment buttons read the captured copies, not whatever the
original file contains later. **Publish idea** is a separate authenticated human
action; a model cannot call it. Until then nothing enters research. Each question
can prepare eight drafts, each with 64 KiB of text and sixteen selected evidence
files. Attach large documents instead of asking the model to reproduce them.
The ordinary evidence exclusions also apply to draft attachments.

Publication itself spends no model credits. The CLI provides the same publication
contract for files you authored or explicitly selected outside the chat's narrower
evidence scope:

```powershell
labgoblin ideas publish .\research --id material-lead --title "An alternative mapping control" --file .\ideas_inbox\drafts\material-lead.md --attachment .\supporting-analysis.md
labgoblin ideas status .\research
labgoblin ideas publish .\research --id material-lead --base CURRENT_REVISION --title "An alternative mapping control" --file .\ideas_inbox\drafts\material-lead.md
```

The draft path is your choice; create/edit it normally. `--sha256 HASH` optionally
requires the reviewed primary-file bytes. Updates must name the current revision;
stale updates fail rather than overwrite another publication. Repeating the same
publication is idempotent. The inbox supports 128 idea identities, with 128 files
and 512 MiB total per revision. Selected files are streamed, hashed and checked
for concurrent changes. Published copies live under
`ideas_inbox\published\ID\REVISION`; never edit those copies. Draft changes do not
republish themselves. Failed captures/index commits are not admitted publications.

New-runtime executions deliver pending revisions to the same researcher at
startup and stopping boundaries, including runs without a coach. Only a short
notice and a captured index path enter its prompt. The researcher reads files as
needed and explains investigation, prior coverage, rejection or deferral in its
normal answer/notes. The coach and closure critic get the same file/reply pointers.
There is no compulsory experiment or structured scientific acceptance schema.

**Pending** includes every revision without a retained reply. **Prepared** means a
notice was captured; **submitted** means its SDK send returned, not proof that the
model read every file. **Responded** requires the exact researcher prompt to
settle and return successfully, with a retained reply. It does not certify that
the idea was investigated or correct. Interrupted/quota-exhausted responses stay
pending. Delivery indexes and replies remain under `ideas_inbox\deliveries`.
One execution permits at most 1024 delivery batches; exhaustion is explicit.

Publication and final closure share a short, separate lock. An idea published
before the closure cutoff must be returned to the researcher before discretionary
completion; an arrival during critic/adjudication cannot hide behind an earlier
acceptance. A post-cutoff publication remains pending for a later explicitly
authorized execution. Hard stops and genuine operator dependencies still take
precedence. Neither publication nor prose resets spending, time or stalled-review
counters. Existing frozen executions do not gain this capability retroactively;
deploy and explicitly resume to use it. Native processes remain trusted, not
filesystem-sandboxed; captured hashes detect changes rather than physically
preventing an operator or shell-capable researcher from modifying files.

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
