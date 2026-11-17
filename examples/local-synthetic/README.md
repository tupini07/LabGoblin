# Small local example

This deterministic example is for understanding the files and launch path, not
for measuring research quality. No dataset, GPU or third-party experiment
dependency is needed.

To run just the experiment without inference:

```powershell
python experiment.py --offset 0 --output artifacts\baseline.json
python experiment.py --offset 0 --output artifacts\replication.json
python experiment.py --offset 1 --output artifacts\changed.json
```

The three means are 4, 4 and 5. These are ordinary commands/files; there is no
experiment manifest or special output environment variable.

To let the researcher investigate the question, install/authenticate Copilot
explicitly and choose a spending limit. From this example directory, copy its
scientific inputs into a fresh campaign's research subdirectory:

```powershell
New-Item -ItemType Directory .\my-campaign\research
Copy-Item .\experiment.py, .\research_goal.md .\my-campaign\research
labgoblin init .\my-campaign --credits 300
labgoblin run .\my-campaign
```

The copied `research\research_goal.md` is preserved. The separate `operator`
folder contains the selectable Copilot operator profile. Initialization is model-free;
`run` **uses paid inference**. Defaults are Sol/high research with Astra/high coaching,
on-demand advice/claim review and closing criticism, all in the same spending pool.
Use `init --no-coach --no-consultant` for research alone. The three-hour advisory
check-in interval exceeds the default 90-minute execution deadline; longer runs
can reach it without changing their spending cap. Inspect `report.md`, `artifacts`
and `.labgoblin\runs` inside research, or run `labgoblin status .\my-campaign`.

The installed-wheel qualification uses a fixed local SDK protocol double and
does not authenticate or call a real model:

```powershell
python -I tests\installed_smoke.py --root NEW_EMPTY_DIRECTORY
```

Run that command using the separately installed wheel's interpreter, not an
editable source installation.
