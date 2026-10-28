# Minimal execution contract

LabGoblin version 3 has no scientific campaign database. Configuration/project
format is 5; the private execution record is 4. The machine ledger schema and
generic exchange shape remain 3. These are separately checked, not migrated.

## Authority

| Module | Responsibility |
|---|---|
| `config.py`, `protocol.py` | Strict configuration and small execution records, not scientific schemas. |
| `runtime.py` | Explicit initialization, one controller, frozen launch, recovery and retained evidence. |
| `provider.py`, `guidance.py` | One ordinary SDK researcher, bounded read-only stopping review and short instructions. |
| `consultation.py` | Shared bounded evidence readers and retained public-literature retrieval for optional advisers. |
| `inspector.py`, `inspection_evidence.py`, `inspection_server.py`, `inspection_ideas.py` | Independently owned campaign chat, qualified evidence, external idea drafts and authenticated human publication. |
| `ideas.py` | Immutable operator file revisions, exact delivery/reply records and the final-closure cutoff; not scientific state. |
| `scheduler.py`, `db.py` | Shared machine admission, one-time claims and qualified terminal receipts. |
| `payload.py`, `processes.py` | Native ownership, affinity, memory checks, finite deadlines and bounded streams. |
| `credits.py`, `storage.py` | Cumulative reported/estimated spending and immutable retained files. |
| `cli.py`, `paths.py` | Explicit local controls and LabGoblin names. |

There is no hypothesis/publication/reflection/handoff/final-analysis service hidden
behind these modules. Normal research files remain the researcher's working
medium. The module must not start another scientific turn because a JSON form is
missing or a report was not published through an API.

## Admission and ownership

1. An existing project identity and OS file lock establish one controller.
   Recovery precedes candidate configuration activation or another admission.
2. The controller reads current configuration, goal and instructions once.
   Prior exact receipts and accounting corrections determine the remaining pool.
3. The shared ledger grants native CPU/RAM/GPU placement. Probes and owner-liveness
   observations occur outside its writer transaction.
4. The controller freezes package helpers and prompt/configuration bytes, then
   arms one exact execution against the grant. Project identity, grant ID,
   resources, CPU set, launcher and allowance must agree.
5. An independent supervisor verifies its frozen helpers and claims that
   one-use execution before starting a payload. A late or repeated claim cannot
   consume a retired grant.
6. The native owned tree runs one provider driver and its children. Only after
   tree retirement, pipe EOF and evidence retention does a matching receipt settle
   execution and release resources.

The ledger still uses its v3 envelope/receipt shape, with the exact private record
inside metadata. The outer digest covers that entire representation. The new
runtime verifies the outer/inner mapping rather than trust inconsistent copies.
This is transport compatibility, not an old campaign reader or an old scientific
workflow. Native research is a genuine `research-run` resource owner, never an
observer or build impersonation.

Other installed runtimes retain authority over their own pending eligibility and
grants. Old installed readers can observe these pending records without decoding
a new scientific schema. No existing campaign, envelope or receipt is rewritten.

## Windows and Linux

Windows independent launches use `background_options(independent=True)`:
breakaway and a windowless console, not `DETACHED_PROCESS`. The payload starts
suspended, receives its named Job Object, RAM limit and exact CPU affinity, then
resumes. Kill-on-job-close and verified job retirement cover ordinary descendants.
Unmodified descendants inherit a windowless console.

Linux uses a dedicated process session and exact boot/start identities, with
pidfds for signaling owned members. Affinity is applied before launch; process
session RSS is monitored. This is trusted native execution, not a cgroup security
boundary. Deliberately escaped daemons, other hosts, WSL and containers are outside
the supported research contract.

All operations have a hard deadline. SDK shutdown grace fits inside it. Rotating
stdout/stderr spools continue draining after retention fills; log pressure cannot
disable cancellation or the deadline. Truncation and stream errors are explicit.

## SDK lifecycle and spending

The ordinary researcher retains its SDK session through any coach follow-up.
Each coach uses a fresh empty-mode runtime and only curated read-only evidence
tools plus a small stopping decision. Custom instructions are supplied explicitly;
extensions, discovered skills, MCP servers, schedules and unrestricted nested
model agents are not part of this execution.

Owned CLI processes disable history sync/export with `--sync-session=off` and
`--no-remote-export`. Every SDK session explicitly uses remote mode `off`;
client-level remote-session support also remains disabled. Do not use the removed
`--no-remote` CLI flag or change shared user settings to enforce these boundaries.

Optional `coach_model`, `closure_critic_model`, `advice_model` and
`claim_review_model` belong to the frozen configuration. Omission inherits the
campaign model independently for each purpose; it never inherits a caller's
override or enables a role. Researcher and independent inspector selection remain
their respective configured model. All purposes use the shared reasoning effort.
The first research client's startup validates every enabled named selection
against the SDK catalog before any prompt, without another client or allowance.
Each fresh session checks its own selection again. Missing/disabled models,
unsupported effort and missing vision for image-enabled coach/critic sessions
fail explicitly. The SDK exposes no separate model-level tool-calling capability
flag; restricted session tool initialization and exact metadata validate the
registered allowlist instead of claiming a scientific tool-use qualification.

The authoritative `session.model.getCurrent` snapshot must match the requested
named model and effort before a prompt, including later researcher continuations
and fresh closure adjudication using the coach model. No fallback or session model-switch API is used.
`auto` retains provider-controlled routing and is not a fixed concrete backend;
its capability/effort acceptance remains the provider's responsibility.
Immutable session identity and `model.json` retain requested/applied selection,
also carried in usage checkpoints and final receipts alongside raw SDK per-model
usage. Mismatches are retained separately; an event spool filling cannot erase
model attribution. Adviser responses and readable briefs identify their model.
New optional fields do not rewrite old snapshots, helpers, records or receipts.

The initial coach's `accept` is provisional. When limits permit, it uses the
first researcher continuation for a neutral closing challenge; a `continue`
instead supplies the coach's concrete question. Subsequent reviews assess updated
evidence and can request further same-session investigation. Each compares progress
with the previous review; meaningful progress resets a consecutive stalled-review
counter, otherwise it increments. At `max_stalled_reviews` (default 10), continued
disagreement checkpoints as unfinished research. The initial review is the baseline.
The neutral challenge is never re-armed, including by a progress reset.

`needs_operator` ends immediately but requires an exact question and a permission,
input or scope dependency. Operational limits use `checkpointed`, with a retained
`checkpoint_reason`, not that operator label. An early coach-requested stalled
checkpoint requires two consecutive no-progress follow-ups; a deadline checkpoint
requires at most 60 seconds remaining including shutdown. The host checks these
boundaries. Hard interruption, quota and failed review retain their distinct
noncompletion outcomes. The original deadline, final-usage checks and idle-session
allowance update govern every continuation. Every review protects continuation/
review headroom when available; no role receives another copy of the project pool.

New configuration snapshots carry `max_stalled_reviews`. Usage accepts contiguous
`coach-N` roles with deterministic session IDs and one prompt each; researcher
prompts must alternate with those reviews. Older frozen snapshots without that
field retain their original two-prompt/two-review accounting bounds. Original
receipts, frozen runtimes and hashes are never rewritten. Previous review context
is bounded to one retained decision; full history remains in per-review records.
Failure, cancellation or exhaustion cannot fall back to provisional approval.

An explicitly enabled `consultant` flag supplies one host-owned consultation
tool to researcher and coach. It does not change their existing tool/role policy.
Each fresh `consult-N` adviser has only curated evidence reads and a verified
`web_fetch` allowlist, never the coach's decision tool or ordinary coding tools.
It runs within the same supervised tree, resource envelope and absolute deadline,
with a 30-minute per-consultation bound for both advice and claim review,
including shutdown grace. There is no additional machine grant, detached worker,
recursive delegation or scientific task scheduler.

Consultations have no independent credit pool or per-call quota. The exact
host-owned tool callback blocks its caller until allowance transfer and adviser
settlement finish. All other sessions must already be settled with known usage.
For finite campaigns, first reduce the caller's cumulative ceiling to observed
usage plus the provider-minimum response allowance. Only after acknowledgment
may adviser admission use the remaining campaign pool, excluding that caller
commitment and protected review/continuation funds. On settled adviser shutdown,
refresh caller usage and restore its share of the remaining campaign pool before
returning the tool result. Each transfer retains its exact tool-call identity
and unique allowance revision. Unlimited campaigns impose no hidden quota.
Arbitrary waiting sessions cannot receive limit updates, and an exhausted
session is never renewed. Failed transfers and soft overshoot into protected
campaign headroom stop the caller; unknown adviser spending blocks inference
and cannot yield a complete aggregate bill.
These fatal conditions cancel the SDK tool callback without returning a result
that could permit another model response, then abort the caller through owned
shutdown. Settled, nonfatal consultation failures remain ordinary tool errors.
Exact contiguous adviser roles and deterministic session IDs qualify aggregate
usage. The optional flag defaults off in existing format-5 records; no frozen
configuration, helper or receipt is rewritten.

Requests bind the caller/session/tool-call identity, original goal/instructions
and initial evidence pages. Successful replays return the same answer without
new inference; changed requests under one call ID fail. Calls are serialized by
rejecting concurrent requests, not queued. Public retrieval uses the installed
CLI's safe fetch path and bounded pre/post hooks; retained results distinguish
actual inspected material from adviser prose. A missing/truncated evidence
capture cannot be called a successful complete consultation. Readable briefs
are creation-only ordinary artifacts, not a second editable research plan.

The optional request `purpose` is `advice` by default or `claim_review`; the latter
requires at least one evidence path and selects a falsification-oriented charter.
It still uses an ordinary owned `consult-N` session with identical permissions,
shared allowance, deadline and settlement. Requests and responses retain the purpose;
successful responses and brief indexes also retain the readable artifact digest.
Coaches receive prior brief pointers and their decisions retain the consultations
completed by that coach. An active consultation prevents recording a stopping
decision. Material objections return through the existing same-session follow-up.
Target selection, reuse after evidence changes and scientific interpretation are
fallible model judgments, not a host-certified proof or a mandatory per-stop call.
The inspector remains an explainer; it gains no scientific-review/control role.

With explicit `closure_critic` plus coach/consultant flags, a follow-up coach's
acceptance is retained as a proposal. A host-only `closure_critique` purpose
reuses an ordinary owned `consult-N` session, not the model-facing consultation
purpose enum. It receives the original goal/stopping response and bounded evidence,
not the coach's approval. After the proposer shuts down, the persistent researcher
receives the full critique for one response/investigation. A fresh
`adjudicator-N` session uses purpose `coach` and its configured model, with only
read/list/image/decision tools. It receives the original goal, critique, response
and prior review context, not the proposed approval. It must record
`critique_response` and explicit `required_corrections`; acceptance rejects any
outstanding correction. Required edits use ordinary researcher continuation,
not conditional acceptance or a separate report writer. Semantic judgments remain
fallible. There is one critic/response/fresh-judge exchange per prospective final
stopping case, not a recursive deciding hierarchy. Progress is counted once
against the prior review, including the intervening researcher work.

Allocation requires every existing session settled, idle and with known usage.
An earlier failed adviser does not block later closure once its usage has settled
and shutdown succeeds. Successful shutdown clears its in-flight marker; this
does not turn its failed answer into a successful consultation. Incomplete
settlement or shutdown still blocks further inference.
The host reduces the finite coach's cumulative ceiling before reassigning unused
pool capacity to the critic, mandatorily protecting a provider-minimum allowance
each for the closing response and fresh judge. The critic has no independent
credit cap. After known, settled criticism the proposer closes, unused capacity
funds the researcher response. The judge uses actual remaining funds after response
settlement, protecting future continuation/review headroom when affordable.
Transfers have unique allowance revision and closure allocation/settlement records. No
exhausted session is renewed and no old spend disappears. Waiting-caller transfers
are restricted to the exact owned consultation callback described above.
Criticism lasts at most 7200 seconds including shutdown, reserves
600 seconds each for response and adjudication, and is bounded by the original
execution deadline. The response can use more available time but its own deadline
is 600 seconds before the execution deadline, retaining the usual shutdown grace.
Timing remains in retained host request records, not routine model prompts or
evidence countdowns. A coach/adjudicator receives a boundary notice only when a
deadline checkpoint is actually eligible. The coach remains bounded by the
execution deadline only.
Insufficient capacity is review-unavailable, never silently skipped approval.

New frozen prompts advertise `closure_response_version = 1`. Every coach,
adjudicator and adviser has at most one prompt. An extra researcher closing prompt
requires a retained proposal and exact completed owned critique in
`closure/N/request.json`. Attempted delivery is not a settled response:
`submitted.json` binds the exact prompt, and `response.json` binds the researcher
answer and settled prompt metrics. The fresh judge's own reference must bind
those exact records. One prompt cannot claim both a closing and an operator-idea
extra allowance. Ideas can still accompany that response and remain subject to
the final inbox cutoff. Missing or failed responses never authorize adjudication.
Legacy frozen records retain their earlier same-coach prompt bounds and bytes;
an unknown capability version is rejected, not interpreted as legacy.
Critic and enabled coach image tools reuse the inspector's PNG/JPEG size/dimension validation but
not its state access. Critic read/image counts are uncapped, unlike the coach's
128 reads/eight images. Each image is at most 4 MiB, retained in a bounded
6-MiB evidence record; ordinary pages keep their 128-KiB retention bound.

The driver records exact session IDs, requested limits, events, final responses,
per-prompt settled metrics and role-level final usage. Only observed idle plus
successful metrics collection establishes a settled prompt. Sending another
prompt invalidates that session's settled status until its new response settles.
Transient progress-metric RPC errors are visible but do not force an unnecessary
research restart; a missing final total still blocks new admission.

Spending is integer `totalNanoAiu`; one credit is 1e9 units. In-place SDK limit
updates only reallocate the unspent project pool after observed role costs.
The driver does not reopen an exhausted session, ask the model for cost forecasts
or discard a soft overshoot. Researcher, coach and adviser spending belongs to the
same receipt.

Receipts with missing final usage retain any partial lower bound and remain
accounting-unknown after resource release. Operator-reported final usage or an
explicit estimate is an additional, receipt-bound record. An estimate is separate
from reported usage and cannot fall below observed spend. Final evidence may
supersede an estimate without rewriting it.

## Recovery

Controller death is not payload death. A new controller adopts the existing
supervisor and its original deadline, configuration, session and allowance.
It does not start an inference retry.

Before a committed claim, recovery requires the exact launcher to be dead and
rechecks that no supervisor claim won the race. It atomically fences the execution
as not started. After a claim, owner death alone is insufficient: the named
Windows job or Linux process session must also be proven quiescent. Unknown
ownership is not permission to release, retry or infer zero spending.

A matching existing receipt can finish an interrupted ledger write. A ledger
receipt can restore a missing identical receipt file. Conflicting bytes fail;
neither direction creates a new scientific interpretation. Neither reconciliation
nor adoption starts another research call. An explicit first `run` can clear
proven-unarmed requests before admitting its requested execution.

## Retention and read-only operations

Control JSON is bounded and finite. New provider sessions advertise
`event_log_version = 1`: original canonical SDK JSONL in 8-MiB segments, a host
sequence/prompt/type/time/tool-call byte index, and a periodically checkpointed
manifest. Final owned usage binds the closed manifest; sealed indexes and
retrieved events are digest-checked. Each event is at most 1 MiB; raw data plus
indexes are at most 1 GiB per role. Oversized records have omission entries;
total exhaustion records the unindexed sequence tail. Navigation metadata may
be shortened, never the retained original event. Live manifests can lag their
active index, and incomplete tails/unverified coverage stay explicit. Retention
errors cannot prevent idle/quota event handling. Exact submitted host prompts
are also retained as `prompt-N.txt`, without extra model calls.
Legacy sessions keep their original byte-paged reader. Indexed excerpts return
at most 30 events; full selected events use UTF-8-aligned 16-KiB byte pages.
Final workspace capture streams regular files and
hashes the retained bytes, rejecting concurrent changes, byte/count exhaustion
and insufficient free space. Excluded inputs/environments/caches/state/links and
special files are listed, not passed off as retained sources.

`status` and machine inspection do not initialize, reconcile or mutate research.
The active/last-loaded configuration remains distinct from candidate TOML,
including malformed candidate edits. An explicit new execution activates new
configuration without resetting cumulative credits.

## Independent inspection

`inspection-run` is a genuine native consumer kind with a deterministic
project-qualified owner distinct from `research-run`. Its v3 exchange envelope
uses observer semantics while retaining the exact private execution record.
Grant kind, owner, allowance and entrypoint are checked; it cannot impersonate
research or consume research billing. Existing research envelope mappings stay
unchanged. No ledger/project schema migration is needed.

Explicit finite cumulative approval and chat records live outside research.
Each question freezes the target identity, recent bounded conversation, model,
allowance and helpers; the same one-use claim, native ownership, deadline,
cancellation and receipt kernel runs one fresh empty-mode inspector session.
There are no coach/adviser roles, GPU allocations or workspace snapshots for
inspection. Unknown spending blocks another question; operator accounting uses
the same receipt-bound append-only mechanism, in the inspection state directory.
New questions recover inspection-owned dead work only, never research work.

The evidence tools expose status, frozen execution/review details, bounded
ordinary files/images, directory listings, literal search and exact-run selected
public events, plus published idea metadata. One narrow tool prepares captured
idea drafts in the owned inspection directory, never inside research. Closed tool
metadata is verified before prompting. No external retrieval, arbitrary
commands/files, session attachment or research editing is available.
Session roles and IDs must match the research envelope; completed
snapshot files must match their receipt-bound manifest. Every retrieved answer
source is captured with exact read arguments, time and coverage; terminal
inspection receipts bind those captures by hash. Live or interrupted captures
without a terminal provider manifest are explicitly not receipt-verified.
Successfully settled inspection receipts also bind draft manifests. Preview and
publication verify the exact draft and captured bytes; a separate authenticated
human HTTP action publishes the reviewed hash. The model has no publication tool,
and a view-only server cannot publish. Ordinary monitoring leaves the entire
research file inventory unchanged.

The HTTP server binds only loopback, authenticates API access with a random token
and checks Host/Origin. Browser content uses text nodes and bounded PNG/JPEG data,
not executable research HTML. Status polling during an answer does not invoke a
model. Browser closure is not research cancellation; explicit inspector shutdown
requests only its own question's cancellation and retains unresolved reservations.

### Independent terminal handoffs

`run --handoff` freezes the explicit inspection profile digest/directory in the
new research prompt. After snapshot capture and ledger settlement, the worker
requests one separately owned inspection of that exact terminal receipt.
The researcher is already retired; reporting neither appends researcher prompts
nor changes the research receipt. Inspection admission uses its existing finite
cumulative pool, model, deadline and genuine machine grant. A changed approval
fails instead of silently adopting a larger allowance.

The reporter has no prior chat or draft tool. Its evidence scope rejects mutable
workspace reads, other executions and the current operator inbox. All sources
remain bounded and captured through the shared inspector reader. Missing snapshots
or incomplete events are evidence gaps, not permission to read current files.
The output is interpretation, not a scientific assessment or accepted handoff.

An exclusive external request records source receipt/envelope, report date and
one inspection ID before admission. Repeated requests cannot duplicate inference.
Final inspection receipts retain narrative/citations/usage; dated HTML and a
read-only chronological index expose the report outside the research workspace.
Admission/reporting failure leaves a visible failure or incomplete request and
the original research evidence intact. No automatic retry or reporting on status/
reconcile. `handoff --run` is an explicit request for a previously unattempted
terminal report. A normal research result can be available before reporting ends.

## Operator idea publication and delivery

The inbox is a bounded file index, not a campaign/scientific database. Explicit
publication streams selected sources into an owned staging directory, validates
their identities, publishes an immutable revision, then atomically updates the
project-qualified head index. Failed capture/index updates never admit a revision.
Historical revisions and replies are retained. Updates use exact base revisions;
identical repeated submissions are idempotent. Mutable drafts are never polled.
The short inbox writer lock is separate from the execution-long controller lock.
File copies/hashes occur outside it; publication and final closure are serialized.

New frozen prompts explicitly opt into `operator_ideas_version = 1`. Delivery
captures exact revision references in both an owned runtime record and a readable
workspace index. Prompts contain only pointers, not document bodies. Prepare,
successful SDK submission and successful settled response have distinct records.
Cancellation, deadline, quota or settlement errors never become response receipts.
A new revision arriving during a response stays pending; acknowledging the old
revision cannot acknowledge its replacement. Coach/critic context names captured
files and replies without seeding the critic with preliminary approval.

Additional same-researcher prompts require owned, envelope-bound delivery records;
usage validation subtracts only these authorized prompts before checking the
original researcher/coach alternation. Original frozen prompts keep their old
bounds. No adviser/coach sessions are fabricated to balance counts. New ideas
do not renew session limits, consume protected review funds, reset stalled counters
or change authority. Both coached and coach-off final completion check the latest
inbox under its writer lock. A successful cutoff binds the execution and publication
sequence; arrivals afterwards wait for a later authorized run. The cutoff is an
operational delivery boundary, not a claim of scientific completion or final billing.

Native agents are trusted and share the operator's filesystem identity. These
service checks do not claim isolation from malicious native code or a replacement
for independent scientific validation.
