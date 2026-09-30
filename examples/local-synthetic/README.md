# Synthetic mean-shift study

Copy this directory into a new, dedicated project before running. The example
uses generated `[2, 4, 6]` values, no private data, dependencies, network or GPUs.

```powershell
xgenius init --agent copilot
xgenius machine configure --cpus 2 --memory-mb 4096 --headroom-mb 2048
xgenius batch-submit --file batch.json --json
xgenius run --no-agent --json
xgenius results --json
xgenius report --no-agent --json
xgenius dashboard --open-browser
```

These operations make **no model calls**. Inspect the baseline/replication means
of 4 and shifted mean of 5. Process completion and a deterministic report are
not a model-assessed research closure. Stop this mechanical run with `xgenius
stop`, or run `xgenius run` to explicitly authorize the configured research
provider to assess the evidence within its reviewed budgets.

For an autonomous exercise from fresh state, omit manual batch submission and
start `xgenius run`; the research goal asks the agent to submit that batch.
Review its provider/model/effort and resources first.

To exercise a prepared WSL/Docker runner, add it to `xgenius.toml` following
`docs/local-research.md`, then change each manifest's `runner` before submission
(or give changed requests new keys). Native Python is selected by init; guest
Python must already exist. Never pull an image or install into shared
environments implicitly. The optional Dockerfile can be built explicitly with
`build --runner container --context . --include experiment.py`; the approved
`python:3.11-slim` base must already be present.

This is a runtime demonstration, not a statistically interesting experiment.
Only the fixed inputs and stated arithmetic claim are supported by its evidence.
