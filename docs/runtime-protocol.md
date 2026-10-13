# Local runtime contract

LabGoblin uses configuration/campaign database version 4 and machine-ledger/
worker protocol version 3. These versions are checked independently. Old formats are
not migrated: initialize a fresh local campaign. An incompatible existing
machine ledger must not be replaced while old work may still be running.

Only LabGoblin command, import, environment and default path names are supported.
Helper manifests must explicitly bind the `labgoblin` namespace and the complete
file set into their identity; every source/bootstrap hash is verified. Missing
or old namespaces are rejected, not interpreted as a compatibility format.
No new helper is substituted for an admitted frozen one. Windows Job Object/
container names, Docker ownership labels/build tags and sandbox markers use
LabGoblin names. The shared ledger retains its identity, application ID and version.
See [names and existing installations](local-research.md#names-and-existing-installations).

Only native, WSL2 and local Docker workers are supported. Each runner declares
its interpreter; WSL also declares its distro, and Docker its local context and
prepared image. Loading configuration is read-only and does not probe runners,
create state or launch inference.

Terminal initialization is a foreground bootstrap exception, not a managed
research/observer consumer. It uses the pinned core SDK with an explicit local
runtime inside an owned stdio relay (no automatic runtime download). The relay
retires its Windows Job Object when setup/its parent exits. No resource grant,
setup usage cap, autonomous work, saved transcript or resume operation is added.
Empty-mode tools require selected roots, per-file consent and approval of fixed
readiness probes; model text never authorizes publication. Exact reviewed bytes
and preimages are rechecked before creation. The initialization marker is removed
last; interrupted/incomplete publication is not an operational campaign.
Noninteractive/JSON init does not start or authenticate an SDK client.

Configuration activation is an exact-controller-owned, once-per-run transaction.
`configs` stores the existing typed representation; `restore_config` validates
it without reading current TOML. Recovery precedes startup validation and remains
independent of malformed/deleted candidate files. Mutation preparation captures
the loaded revision and an ownership epoch, including idle-to-running-to-idle
races. Build admission rechecks that fence after ledger arming and before process
launch; a mismatch records a proven-not-started receipt. No campaign writer is
held while entering the machine ledger. Already admitted envelopes are unchanged.
All TOML budgets/settings require restart; configuration activation never resets
accounting, generations, evidence or grants. Queued execution settings/resources
are revalidated, not silently rewritten, and old research packets cannot submit
under a newly activated configuration. Dashboard settings have a separate
process-start lifetime.

Research workspace publication and role scheduling are specified in
[the research-loop guide](research-loop.md). One controller owns all scopes;
root research/reflection/coaching/maintenance remains serialized, while workers
have distinct turn slots and event acknowledgements. All invocation receipts
are charged once to the single `campaign.max_ai_credits` pool. Provider-call
reservations are derived automatically, not configured as extra task/coach/call
budgets. Historical task ceilings and frozen launch commitments remain binding.
`research.task_max_seconds` accepts positive seconds or `"unlimited"`; snapshots
and task charters store unlimited as null. A finite charter pins an absolute
deadline from task creation into both provider and experiment envelopes, checked
again at arming and enforced by the payload owner. Unlimited charters omit only
that aggregate deadline; finite operation deadlines and cancellation still apply.
Late or cancelled worker evidence cannot advance root
strategy. Coach results require the same owned result capture and packet identity
as other roles. Failed review never authorizes sealing.

Explicit local image builds and observers share the machine ledger's
`consumer_runs` authorization/claim/receipt mechanism, not campaign invocation
accounting. Builds reserve container resources plus their API client, freeze an
explicit bounded context, pin local base IDs, and use the classic Engine build
API with no pull. Native-client exit alone never proves daemon quiescence.
Guest path/validator mappings are separately hashed and verified against the
owned launch argv before execution. Recovery qualifies the same mapping digest
on guest receipts, including a pre-execution input-pin refusal; it does not
bypass that check when the host supervisor was lost.

Campaign creation takes an exclusive external `.labgoblin.lock` lease; readers,
submission preparation and dashboard lifetimes take shared leases. Reset needs
exclusive access and never renames a directory containing its open lock handle.
Cached database objects verify campaign identity on every read/write.

Campaign and machine-ledger connections allow up to 30 seconds of SQLite busy
waiting for an individual operation. Durable commits on contended storage can
hold the writer lock longer than the former ten-second allowance. WAL mode,
full synchronous durability, read-only connections and operation deadlines are
unchanged; a lock error never replays a transaction body or authorizes a launch
retry. SQLite failures retain the database path, transaction stage and native
error name. Unhandled worker startup failures also retain a traceback in the
bounded supervisor diagnostics. Exhausted waits still fail explicitly and retain
uncertain launch/resource ownership. These diagnostics do not constitute a
non-execution or final-usage receipt, and updating the package does not repair
or replace a previously admitted frozen launch.

Slow database stages (at least one second) also emit a warning on stderr with
database, process, caller locations and elapsed time. Acquisition, transaction
body, commit, rollback and close are distinguished; SQL parameters and source
bodies are not logged. The busy timeout is not a deadline on disk flushes.
Use low-latency local storage for operational SQLite state: a second application
mutex cannot accelerate a durable commit, and increasing the wait is not proof
that storage contention is resolved. Do not disable synchronous durability or
move a live campaign's database as a workaround; its frozen paths and ownership
must remain intact.
For native Windows campaigns, a **cold whole-project relocation** can retain the
original directory as a junction to the new location. Stop all campaign processes
and readers, including the dashboard. Save and close editors, language servers
and terminals rooted in the project too: Windows directory handles can prevent
the rename even when research is stopped. Verify there is no controller, live or
uncertain launch, or campaign grant before copying. Preserve and verify the
complete project (including the environment, state, research files, evidence and
frozen runtimes), then replace the original directory with a junction. Keep a
verified rollback copy offline; never operate both copies as separate campaigns.
Do not move only the database, change the shared ledger, or retarget live paths.

Saved configuration revisions, queued specs, packets, envelopes and receipts
remain byte-for-byte unchanged. Restoration verifies that the recorded **project
directory**, not just its configuration file, resolves to the current project.
Only configuration location and project-local storage watermark paths are
normalized in memory; all other settings retain strict comparisons. Queued work
uses the same rule without rewriting its captured spec. External storage paths
and thresholds cannot silently change. Ordinary loaded snapshots still work
without a readable TOML file.

Keep every historical project alias permanently: original interpreter, input,
capture and helper paths remain in use. Verify snapshot restoration, historical
helpers/receipts and installed environment through both paths before restarting.
A copied project without the original whole-directory alias, a broken/retargeted
alias, or a config-file-only link is not a supported relocation. Guest
WSL/Docker mount relocation and live migration are not qualified by this native
procedure. Initialize new campaigns on their intended local volume when possible.

Reconciliation can retire a proven pre-claim failure: the exact recorded launcher
has exited, the launch is still armed with no supervisor/payload, and its frozen
runtime verifies. Workers must commit their claim before starting any provider
or payload. These conditions are rechecked while atomically recording a
`not_started` outcome, fencing out late claims before publishing its receipt.
The receipt retains the launcher and frozen runtime identities; normal settlement
then releases its unused reservations without inventing a provider bill or
replaying that launch. Missing launcher identity, unknown liveness, a prior claim
or damaged helper files do not qualify. A dead PID or missing receipt alone is
still insufficient.

`campaign.max_seconds = 0` and `campaign.max_invocations = 0` explicitly mean
unlimited for those dimensions. Per-operation deadlines, resources and storage
limits remain positive and finite. Zero GPU-hours means no GPU work. Invocation
accounting includes campaign research, retries, maintenance, final analysis and
model-executing canaries, not internal API calls or monetary cost. Observer usage
is separate.

`campaign.max_ai_credits` and `dashboard.chat.max_ai_credits` accept `"unlimited"`
(the generated default) or at least 30 Copilot AI credits. Credit limits are
post-response soft caps, not hard billing ceilings. Copilot research launch
envelopes freeze an allowance checked again in the arming transaction and enforce
it through SDK session limits (or `--max-ai-credits` for explicit CLI transport).
The frozen SDK wrapper command also carries the checked allowance. Normal inference preserves the final-analysis and
remaining canary bundle minimums. Receipts retain `totalNanoAiu`; integer nano-unit
aggregation avoids counting premium multipliers or replaying charges. Unknown
usage without an operator-approved estimate blocks capped inference, and proven
non-starts cost zero. Accounting spans
all generations and controller restarts without changing immutable receipts.

Reservations share unreserved credits among actually prepared/ready owned
reasoning scopes, not hypothetical idle slots. Final-analysis planning protects
at least one provider minimum, or the median of up to seven comparable accepted
root completion costs once three are available under the loaded configuration.
The budget projection labels this uncertain planning evidence and exposes
unusable history; no estimate is substituted into billing. Stopping-coach,
reflection and canary minimums remain separate protected commitments.

The SDK result captures explicit `session_limits_exhausted` provider events and
the `session.info` / `session_limits` notice
`Session limit reached. Increase or unset the session limit to continue.`
Usage updates and final-response/tool-disable warnings are not exhaustion.
Final spend at/above an allowance alone does not establish such an event.
Valid owned completion is accepted through the ordinary contract. A quota-only
session error can finish with its already-valid handoff after model-free preflight;
final usage/idle confirmation, shutdown and owned receipt checks still apply.
Other provider errors are not converted into completion, even if a later quota
error arrives. Without a valid handoff,
an explicitly limited root turn produces a durable `provider_limit` blocker
and an `operational_checkpoint` source, not another generic retry or invented
science. Only explicit operator resume clears this category; it does not clear
other recovery/accounting blockers. Advice failures remain nonblocking except
for ordinary accounting/safety constraints. Original receipts and drafts are
never rewritten, and historical SDK results lacking this field are not inferred
to contain a limit event.

Research defaults to a 1800-second hard deadline. Copilot's owned SDK payload
records bounded activity checkpoints and periodic provider aggregate usage.
Only meaningful response/tool lifecycle events update progress; a heartbeat or
log write is not progress. The 600-second idle watchdog applies only when no
response/tool is active. Quiet active work is flagged, not declared hung.
Nothing extends the hard deadline. A 30-second grace reserved inside that
deadline allows abort, provider-idle confirmation and final metrics retrieval;
the enclosing native owner remains responsible for exact-tree termination.
Partial checkpoints never become final receipts simply because a process died.

An operator-only credit reconciliation validates an identity-bound final provider
aggregate against the quiescent launch and any retained partial lower bound.
The evidence and reason are appended as a `credit_reconciliation` source, outside
scientific source revisions. The original receipt remains immutable; equal
replays are idempotent and conflicts fail. Accounting uses one final amount per
invocation, across restarts and generations. Reconciliation is not a research
handoff, credit-limit increase, controller restart or permission to guess usage.

When final usage is unavailable, `estimate-credits` requires explicit operator
confirmation and a reason for one quiescent, executed, unknown Copilot invocation.
Its immutable `credit_estimate` source binds the launch and original receipt.
The budget retains the unknown record, separately exposes the additional estimated
amount, and blocks admission for any unknown invocation without an approved estimate.
An estimate cannot be below retained partial usage; it does not change the cap or
scientific revisions. Final reconciliation replaces it in accounting without deleting
its audit history. Remaining allowance with estimates is approximate, not a verified bill.

Observer envelopes reserve from a distinct campaign-scoped allowance in the
machine ledger; dashboards cannot spend the same reservation concurrently.
Observer SDK metrics are retained before session deletion and published in the
final consumer receipt before release. Unknown interrupted usage stays unknown.
Dashboard restarts preserve credit usage, unlike their per-process invocation
count. The setup interview remains outside both credit allowances.

Managed reasoning has an explicit resource request within the campaign envelope.
The generated 2,048 MiB allowance is a starter configuration, not a measurement
of every provider's requirements. Review both campaign and machine capacity.

Each launch carries a generation-scoped work identity, one-time grant and nonce,
frozen settings, a protocol version and a digest. An old token cannot release a
new allocation. Missing receipts never prove that an armed process did not run.
Recovery uses existing state and the persisted envelope, not current TOML.

The OS launcher is not the authoritative worker identity: Windows virtualenv
redirectors may start the actual interpreter as another PID. The worker claims
its nonce with its own PID/creation time before executing user code. A late
launcher diagnostic cannot override an already committed terminal receipt.

Native Windows payloads enter a named Job Object while suspended. The grant's
CPU set is applied to that job, including descendants; memory and kill-on-close
limits remain in force. Affinity is placement, not a CPU-time quota. WSL uses
guest-local affinity and monitored session RAM, not a claim that guest CPU IDs
identify host CPUs. Docker uses its frozen local image ID, read-only source and
helper mounts, and an explicit no-pull policy. A guest payload receipt does not
prove that a Docker container has exited; recovery checks the owned container.

Each user stream has a finite two-segment spool which continues draining after
truncation. Deadlines and cancellation remain effective under output pressure.
Execution exit and output-validator success are separate recorded outcomes.
Supervisor Python diagnostics are separately capped at 64 KiB per stream.
Windows file replacement/removal retries transient access/sharing denials for a
finite interval; persistent errors remain errors and never establish completion.

Provider adapters inspect model-free help before constructing an invocation.
Explicit unsupported model/effort flags are refused, and unspecified effective
values remain unknown. Copilot sessions disable self-update, remote export and
interactive questions. Windows batch shims must be configured as a direct
executable argv (such as Node plus its CLI script), avoiding shell re-parsing.
Claude retains the existing subscription-auth behavior rather than inheriting
`ANTHROPIC_API_KEY`. These ambient credentials are not serialized in envelopes.

Only a short packet/result-path bootstrap goes on the provider command line.
An accepted result requires a completed owned invocation and the exact turn and
packet IDs. Sandbox canaries consume their own reserved invocation within the
same resource grant as the subsequent operation. A failed canary cancels the
uninvoked remainder; main execution rechecks both control authority and policy.
Canary receipts are published only after validation, not merely on provider exit.

Source snapshots copy only explicitly selected project files and enforce the
snapshot allowance during copying. Source bytes and declared input pins are
rechecked before execution; guest helpers also check the actual guest input
paths rather than assuming a host check validates an alternate mapping.
Input hashes have an explicit finite `verification_bytes` allowance.

Requested `stable-consumption` streams a bounded exact file copy. Native
Windows holds a read lease while it is consumed; Docker exposes the prepared
copy through a read-only mount. Unsupported WSL/native-POSIX guarantees are
refused. This is not protection against an unrestricted same-user administrator.
Large inputs are neither copied nor hashed without an explicit applicable policy.

Collection, execution and validation have separate outcomes. Artifact capture
streams into a private temporary file, computes SHA-256 over those same bytes,
and publishes the complete immutable object only after copying succeeds. The
default `storage.capture_bytes` is 16 GiB per file; new preparations also freeze
a total retained-byte allowance of four times that setting (64 GiB by default,
including metrics). These are finite storage limits, not model-context limits.
Metrics retain their separate 256 KiB default and bounded numeric parsing.
Configuration identity includes the effective capture limit, so a new default
cannot collide with an existing immutable snapshot from unchanged TOML.
Collection checks destination space and configured watermarks before copying;
watermarks are soft admission checks, not cross-process disk reservations.
An exceeded new-work limit is an explicit collection failure, with a qualified
unmanaged reference if the output exists, never a fabricated retained digest.
Existing frozen work without the aggregate allowance keeps its original
per-file policy, including qualified unmanaged references for oversized files.

Reads verify the complete retained digest while buffering only the requested
byte page. CLI pages are at most 1 MiB; SDK artifact pages are at most 16 KiB.
Full exports stream to a new local file and never emit artifact bytes to stdout.
Dashboard downloads verify before headers, then stream without keeping a SQLite
read transaction open. Large retained files are downloadable regardless of the
small inline-image/report-preview limits. Recollection creates new observation
IDs and a new collection event; old citations keep their original bytes. A
missing artifact in the latest collection cannot inherit an earlier metric.

Result pages expose their complete attempt denominator and bounded observation/
metric coverage. CSV is an explicitly requested, byte-bounded, read-only snapshot
export and never a second write authority. Cumulative GPU hours and invocation
commitments are maintained transactionally, not rescanned from terminal history
at each admission.

Packets commit their exact bytes, event membership and sequence cutoff before
file publication. A committed packet is not launchable until the published file
matches its digest. Replays republish those bytes, including after a control
change; they do not rebuild today's context. Admission still checks today's
control revision before a provider can launch.

Every packet is at most 64 KiB with 64 event headers. Half the event selection
slots prioritize oldest pending evidence; remaining slots take oldest pending
events. Complete goal/protocol and active operator constraints are mandatory
separately from delivery. Oversized mandatory context fails explicitly.
Large event payloads have bounded previews and byte-paged, digest-qualified
retrieval; exact chunks include base64 to preserve split UTF-8 characters.

Accepted rationale/next-step previews and exact selected research revisions
are optional, not another source of authority. They are added after sealed
inventory and event delivery, within at most 12 KiB of additional working context
and the overall packet limit. Generation/authority and worker-charter qualification,
UTF-8 coverage, omitted previews, paging and exact source identities are explicit.
Historical focus is not current strategy; ordinary published heads are discovery
only. Selected revisions never follow a newer head. Final analysis uses sealed
focus and has no live discovery index. Current job/task facts and existing budget
accounting are read using the packet transaction, without probing processes or
entering the ledger. Process success, collection, evidence disposition and worker
integration remain distinct. This projection does not rewrite sources,
receipt digests or old frozen packets.

New handoffs optionally carry root `goal_progress` and `commitment`. The former
is a bounded, evidence-linked interpretation of original deliverables, carried
by an accepted handoff source head rather than a second memory store; omission
means unchanged, not cleared. Stale/rejected/worker handoffs cannot replace it.
It precedes local plan previews within optional context and is frozen at sealing.
The latter records decision relevance at branch/refinement/milestone boundaries,
without task-credit forecasts or a per-turn obligation. Default-valued optional
additions preserve acceptance of predecessor normalized capture digests, never
allowing nondefault content to be stripped from an owned capture.

With coaching enabled, evidence-qualified commitments can coalesce a fresh
non-stopping strategy episode. Its only verdict is `advise`; no stop authority,
automatic pivot or mandatory reflection is available. Original goals, accepted
trajectory and exact evidence remain the review context. Failed/stale reviews
are explicit terminal episode outcomes, not campaign blockers. Their source and
event kinds do not renew substantive-research or stopping-review allowances and
cannot independently schedule research. Genuine stop episodes retain their
separate two-review bound and stricter unresolved-review behavior.

A provider receipt pins the exact result bytes after owned-tree quiescence and
records the normalized handoff digest. Acceptance requires that successfully
owned result, exact packet membership and delivered/retrieved references. It
commits acknowledgements, rationale, journal source and next action together.
Human journal edits and post-completion result rewrites cannot stand in for it.
Journal pages are read-only projections over retained source IDs.

`research_complete` and CLI `research complete` share typed handoff fields,
owned ID binding and rolled-back acceptance preflight before immutable result
publication. Validation errors remain in-session and do not publish science or
acknowledge events. Final acceptance repeats all owned-receipt checks. Manual
file/preflight completion remains supported, and supervisor memos remain an
independent optional capture.

New root packets may advertise a separate optional supervisor-memo sidecar.
After quiescence, the provider captures its bounded parsed content, original
byte digest/length and normalized content digest in an independent receipt
field. It does not change `Handoff` or any existing handoff digest. Publication
uses the accepted-result transaction, root scope, exact turn/packet identity,
current authority and delivered/retrieved evidence. Memo validation errors
become retained reporting-status sources, not rejected scientific handoffs.
There is no post-acceptance filesystem dependency or publication recovery gap.

`sources`/`source_heads` retain immutable memo versions, request milestones and
reporting status without a schema migration. Coverage is bounded by the packet
request cutoff and actual delivery/retrieval, plus own-turn milestones; later
requests and unread pages remain pending. These source kinds are excluded from
substantive source revisions and do not schedule inference or renew coaching.
Old packets/receipts without memo fields remain valid. Runtime verification
accepts the exact predecessor helper manifest without `supervisor_memos.py`
only when the envelope does not advertise that feature; every original helper
hash is still checked, and no frozen file is replaced.

Operator directives retain exact bytes, scope and explicit supersession. Source
and archive pages have stable sequence cutoffs; lexical search scans at most 64
16-KiB prefixes and returns at most 20 matches with explicit searched coverage.
Zero matches do not establish that the archive contains no relevant evidence.
`source set --kind goal|protocol` commits a retained operator revision. A goal
set this way does not overwrite the goal file or reimport its unchanged older
bytes; a subsequent observed manual edit becomes a new attributed revision.
Manual `.labgoblin\journal.md` imports are retained notes, never owned handoffs.

Compaction is one owned, budgeted maintenance request, never an independent child
provider. Archive growth no longer authorizes automatic paid compaction: fresh
research packets already bound working context and expose exact retrieval.
Operator/researcher requests contain bounded source prefixes and the previous
derived summary, separately from mandatory authority and without duplicating
the full current handoff. New summaries are at most 4096 UTF-8 bytes; frozen
older packets retain their recorded limits. Unstarted automatic requests are
cancelled, not silently serviced after upgrade. Summaries retain source
IDs, prefix coverage and exact originals; shrinking bytes does not certify
semantic equivalence. Neither a failed nor a non-shrinking compaction retries
automatically at the same non-maintenance source revision. Maintenance events
do not wake a waiting researcher and create a paid retry loop.

`compact` queues a fixed request for the running controller (or the next `run`);
`compact --no-agent` only displays archive coverage. Explicit operator requests
can be serviced after closure at a quiescent safe point without reopening
research. They still obey elapsed/invocation/resource limits. A newer control
revision fences pending work; only a proven unexecuted cancelled request can be
explicitly reauthorized at the same source revision. Paused/no-agent operation
never starts a provider or canary.

Submission snapshots retain the generation, governing authority and source
revisions observed before copying. Research submissions use their owned
packet's source references. Authority changes during preparation, or before
queued work is armed, require explicit resubmission rather than silently
evaluating a different goal or claim.

A research handoff belongs to one exact turn and packet. It records a concise
summary, rationale, next step and disposition; evidence is assessed, excluded
with a reason, or deferred with a wake condition. A finalize request names its
stopping criterion. A separate journal append does not establish ownership.
Encoded packets and handoffs are bounded to 64 KiB and 16 KiB respectively.

Finalize seals attempt membership and authority versions before draining admitted
work. Queued work becomes explicitly unperformed; late, failed, invalid and
unresolved outcomes remain in the denominator. Immutable closure views retain
bounded paginated inventories, exact row revisions and all referenced sources.
Unknown ownership retains its grant and an incomplete inventory, not completion.

One additional final-analysis bundle is authorized at arm, not packet creation.
When ordinary admission seals a generation late in a controller cycle, continuous
execution advances to the next admitting cycle for that reserved analysis rather
than returning as idle. Pause/stop, blockers, no-agent and single-step policies
retain their normal precedence; no new research cycle or allowance is created.
Pausing a merely prepared final turn does not spend it. Final failure, inability
to prepare, disabled inference or insufficient allowance closes with an explicit
unassessed outcome, without retrying the analysis. A request for more research
closes as `needs_more_work`; only explicit reopen starts another generation.
A mechanically complete finalize handoff avoids the additional call.

Final handoffs reference the immutable view digest and explicitly consider every
listed observation except named, reasoned exclusions. All inventory pages must
have been delivered/retrieved by that owned turn. Page coverage is not a claim
that every artifact byte was read or that the interpretation is sound. New
constraints after sealing are visible follow-up/staleness facts; they neither
enlarge the old scientific cohort nor trigger another final-analysis cycle.

Reports use the same immutable source views without becoming closure decisions.
`report` queues an owned maintenance request; `report --no-agent` publishes a
deterministic inventory even when inference allowance is unavailable. Optional
`--select ID --selection-reason TEXT` selects evidence without removing other
attempts from the scoped denominator. Distinct explicit selections are separate
requests; repeated identical requests at the same source revision are coalesced.

HTML, Markdown and a streaming JSON-lines source manifest are published to a new
owned report directory. Each output has a finite 32 MiB default limit and a
retained digest. HTML includes a bounded chart from actual captured metrics and
at most eight small captured PNG/JPEG figures, with no external assets or model
HTML. Structured numeric claims must match selected captured observations;
prose is not automatically fact-checked. Publication failure can recover the
same already-owned result without another provider invocation. Later goal edits,
compaction and recollection cannot replace an older report's referenced sources.

The harness can check identities, accounting and explicit evidence coverage.
It cannot certify scientific truth, sound interpretation or adequate controls.
Trusted same-user execution is not a security boundary.

# Dashboard read projections

Brief / Evidence / Work / History are read-only projections, not a second
journal or task ledger. A page shares one campaign read transaction for its
state, budget and source/event cutoffs; shared-machine reads are explicitly
separate. Common attempt filters drive both counts and their destination lists.
Update checks compare recorded cutoffs and a stable control/work-state token,
not continuously changing elapsed time. They announce changes without replacing
the current page. Explicit browser-local checkpoints are campaign/generation
scoped and never write acknowledgements.

Exact observation views include execution, collection and validation context.
With a source-view ID, that context and membership come from the digest-verified
historical member, never today's attempt. Verification streams member bytes in
64 KiB chunks with a 16 MiB bound; larger members remain available through exact
source-view byte pages. Numeric metric previews have explicit count/byte limits.
Raw comparisons do not infer pairing, units, effects or scientific validity.

The report reader accepts only registered `report.md` output under the report's
owned directory. It verifies length and digest through one open stream while
retaining at most a 128 KiB page, with a 256 MiB verification bound matching
publication. Missing, escaping or changed outputs fail explicitly. Active HTML,
remote assets and arbitrary filesystem browsing are not enabled.

# Read-only observer ownership

Dashboard questions use a fresh empty-mode Copilot SDK session with only curated
record readers. History search exposes its exact source-prefix coverage, cutoff,
cursors, truncation, recorded time and retrieval time. Historical source and view
IDs never resolve to a newer summary. Zero lexical matches do not prove absence.
Views of captured observations expose bounded numeric metrics, not arbitrary
files, commands, environments, logs, datasets, MCP tools or research controls.
Optional question context is limited to 4 KiB of validated labels, local
read-only URLs, exact IDs and page cutoffs. It is retained with the question and
included in retry identity, but confers no authority: tool reads resolve pinned
IDs and identify current state separately. Historical observation citations
carry the source-view ID; source-view member citations retain the member and
byte-page offset rather than silently pointing to current experiments.

The SDK runs **inside** an owned native payload; on Windows its entire child
tree inherits the grant's Job Object and CPU placement before executing code.
The HTTP server does not spawn an unaccounted SDK runtime. A separate observer
consumer requests capacity in the campaign's recorded compatible machine ledger,
without creating that ledger, binding it or writing the research database.
Waiting is visible and cancellable. Its armed authorization, conditional worker
claim and terminal receipt live in the machine ledger. Missing receipts retain
capacity; `machine reconcile` can ingest matching late receipts.

`[dashboard.chat]` is enabled by default, with inference only on an explicit
question. `enabled = false` disables it; CLI `--chat`/`--no-chat` override the
file setting. Missing SDK dependencies leave ordinary pages usable with an
explicit unavailable reason. Invalid configuration disables chat. It accepts `enabled`, `model`,
`reasoning_effort`, `cli_path`, positive `timeout_seconds` (5-600), `cpus`,
`memory_mb`, and positive `max_invocations`. The starter resources are 1 CPU and
2048 MiB, not a measured SDK requirement. The default allowance of 20 is scoped
to this dashboard process, not research generations, tokens or monetary spend.
No silent retries, refresh-triggered inference or research-turn charges occur.
Transport files are temporary and removed after verified shutdown. An uncertain
crash may retain bounded transport files for recovery; chat restoration across
server restarts is not provided.
## Explicit provider acceptance

Ordinary pytest runs use provider doubles, including the complete three-turn
late-replication sequence. `python -m tests.copilot_acceptance --root NEW_PATH`
also runs that sequence without real inference.

The separate `--live` option requires explicit authorization for up to four
real invocations: two research turns, one final assessment and one SDK observer
question. It creates a durable call ledger outside the synthetic campaign,
records each research launch before spawning, limits research to three slots,
and reserves the fourth for the observer. Launch uncertainty still spends a
slot. No retries, sandbox canaries, report or compact calls are enabled. Keep
the result, envelopes and ledger; do not rerun live validation to hide a failure
or exceed the authorized aggregate allowance.
