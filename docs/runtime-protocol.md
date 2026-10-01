# Local runtime contract

LabGoblin 2 uses configuration, campaign database, machine-ledger and worker
protocol version 3. These versions are checked independently. Old formats are
not migrated: initialize a fresh local campaign. An incompatible existing
machine ledger must not be replaced while old work may still be running.

Only LabGoblin command, import, environment and default path names are supported.
Helper manifests must explicitly bind the `labgoblin` namespace and the complete
file set into their identity; every source/bootstrap hash is verified. Missing
or old namespaces are rejected, not interpreted as a compatibility format.
No new helper is substituted for an admitted frozen one. Windows Job Object/
container names, Docker ownership labels/build tags and sandbox markers use
LabGoblin names. SQL application IDs and schema versions are unchanged.
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

`campaign.max_seconds = 0` and `campaign.max_invocations = 0` explicitly mean
unlimited for those dimensions. Per-operation deadlines, resources and storage
limits remain positive and finite. Zero GPU-hours means no GPU work. Invocation
accounting includes campaign research, retries, maintenance, final analysis and
model-executing canaries, not internal API calls or monetary cost. Observer usage
is separate.

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

Requested `stable-consumption` prepares only a bounded small exact copy. Native
Windows holds a read lease while it is consumed; Docker exposes the prepared
copy through a read-only mount. Unsupported WSL/native-POSIX guarantees are
refused. This is not protection against an unrestricted same-user administrator.
Large inputs are neither copied nor hashed without an explicit applicable policy.

Collection, execution and validation have separate outcomes. Small evidence is
captured once, with parsing, digests and retained downloads derived from those
bytes. Large mutable artifacts expose a qualified current reference, not a
fabricated hash or exact download. Recollection creates new observation IDs and
a new collection event; old citations keep their original bytes. A missing
artifact in the latest collection cannot inherit a metric from an earlier one.

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
events. Active operator constraints and the complete current rationale are
mandatory separately from delivery. Oversized mandatory context fails explicitly.
Large event payloads have bounded previews and byte-paged, digest-qualified
retrieval; exact chunks include base64 to preserve split UTF-8 characters.

A provider receipt pins the exact result bytes after owned-tree quiescence and
records the normalized handoff digest. Acceptance requires that successfully
owned result, exact packet membership and delivered/retrieved references. It
commits acknowledgements, rationale, journal source and next action together.
Human journal edits and post-completion result rewrites cannot stand in for it.
Journal pages are read-only projections over retained source IDs.

Operator directives retain exact bytes, scope and explicit supersession. Source
and archive pages have stable sequence cutoffs; lexical search scans at most 64
16-KiB prefixes and returns at most 20 matches with explicit searched coverage.
Zero matches do not establish that the archive contains no relevant evidence.
`source set --kind goal|protocol` commits a retained operator revision. A goal
set this way does not overwrite the goal file or reimport its unchanged older
bytes; a subsequent observed manual edit becomes a new attributed revision.
Manual `.labgoblin\journal.md` imports are retained notes, never owned handoffs.

Compaction is one owned, budgeted maintenance request, never an independent child
provider. Automatic compaction becomes eligible after 32 KiB of newly indexed
handoff/note text. Its packet contains bounded source prefixes and the previous
derived summary, separately from mandatory authority. Summaries retain source
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
