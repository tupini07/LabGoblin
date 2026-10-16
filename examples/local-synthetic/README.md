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

To let the researcher investigate the question, copy this directory to a fresh
workspace, install/authenticate Copilot explicitly, and choose a spending limit:

```powershell
labgoblin init . --model auto --credits 300
labgoblin machine configure --cpus 2 --memory-mb 4096 --headroom-mb 2048
labgoblin run .
```

The existing `research_goal.md` is preserved. Initialization is model-free;
`run` **uses paid inference**. Add `--coach` at initialization to include a stopping
review in the same spending pool. Inspect `report.md`, `artifacts`,
`labgoblin status .` and the immutable execution directory under `.labgoblin\runs`.

The installed-wheel qualification uses a fixed local SDK protocol double and
does not authenticate or call a real model:

```powershell
python -I tests\installed_smoke.py --root NEW_EMPTY_DIRECTORY
```

Run that command using the separately installed wheel's interpreter, not an
editable source installation.
