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
| `inspector.py`, `inspection_evidence.py`, `inspection_server.py` | Independently owned read-only questions, qualified evidence and loopback browser access. |
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
with a five-minute per-consultation bound. There is no additional machine grant,
detached worker, recursive delegation or scientific task scheduler.

Before caller admission, reserve 10% of the available pool, floored at 30 and
capped at 300 credits (300 for unlimited), for all consultations together.
Every normal caller ceiling excludes the unused reserve. Adviser admission
also checks observed spend plus the caller's remaining commitment and protected
review/continuation allowance. It never adjusts a waiting caller's ceiling.
Known soft overshoot that overlaps protected allowance stops the caller; unknown
adviser spending blocks inference and cannot yield a complete aggregate bill.
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
reserve, deadline and settlement. Requests and responses retain the purpose;
successful responses and brief indexes also retain the readable artifact digest.
Coaches receive prior brief pointers and their decisions retain the consultations
completed by that coach. An active consultation prevents recording a stopping
decision. Material objections return through the existing same-session follow-up.
Target selection, reuse after evidence changes and scientific interpretation are
fallible model judgments, not a host-certified proof or a mandatory per-stop call.
The inspector remains an explainer; it gains no scientific-review/control role.

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

Control JSON is bounded and finite. Provider events have explicit per-event and
per-role retention bounds. Final workspace capture streams regular files and
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

The six custom tools expose status, frozen execution/review details, bounded
ordinary files/images, directory listings, literal search and exact-run selected
public events. Closed tool metadata is verified before prompting. No external
retrieval, arbitrary commands/files, session attachment or research mutation is
available. Session roles and IDs must match the research envelope; completed
snapshot files must match their receipt-bound manifest. Every retrieved answer
source is captured with exact read arguments, time and coverage; terminal
inspection receipts bind those captures by hash. Live or interrupted captures
without a terminal provider manifest are explicitly not receipt-verified.

The HTTP server binds only loopback, authenticates API access with a random token
and checks Host/Origin. Browser content uses text nodes and bounded PNG/JPEG data,
not executable research HTML. Status polling during an answer does not invoke a
model. Browser closure is not research cancellation; explicit inspector shutdown
requests only its own question's cancellation and retains unresolved reservations.

Native agents are trusted and share the operator's filesystem identity. These
service checks do not claim isolation from malicious native code or a replacement
for independent scientific validation.
