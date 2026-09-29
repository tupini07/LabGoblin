"""Shared non-interactive agent launcher for research tasks."""

import ntpath
import os
import subprocess

from xgenius.config import XGeniusConfig, get_project_dir


def run_agent(
    config: XGeniusConfig, prompt: str, *, capture_output: bool = False,
) -> subprocess.CompletedProcess:
    """Run the configured CLI with a prompt in the research project directory."""
    if config.local:
        from xgenius.agent_policy import run_local_agent
        return run_local_agent(config, prompt, capture_output=capture_output)
    command = config.watcher.command_args()
    env = os.environ.copy()
    executable = ntpath.splitext(ntpath.basename(command[0]))[0].lower()
    if executable == "claude":
        # Keep subscription auth for Claude without changing the parent or Copilot.
        env.pop("ANTHROPIC_API_KEY", None)
    return subprocess.run(
        [*command, "-p", prompt],
        cwd=get_project_dir(config),
        env=env,
        capture_output=capture_output,
        text=True,
        encoding="utf-8",
    )
