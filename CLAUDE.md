# CLAUDE.md

This file provides shared guidance to Claude Code and GitHub Copilot CLI when working with code in this repository. Both CLIs load `CLAUDE.md`.

## Project Overview

xgenius is a local-first autonomous research harness for Claude Code or GitHub Copilot CLI, with native, WSL2 and local Docker execution and a preserved legacy SLURM workflow. Trusted agent execution is not a sandbox. See `docs/local-research.md`.

## Build & Install

```bash
pip install -e .           # editable install for development
pip install .              # standard install
```

Dependencies: `rich`, `markdown-it-py`, `paramiko`, `scp`, `tomli_w`, `psutil`, and Windows-only `pywin32`. Requires Python 3.11+ (`tomllib` in stdlib).
The optional `dashboard-chat` extra installs the pinned Copilot SDK for the read-only dashboard observer; ordinary dashboard use does not require it.

## Running Tests

```bash
python -m pytest tests/ -v                    # all tests
python -m pytest tests/test_safety.py -v      # safety tests only
```

## Architecture

**Config-driven**: Everything starts from `xgenius.toml`. New local projects configure named runners, campaign limits, declared inputs and agent settings. Legacy projects retain cluster definitions, SLURM limits and watcher settings.

**Two state systems**:
- **SQLite DB** (`.xgenius/xgenius.db`) — automated operational state (job statuses, walltimes, exit codes). Updated by the watcher every cycle.
- **Research Journal** (`.xgenius/journal.md`) — the agent's persistent research memory. Written by the agent, read at the start of every session.

**Module layout:**
- `xgenius/local_config.py`, `local_cli.py` — explicit schema v2 local contracts and command routing; absent schema version stays SLURM
- `xgenius/state.py`, `campaign.py` — migration-backed attempts/events/turns and campaign lifecycle
- `xgenius/scheduler.py` — shared per-user CPU/RAM/GPU reservations; unknown liveness retains capacity
- `xgenius/workspace.py` — explicit source snapshots, declared inputs, registered artifacts
- `xgenius/backends.py`, `worker.py`, `payload.py` — owned independent native/WSL/Docker execution; guests write receipts, never host SQLite
- `xgenius/agent_policy.py`, `agent_worker.py` — bounded provider sessions, maintenance serialization, isolated optional sandbox preflight
- `xgenius/cli.py` — Unified CLI (argparse with 25+ subcommands, all support `--json`)
- `xgenius/config.py` — TOML config loading, validation, dataclasses, run ID management
- `xgenius/agent.py` — shared non-interactive launcher using `[watcher].trigger_command` for watch, report, and compact
- `xgenius/db.py` — SQLite DB for operational state (jobs table, hypotheses table, state sync)
- `xgenius/safety.py` — `SafetyValidator`: resource limits, command allowlist, path containment, shell injection detection
- `xgenius/ssh.py` — `SSHClient`: structured SSH/SCP/rsync operations via subprocess, returns `SSHResult`
- `xgenius/jobs.py` — `JobManager`: job submission, status checking, cancellation, SLURM log pulling, completion detection
- `xgenius/journal.py` — Simple append-only markdown research journal
- `xgenius/results.py` — Results bank: two-table CSV system (experiments + hypotheses)
- `xgenius/container.py` — `ContainerManager`: step-by-step Docker→Singularity build with structured output
- `xgenius/watcher.py` — Background daemon: polls for `.done` markers, syncs DB from squeue, pulls results + logs, triggers a fresh configured-agent session
- `xgenius/templates.py` — SBATCH template loading, `{{PLACEHOLDER}}` rendering, trap-based completion epilog
- `xgenius/dashboard.py`, `static/dashboard.{css,js}` — read-only loopback research dashboard; Markdown rendering disables raw HTML and all assets are packaged locally
- `xgenius/dashboard_data.py`, `dashboard_chat.py`, `static/dashboard-chat.js` — opt-in, on-demand Copilot observer with curated read-only evidence tools; no research-session control, arbitrary files/commands, or campaign-state writes

**Safety enforcement**: Local resource reservations and owned process supervision do not constrain an unrestricted agent. Legacy SLURM operations use `SafetyValidator`, containerization, and scheduler limits. Remote paths use POSIX semantics even on Windows.

**Per-project state**: `xgenius init` creates `xgenius.toml`, `research_goal.md`, and `.xgenius/`. Local projects add per-attempt snapshots/receipts and agent-turn logs. Legacy cluster projects also use templates and SLURM logs:
- `xgenius.db` — SQLite operational database
- `journal.md` — research memory
- `DEBUG.md` — error log for human review
- `templates/` — customizable SBATCH templates
- `slurm_logs/` — locally pulled SLURM .out/.err files
- `batches/` — archived batch submission files
- `run_id` — unique run identifier for scoping jobs

## Key Patterns

- All CLI commands support `--json` for structured output (critical for LLM consumption)
- Local experiment manifests can record `hypothesis_description` alongside `hypothesis_id`. Use `db.hypothesis_statement()` to distinguish real statements from legacy ID/submission placeholders; dashboard journal context never backfills research records.
- Safety validation happens before every remote operation in `jobs.py` — the LLM cannot bypass it
- Job IDs are captured from `sbatch` stdout and tracked in the SQLite DB
- SBATCH scripts get a trap-based completion epilog that writes `.done` marker files on the cluster
- The watcher daemon polls for markers, syncs DB from squeue, pulls results + SLURM logs locally, and triggers a fresh agent session per completion batch
- `xgenius init --agent copilot` selects `copilot --allow-all`; plain `init` keeps Claude as the default. New projects are local. `--backend slurm` selects legacy init; legacy projects switch agents via `[watcher].trigger_command`, local projects via `[agent].command`.
- Each run has a unique ID (xg-XXXXXX) that scopes SLURM job names and prevents old jobs from interfering
- New SLURM logs are pulled to `.xgenius/slurm_logs/{cluster}/{hypothesis_id}/{experiment_id}/`; unambiguous legacy logs remain readable.
- Project-local SBATCH templates in `.xgenius/templates/` take priority over package templates
- `xgenius compact` spawns the configured agent to intelligently compact the research journal — reducing size while preserving all essential context (findings, hypothesis statuses, decisions, human directives, next steps). The original journal is backed up before replacement. Call this when the journal grows large and starts consuming too much context. Works with `--json` for programmatic use.
