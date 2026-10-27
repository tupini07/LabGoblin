# Research and stopping review

The researcher owns scientific choices. LabGoblin supplies operational boundaries
and a short charter, not a state machine for hypotheses or a definition of
scientific success.

## One ordinary conversation

`research_goal.md` states the question and relevant constraints.
`.github\copilot-instructions.md` contains short working guidance. The first
prompt includes the reviewed goal and the existing interpreter path.

The normal Copilot SDK profile provides ordinary coding, file and command tools.
The conversation persists through experiments, interpretation and any automatic
stopping follow-up. There are no framework-authored per-turn packets,
scientific publication tools, typed handoffs, acknowledgment ceremonies or a
separate final-analysis model.

The charter asks for a useful baseline, discriminating experiments, separation
between development and final evaluation, retained failures/negative evidence,
reproducible commands, concise working notes and an honest report. The researcher
can challenge its initial framing, revise a method and pursue prerequisites.
It should reconsider the original goal at meaningful milestones, without
inventing task-cost forecasts or performing ritual reflection calls.

When stuck, it considers materially different approaches rather than only more
seeds or tuning. Existing `research-notes.md` retains promising untried ideas,
what each could clarify, and why remaining leads are infeasible or no longer
promising. This is a small revisable working list, not a new tracker, mandatory
method count or exhaustive inventory. A frozen evaluation must stay unchanged;
it does not prohibit a new, explicitly labeled investigation.

Known-answer or positive controls, where feasible, qualify a search/evaluation.
Their failure exposes a method limitation, not impossibility of the original
goal. The researcher investigates that limitation or a different approach rather
than treating the resulting negative report alone as sufficient reason to stop.

Normal provider context management remains provider behavior and belongs in its
usage totals; LabGoblin schedules no separate compaction or summarizing agent.

Optional `coach_model`, `closure_critic_model`, `advice_model` and
`claim_review_model` can provide different perspectives in fresh review sessions.
Each defaults independently to the campaign model, not the requesting model.
They do not rotate or replace the persistent researcher; closure adjudication
uses a fresh session with the configured coach model. All retain the common reasoning
effort, original permissions, time limits and single spending pool. Model diversity
alone is not independent scientific validation or demonstrated research improvement.
See the operating guide for selection checks, attribution and initialization options.

## A coach at the stopping point

With `coach = true`, a normal final response is a proposed stopping point. A fresh
empty-mode coach receives the original goal and that response. It can page through
workspace evidence using only `labgoblin_read_evidence`,
`labgoblin_list_evidence` and `labgoblin_review_decision`. Their names are distinct
from built-in Copilot tools, so SDK dispatch cannot select an unrelated reader.
It has no shell, writes, research-session attachment, arbitrary external file
access, unrestricted additional agents or budget-changing tool.

The coach gives a short reason and explicitly describes evidence inspected and
limitations of its review:

| Decision | Effect |
|---|---|
| `accept` | Provisional initially: supply one closing challenge in the same session. After a follow-up, finish with supported resolution or exhaustion. |
| `continue` | Ask a concrete, goal-relevant unresolved question. The researcher chooses how to address it in the same session. |
| `needs_operator` | Require the exact human question and permission/input/scope dependency, with reasons in-scope alternatives cannot resolve it. Stop immediately. |
| `checkpoint` | Preserve unfinished research at a host-checked deadline boundary or an evidenced repetitive stopping discussion; produce `checkpointed`, not scientific completion. |

The initial review establishes a baseline. Every follow-up review compares new
evidence with the previous review using `progress` (`meaningful` or `none`) and
a concise `progress_reason`. Meaningful progress resets the consecutive stalled
count; otherwise it increments. At `max_stalled_reviews` (default 10), further
pushback ends at an operational checkpoint. The ceiling is not global across
all reviews, and resets never renew the deadline, credit pool or permissions.
The coach can recognize decisive negative evidence or useful prerequisites as
progress; mere file churn, report polish or renamed questions do not count.
This remains an attributed, fallible judgment rather than a new science database.

Initial acceptance uses the first continuation for a neutral, evidence-grounded
closing challenge. The first coach's acceptance/reason is not presented as an
endorsement to the researcher. The challenge reconnects to the original goal,
failed controls, important untested assumptions and the strongest credible
remaining investigation. The researcher can do useful work or explain supported
stopping without inventing another method. A genuine operational limit instead
requires an honest partial checkpoint, not a claim of scientific exhaustion.

A continuation can contain multiple experiments toward the original goal, not
just a report defending the proposed stop. The final coach receives the updated
answer and can inspect the new evidence, including the response to the strongest
remaining lead. Coach-requested continuation already uses this one slot: its
eventual acceptance never triggers another challenge, even after a progress reset.
Later concrete requests can continue the same researcher while limits permit.
Clear repetition can checkpoint early after two consecutive no-progress follow-ups.
A deadline checkpoint is allowed only within the final 60 seconds including
shutdown; the host checks the clock. A stopped execution is not an instruction
to create another one automatically.
Unavailable/failed review is not silent acceptance. Budget exhaustion never buys
a smaller fresh retry.
An unfunded or interrupted challenge/final review leaves an incomplete execution;
the initial approval is not a fallback completion. Coach-off behavior is unchanged.

The coach judges the decision to end research, not just the report's honesty.
It examines decisive evidence, failed controls, relevant notes and the strongest
credible untried alternative: what feasible next investigation could materially
change the answer, and why is it not being done? A proof must address the intended
goal, not only a researcher-chosen renderer, benchmark or method.

Supported goal completion, including a scope-matching impossibility proof, and
supported exhaustion of credible feasible avenues are legitimate stopping
reasons. The latter remains unresolved, not proved impossible. A cautious
negative report or self-selected scope limitation alone does not suffice.
Explicitly scoped operator goals still govern; the coach cannot broaden them.
It must not demand every conceivable idea, minimum method counts, speculative
busywork or endless tuning. More activity and more credits are not success metrics.
Hard spending, deadline and permission limits retain precedence; operational
stopping does not resolve the scientific question.

## One spending pool

All roles share the project's cumulative cap. The first researcher reserves the
provider's 30-credit minimum for a stopping review. If sufficient observed budget
remains, the first review leaves the minimum for a useful continuation and final
review. Otherwise it uses the available review allowance and may have to leave an
unfunded follow-up to the operator.

Before a continuation, the idle researcher's existing cumulative SDK ceiling is
updated to its own already-reported usage plus only the remaining shared pool,
less the next review minimum. This does not create another researcher, reset
usage or reopen an exhausted session. Every ceiling and settled session total is
retained. Soft overshoot is counted and can prevent another role's admission.

## A colleague on demand, not a second manager

With `consultant = true`, both the researcher and coach additionally receive
`labgoblin_consult`. The researcher can ask about related work, assumptions or
alternative approaches. A coach can ask about a consequential stopping doubt;
consultation is not mandatory on each review and adds no stopping-decision power.
An adviser supplies possibilities and evidence, not an experiment prescription.
Its source-linked artifact is made available to the researcher on a follow-up.

Each call creates a fresh read-only adviser with the original goal, scope
restrictions, a specific question and selected evidence pages. It can inspect
ordinary workspace files and public literature but cannot run experiments,
edit notes, change the goal, call a coach or create more agents. The caller
waits and continues its existing conversation afterward. No full transcript,
parallel research branch, standing sidecar memory or competing plan is created.

The answer is a concise cited brief: relevance and assumptions, promising
alternatives, useful discriminating checks and retrieval/coverage limitations.
Metadata or abstracts are not full-paper inspection. Finding no useful new lead
is legitimate; generating more ideas is not itself progress. The researcher
adopts or rejects suggestions and owns the only canonical research notes.

Advisers share a bounded reserve inside the same project pool; their actual
spending and failures remain attributed. Limits are allocated before the caller
starts, never by giving a nested session overlapping money during a tool call.
See the operating guide for the reserve calculation and exact time/read bounds.
Unknown final usage stops inference; a settled failed consultation is an explicit
tool error, not a fabricated success or another automatic research continuation.

## Human ideas without a second research manager

The existing campaign chat can discuss evidence and prepare operator idea drafts.
Only a human Publish action admits exact captured files. The same researcher gets
file pointers at startup or a stopping boundary; large documents are read on
demand, not pasted into context. It may investigate, recognize prior coverage,
reject or defer suggestions, explaining why in its normal answer and notes.
The coach and independent critic see the same captured inputs and replies.
Publication and a retained response are not scientific progress.

A publication arriving during a closing review must be considered before
discretionary completion. The host serializes final closure with publication;
post-cutoff ideas wait for a later explicitly authorized execution. This also
applies without coaching. Suggestions never restart research automatically or
expand the goal, permissions, deadline, budget or stalled-review allowance.

## What this does not guarantee

An optional **adversarial closure critic** changes the objective of a fresh
reviewer: find a substantiated case against stopping, rather than collaborate
on defending the report. Enable `closure_critic` with coaching and consultation.
It is host-triggered before discretionary final acceptance, not every experiment
or ordinary continuation. The preliminary coach approval is withheld from it.
The critique first returns to the persistent researcher for one investigation,
correction or evidence-based rebuttal. A fresh read-only adjudicator then assesses
the critique, response and actual artifacts, using the configured coach model
without the proposing coach's approval in context. This replaces the proposing
coach's second turn, rather than adding another committee or experimental branch.
Required report corrections must be applied before acceptance; explicitly
outstanding corrections force continuation. Product non-release and scientific
termination are separate decisions. The critic has no veto.

A useful objection links an original-goal consequence to evidence and a feasible
discriminating continuation. A speculative objection, an already answered control,
or a demand to solve every gap in one step is not enough. Conversely, repairing
an overclaim by narrowing the report does not alone answer an unmet original
goal. Prior briefs remain available to avoid repeated objections. Critic prose
does not reset the stalled-review counter. Failed/unfunded criticism leaves an
explicit incomplete outcome rather than falling back to preliminary approval.
All role costs stay in the existing pool and deadline. The critic is a skeptical
research colleague, not a psychological reviewer: it has up to two hours for
evidence-grounded challenge, no separate credit/read/image-count cap, and a
visible response deadline. Individual payloads stay bounded. Ten minutes each
are reserved for researcher response and adjudication; the response can use more
available time while leaving the judge's reserve. The coach shares the campaign deadline without a
separate time cap. These are operational ceilings, not targets for longer work.

The coach is a process reviewer, not an independent execution of the benchmark.
Its `coverage` text is an attributed claim, not a machine-verified scientific
inventory. Reports and normal working files can be wrong. A robust evaluation
therefore checks retained outputs independently, includes failures and unknown
spending, and compares the complete goal rather than only a convenient proxy.

Evaluate stopping changes on mixed cases: failed positive controls with a useful
alternative, a narrow result that leaves an original requirement unmet, genuinely
answered goals or applicable impossibility proofs, supported exhaustion with an
unresolved result, and genuine permission/time/spending boundaries. Score useful
follow-through and justified stops, false continuation, evidence quality and
total role cost, not extra turns or more persuasive closing prose. Scripted
provider/native tests establish routing, ownership and accounting, not improved
scientific judgment. Paid behavioral comparisons require separate authorization.

Across separate explicit `run --resume` executions, continuity comes from retained
files and notes; there is no promise to resume an interrupted provider session.
The prior execution's snapshots, receipts, reviews and event logs remain intact.
