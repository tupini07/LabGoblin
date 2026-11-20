# LabGoblin

**Give an autonomous researcher a question, a workspace, and room to investigate.**

LabGoblin runs a persistent Copilot researcher on your machine. It plans and runs
experiments, examines evidence, and keeps research notes while the harness manages
execution, spending, and review.

## Quickstart

You need [uv](https://docs.astral.sh/uv/getting-started/installation/), Git, and an
installed, authenticated Copilot CLI. LabGoblin supports native Windows and Linux
with Python 3.11+. The examples below use PowerShell.

### 1. Install

```powershell
uv tool install --python 3.13 "git+https://github.com/tupini07/LabGoblin.git@main"
uv tool update-shell
```

Open a new terminal afterward. This makes `labgoblin` available on your PATH,
using an isolated tool environment rather than your system Python or a campaign's
scientific environment.

### 2. Create a campaign

```powershell
labgoblin init .\my-research
```

Initialization creates:

```text
my-research\
  research\   Research goal, configuration, scientific files, Git, and .venv
  operator\   Copilot operator profile, handoff, and your review notes
```

**Initialization calls no model and starts no research.** It creates an editable
goal scaffold, not a conversational setup wizard.

### 3. Describe the question

Edit `my-research\research\research_goal.md` with the goal, relevant context,
available inputs, and genuine constraints. Let the researcher choose its methods
and experiments. Replace the scaffold before running.

Review `my-research\research\labgoblin.toml` for models, budget, and execution
limits. Already have a brief? Use `init --goal-file .\question.md` when creating
the campaign.

### 4. Start research

```powershell
labgoblin run .\my-research
```

This authorizes the configured research execution and its credit allowance.
The command runs in the foreground; Ctrl+C requests cancellation.

## Prefer a conversational operator?

After initialization, start a fresh Copilot CLI session in the operator folder:

```powershell
Set-Location .\my-research\operator
copilot --agent labgoblin-operator
```

Ask it to help prepare the goal and settings, review progress, or start the
campaign when you are ready. Its handoff contains the exact executable and
campaign paths. For an authorized background start, it is instructed to use a
persistent process, retain logs, and verify researcher activity.

The operator helps you manage the campaign; it is not a second researcher.
Selecting it does not start research, spend campaign credits, or enable unattended
monitoring. See the [operator guide](docs/local-research.md#operating-through-a-normal-copilot-cli-session).

## What happens during a campaign?

- **One researcher owns the investigation.** It works with ordinary code,
  experiments, notes, and artifacts. Reports are checkpoints, not automatic
  reasons to stop.
- **Reviewers challenge the reasoning.** Coaching, on-demand scientific advice,
  and independent closing criticism are enabled by default. Feedback returns to
  the researcher, which remains responsible for the scientific work.
- **You can pause for review.** Optional scheduled meetings request a cooperative
  checkpoint and hold the campaign until you explicitly continue it.
- **Useful procedures can become skills.** Optional extraction proposes reusable
  campaign-local skills. The operator agent reviews them and gives you a
  recommendation; approved changes activate only through a reviewed continuation.

A completed run or an accepting reviewer is **not independent scientific
validation**. Inspect the evidence before relying on a result.

## Defaults worth knowing

| Setting | Default |
|---|---|
| Research model | GPT-6.1 Sol, high reasoning effort |
| Review and advice models | GPT-6 Astra, high reasoning effort |
| Research credit allowance | 1,000-credit soft cap, shared across roles and resumes |
| Execution deadline | 24 hours, including reviews and shutdown |
| CPU and RAM limits | Unlimited; runs share local hardware |
| GPU access | None unless explicitly admitted with `--gpu UUID` |
| Scheduled meetings, skill extraction, full-workspace archives | Off; opt in when needed |

For example, select another researcher with `init --model gemini-4-carbon`.
Set `--credits unlimited` explicitly for an uncapped campaign. Changes to
`labgoblin.toml` apply to the next execution, not one already running.

The scientific `.venv` is separate from LabGoblin's installation. Research may
manage its own dependencies within the campaign's permissions. Native execution
is trusted, not a hostile-code sandbox.

## Everyday commands

Run these from outside the operator subdirectory, using your campaign's path:

| Command | Purpose |
|---|---|
| `labgoblin status .\my-research` | Read current state, configuration, and spending. |
| `labgoblin inspect .\my-research --open` | Browse retained evidence locally without model calls. |
| `labgoblin stop .\my-research` | Request cancellation of the campaign's owned execution. |
| `labgoblin run .\my-research --resume` | Start a fresh conversation from retained files, preserving cumulative spend. |

Scheduled review holds and pending skill proposals need explicit review bindings
when continuing; see the [operating guide](docs/local-research.md). Inspector
chat is optional and uses a separately approved allowance.

## Learn more

- [Operating guide](docs/local-research.md): configuration, review meetings,
  skills, inspection, spending, and recovery.
- [Research and coaching](docs/research-loop.md): how the researcher and reviewers work.
- [Small local example](examples/local-synthetic/README.md): a minimal campaign.
- [Paired evaluation example](examples/evaluation/README.md): checks against an
  operator-supplied evaluator and baseline.
- [Runtime contract](docs/runtime-protocol.md): process ownership, frozen
  execution records, and accounting.

## Development

From a checkout, in a dedicated Python environment:

```powershell
python -m pip install -e . pytest
python -m pytest tests -q
```

Ordinary tests are model-free. Real provider evaluations require separate,
explicit authorization.

## License

MIT licensed. Copyright and attribution are preserved in [LICENSE](LICENSE);
the original software citation is retained in [CITATION.cff](CITATION.cff).
Historical reports and their generators under `examples` are archival material,
not runtime tools or validation of current LabGoblin behavior.
