# Storyboard format, version 1

Write UTF-8 `storyboard.json` in the job directory:

```json
{
  "version": 1,
  "title": "Campaign review",
  "subtitle": "Progress since the previous review",
  "voice": "af_heart",
  "pronunciations": {"IoU": "intersection over union"},
  "scenes": [
    {
      "title": "One supported finding",
      "takeaway": "A concise, qualified conclusion",
      "narration": "Explain what the viewer is seeing and why it matters.",
      "image": "figures/comparison.png",
      "sources": ["sources/research/experiments/example/results.json"]
    }
  ]
}
```

Use 3-12 scenes. Each narration is plain spoken prose, not SSML/Markdown.
Use actual relative file paths inside the job, including for sources. At least
one source per scene is required. No absolute paths, URLs or `..` components.
Keep titles under 90 characters and takeaways under 180. Do not hard-code
runtime: the renderer uses the actual generated audio duration.

Each scene must have exactly one visual:

- `image`: a PNG/JPEG evidence figure; large labels and few elements work best.
- `frames`: 2-120 PNG/JPEG paths, shown in order across the narration. Use this
  for task-specific evolution, progressive annotations or before/after changes.
- `video`: a local captured MP4/WebM/GIF clip. The renderer fits its frames to
  the narration duration; label time compression and do not imply real-time speed.

For narration-aligned reveals, pair `frames` with optional `frame_sentences`:

```json
{
  "narration": "Here is the input. Now compare the prediction. The control explains the limitation.",
  "frames": ["figures/input.png", "figures/prediction.png", "figures/control.png"],
  "frame_sentences": [0, 1, 2]
}
```

The indices are zero-based spoken sentences, split after `.`, `!` or `?`
followed by whitespace. Use plain sentences; avoid period-bearing abbreviations.
Supply one strictly increasing index per frame, starting at zero. Each frame
starts with its actual synthesized sentence and stays until the next cue,
including pauses; sentence lengths need not match. Without this field, frames
retain the original evenly spaced timing. Sentence start times are retained
in `timing.json` for inspection. This is an explanatory reveal, not a claim
that these are recorded stages of an experiment.

Use captions such as "illustrative mechanism, not a new experiment" where
appropriate. Label selected cases as selected, and never imply their frequency
without population evidence. For source videos use only approved local clips.

Optional `highlights` annotate the visual with normalized rectangles:

```json
{"box": [0.2, 0.3, 0.25, 0.15], "start": 0.1, "end": 0.8}
```

Coordinates are fractions of the fitted visual: x, y, width, height. Start/end
are fractions of scene duration. Highlight actual explanatory regions, not
decorative random movement.

Narration, takeaways, labels and charts must agree on values, units, populations,
baselines and caveats. Use external calculations only on saved source results,
never on held payloads or by rerunning experiments.
