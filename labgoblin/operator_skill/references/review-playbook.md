# Evidence-led review and video playbook

Maintain attention through discovery and visible explanation, not constant
motion. The whole briefing should help the viewer understand the research,
not start with a demonstration and then revert to narrated slides.

## 1. Find the story in the evidence

Start from the original goal, approved guidance, previous review and the exact
covered executions/cutoff in `brief.json`. "Since the last review" may span
several days or runs. Separate new findings from inherited results, repeated
evaluations and unfinished work. Give a clear verdict about goal-relevant
progress, not an inventory of files or experiments.

Choose a few consequential investigations. For each, establish the question,
what the researcher actually tried, the observed result, the credible matched
control, and what the result changes about the assessment. Inspect the saved
numbers and decisive method details; do not merely paraphrase the researcher's
summary. Keep practical usefulness separate from benchmark fidelity. Review
recommendations are not new research instructions or permission to integrate.

Before polishing, find a small claim-linked evidence set in `sources.json`:

- The actual example figures, clips or saved outputs needed to explain the claim.
- Matching case-level results, case identities and input/output/reference roles.
- Aggregate comparisons, denominators, failures, exclusions and uncertainty.
- The registration, relevant method code and exposure history needed to judge it.

Use the captured set, not bulk source-code browsing as a substitute for results.
Check the manifest's omissions before promising a visual demonstration. If a
decisive image/table is absent, disclose the gap; do not silently read live
research, held inputs or external roots, or pretend an aggregate chart is a
case-level replay. A separately authorized future capture should prioritize
claim-linked visuals, matching tables and necessary methods before bulk code,
not simply raise capture limits. Never edit research notes to manipulate capture.

Select cases to answer a question, not just because they look impressive.
Explain selection (representative range, informative failure, extreme or
outcome-selected example). Prefer matched comparisons on the same case with
consistent scales, crop, orientation and masks. Pair favorable examples with
counterexamples when available, then show population evidence to establish how
far the examples generalize. Missing comparisons stay missing.

## 2. Storyboard questions and payoffs

Write the illustrated HTML assessment first so presentation cannot outrun the
findings. Then compress it into a roughly 3-5 minute briefing, usually 450-750
spoken words. Shorter is better than padding when little changed. Aim for two
or three case stories rather than a chronological recital.

While drafting, sketch each chapter's viewer question, supported answer,
source/case IDs, visual beats and narration cues. These can be working notes;
they are not new required fields in the version-1 storyboard.

A useful sequence, adaptable rather than mandatory:

1. Open on an actual result and a goal-relevant question; state the qualified
   verdict early instead of spending the opening on a long title card.
2. Show the problem and attempted solution through a concrete case. Introduce
   the input and what the viewer should notice before revealing comparisons.
3. Use a matched control or informative failure to test the tempting explanation.
   Reveal what changed our understanding, not just what score increased.
4. Return to the aggregate result, including its denominator and uncertainty,
   to show whether the case is typical, exceptional or still unqualified.
5. End with what is now useful, what remains unresolved, and the next decision
   or discriminating question. Advice is not an authorized experiment or restart.

Every chapter needs a question and a payoff, even a limitations or next-steps
chapter. Revisit the relevant evidence to explain a limitation; do not finish
with a long wall of caveat text. Honest negative results can be compelling.
Do not invent a dramatic reversal or force a success story.

## 3. Let visuals explain throughout

Give evidence most of the frame. Use large, readable crops rather than shrinking
a contact sheet beside a paragraph. Retain the full source and enough surrounding
context that the crop does not hide a failure. Put dense detail in the report.
Use short titles, a concise takeaway and stable role labels.

Vary the representation because the explanation changes: concrete example,
close-up, progressive annotation, side-by-side comparison, stepwise sequence,
then brief aggregate summary. Avoid repeating the same small-chart/text-card
layout for every chapter. A useful new focus every 8-15 seconds is a planning
heuristic, not a cut quota. Allow quiet holds when evidence needs inspection.

For each movement, name what it teaches. Reveal a comparator, highlight the
error mentioned in speech, or trace an observed sequence. Remove arbitrary
pans, zooms, transitions and decorative motion. Match the representation to
the campaign, for example:

| Research task | Possible explanatory visual |
| --- | --- |
| Spatial prediction or placement | Aligned input/prediction/reference/control, meaningful region highlights, recorded edits |
| Simulation or dynamics | Observed trajectory beside reference/control, with explicit time scale |
| Optimization or training | Saved checkpoints and error cases beside curves; a curve alone does not explain the result |
| Algorithm or systems research | Small worked trace from retained data, state transitions and measured outcomes |
| Mathematical investigation | Diagram of assumptions, a genuine counterexample or a labelled explanatory construction |

These are options, not a room-specific template or a requirement to animate.
No new fit, inference or experiment is needed to explain retained evidence.
Do not fabricate intermediate states by interpolating unrelated final outputs.
Distinguish on screen:

- **Recorded sequence:** actual retained stages; disclose missing stages or time compression.
- **Reconstruction:** deterministically redrawn saved outputs; identify the source
  and say what was reconstructed, rather than claiming a new experimental replay.
- **Explanatory reveal/illustration:** staged labels or a conceptual mechanism,
  not observed evolution or additional scientific evidence.

Keep a stable campaign palette, typography and visual motifs across reviews,
with consistent input/prediction/reference/control colors and textual labels.
Other campaigns should have distinct identities, not the same skin or arbitrary
new styling every review. Do not rely on color alone to explain meaning.

## 4. Write speech and visual cues together

Narration should direct attention and interpret the comparison: "Watch this
region; the control fixes the same error" is more useful than reading chart
labels. Use short spoken sentences, explain necessary terms and give inspection
time before the next conclusion. Speak only decision-relevant numbers, units
and caveats; keep full tables, identifiers and precision in the report.

Use the same checked values for report, charts, labels, captions and speech.
State consequential uncertainty aloud, not only in fine print. Do not say
"better" without explaining better on which outcome and against what control.

Use `frames` with `frame_sentences` for sentence-timed reveals as documented in
[the storyboard schema](storyboard.md). Indices refer to actual spoken sentences,
not guessed seconds. The renderer uses generated audio to time scenes; splitting
one long narration across evenly spaced frames can reveal the result too early
or leave the wrong image on screen. Check `timing.json` after rendering.
Use local Kokoro and `pronunciations` for troublesome acronyms or names.
Do not add Azure provisioning or another paid speech service by default.

## 5. Check understanding, not just encoding

Inside the review worker, finish the report, figures and storyboard; the host
performs speech and rendering after that worker returns. Do not start a parallel
renderer/sub-agent or change captured tooling to bypass this separation.
Customize operator-local tooling before a future authorized job captures it.

During authorized post-render review, inspect beginning/middle/end frames from
every chapter and the transitions. Check readable labels, unclipped crops,
source links, fair comparisons, timing, captions and consistent numerical claims.
Watch with narration to assess pronunciation, pauses and whether each reveal
arrives at the sentence that explains it. A contact sheet cannot establish
voice quality; successful decoding cannot establish understandable storytelling.
State exactly what was inspected or listened to and leave unassessed portions
explicit. Do not call a video "fully reviewed" after only skimming frames.

Ask of each chapter: what should the viewer notice, does the image actually
show it, what changes during the explanation, and why does it matter to the
goal? Revise repetitive passages or missing comparisons rather than add motion.
For a major style change, a short representative segment can reveal problems
before a full render, within the same authorized production scope.

All production stages, including revisions and cleanup within an attempt, share
the host's original absolute deadline (at most 90 minutes). It does not restart
per stage or when a helper is replaced. Do not start out-of-band work after
expiry; preserve finished written material and the truthful failure outcome.

Presentation-only revisions should reuse verified findings and captured sources
where possible, not automatically buy another review invocation. They still
need explicit scope and a bounded owned execution path; this playbook adds no
rerender command or new spending authority. Never edit a completed attempt,
captured tooling, receipt or historical report in place. Publish a new immutable
revision through supported tooling, retaining its predecessor, provenance,
coverage, costs, checks and limitations in the operator review journal.
