# Optional paired evaluation kit

This known-answer example predicts `y = 2*x + 1`. A memorized lookup can pass the
two development cases and fail the two confirmation cases. The correct affine
candidate passes both. This demonstrates the protocol, not scientific discovery
or evidence that the harness improves research quality.

Keep the kit outside the research project. When copying an installed kit, omit
pip-generated `__pycache__` directories; captures require self-contained source
without excluded caches. Review its evaluator and dependency requirements.
New projects use their separate research `.venv` for both evaluator
sides; the researcher may install scientific dependencies there unless explicitly
restricted. LabGoblin itself downloads no packages. Initialization alone does not
run research:

```powershell
labgoblin init .\research --goal-file .\question.md --evaluation-spec (Resolve-Path .\examples\evaluation\spec.json).Path
Copy-Item .\examples\evaluation\candidate .\research\candidate -Recurse
```

For an existing project, set an absolute `evaluation_spec` path in TOML before
its next explicitly authorized execution. Omission keeps evaluation disabled.
Changing a kit during execution has no effect; the next execution captures a
new definition without changing old evidence or resetting case exposure.

## What the researcher can do

The ordinary researcher receives one execution tool:

```json
{"stage": "development", "candidate": "candidate"}
```

`candidate` is a relative, self-contained directory in research. The host captures
its files and runs the frozen evaluator separately on that capture and the frozen
baseline. A completed check returns its exact `run` and `check` IDs.

```json
{"stage": "confirmation", "source_run": "EXACT_RUN_ID", "source_check": "EXACT_DEVELOPMENT_CHECK_ID"}
```

Confirmation evaluates the **selected captured candidate**, not current mutable
files, under the same definition. It spends the next unused confirmation batch
before execution. Failed, cancelled and interrupted attempts cannot recover its
freshness. Development exposure also prevents those cases later becoming fresh
confirmation. Fingerprints ignore case IDs; relabeling cases does not reset them.

```json
{"stage": "regression", "candidate": "candidate", "source_run": "EXACT_RUN_ID", "source_check": "EXACT_CHECK_ID"}
```

Regression captures the current candidate and replays cases from a completed or
settled failed earlier check, with explicit source lineage. It can test a repair,
but is not independent confirmation. An incomplete/uncertain check is not a
settled source. Retain the discrepancy and its scoped lesson in normal research
notes; no additional journal, reporting prompt or agent is required.

Only the researcher executes checks. Other research roles get
`labgoblin_read_evaluation`; the inspector has equivalent read-only access.
Humans can browse **Paired evaluation checks** or run:

```powershell
labgoblin evaluations .\research --run EXACT_RUN_ID
labgoblin evaluations .\research --run EXACT_RUN_ID --check EXACT_CHECK_ID --offset 0
```

These commands never execute an evaluator or call a model. Follow `next_offset`
for more checks/cases. `completed` means a complete paired measurement, not that
the candidate passed. Pass counts and candidate-minus-baseline metric deltas
remain separate. Results do not promote code, certify claims, or decide stopping.

## Operator-owned protocol

`spec.json` declares the evaluator directory/entrypoint, baseline directory,
development file, ordered confirmation files, 1-8 named metric directions,
per-pair seconds including shutdown, and a per-capture byte limit no larger than
the campaign snapshot allowance. Paths are relative to the specification;
aliases, traversal and research-owned kit sources are rejected. Captures reject
excluded files such as credentials, runtime state, `inputs` and symbolic links.
Use dedicated directories, not a whole environment or repository.

Case files contain nonempty arrays of at most 256 records:

```json
[{"id": "example", "data": {"x": 3, "expected": 7}}]
```

Case IDs and data must be unique within a batch; development and confirmation
batches must have distinct data. The host passes only the selected batch to the
evaluator. Total frozen JSON is bounded to one MiB, even across multiple batches.
Case IDs have a 128-UTF-8-byte limit.

The host runs the captured entrypoint with the admitted interpreter:

```text
SCIENTIFIC_PYTHON -I -B CAPTURED_ENTRYPOINT REQUEST_JSON RESULT_JSON
```

For newly initialized projects this is the recorded research Python, not the
protected harness environment. Use `sys.executable` for candidate subprocesses
so they see the same dependencies. Legacy/manual projects without this recorded
environment retain the harness interpreter unless their operator-supplied
evaluator explicitly selects a separately authorized scientific interpreter.
Installing dependencies during research does not rewrite the frozen kit or
freeze those dependencies; retain their versions with the scientific artifacts.

Input has `version: 1`, the absolute captured `artifact` directory, `cases`, and
the declared `metrics`. The evaluator must create the result file:

```json
{"version": 1, "cases": [
  {"id": "example", "passed": true, "metrics": {"absolute_error": 0},
   "diagnostic": "expected=7; predicted=7"}
]}
```

Return every requested case exactly once, including failures, with exactly the
declared finite numeric metrics and an explicit boolean `passed`. Diagnostics
are bounded to 1000 UTF-8 bytes. Missing, duplicate, nonfinite or invented rows
fail the comparison. Baseline/candidate/evaluator capture changes and child-input
mutation also invalidate it. Isolation flags disable implicit local imports;
keep the script self-contained or explicitly resolve its captured helpers.

## Limits of this evidence

The evaluator and baseline must be independently appropriate to the real goal.
Same-build comparisons and retained failures cannot repair invalid reference
labels, a misleading proxy, inadequate coverage or a bad scientific question.
Fresh JSON fingerprints are an exposure safeguard, not proof of semantic
independence or statistical significance.

Native execution is trusted, **not an adversarial sandbox**. The researcher and
evaluator run as the local user; external cases are procedurally withheld, not
OS-secret. A hostile process could read or modify them. Do not put credentials
or private data in a kit, and use a real isolation boundary if your study needs
it. The interpreter, evaluator and baseline bytes are recorded; installed
dependencies, hardware and OS are not fully frozen.

All work stays inside the research execution's existing resources and deadline.
There is no new model allowance, automatic retry, scoring-driven agent team,
harness self-improvement or deployment mechanism.
