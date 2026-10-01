"""Model-free installed-wheel acceptance; invoke with isolated Python (-I)."""

import argparse
import importlib.metadata
import importlib.util
from importlib.resources import files
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import urllib.request


def run(root):
    import labgoblin
    from labgoblin.dashboard import DashboardServer
    from labgoblin.scheduler import ResourceLedger
    from labgoblin.state import State

    assert sys.flags.isolated, "Run this installed-package check with python -I"
    assert Path(labgoblin.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()), "Source import leakage"
    assert importlib.util.find_spec("xgenius") is None
    assert importlib.metadata.version("github-copilot-sdk") == "1.0.15"
    root.mkdir(parents=True, exist_ok=False)
    project, ledger = root / "campaign", root / "machine.db"
    example = Path(sys.prefix) / "share" / "labgoblin" / "examples" / "local-synthetic"
    shutil.copytree(example, project)
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("LABGOBLIN_", "COPILOT_", "GITHUB_TOKEN", "GH_TOKEN"))
                   and key != "PYTHONPATH"}
    environment.update(PYTHONUTF8="1", COPILOT_SKIP_CLI_DOWNLOAD="1", LABGOBLIN_RESOURCE_DB=str(ledger))

    def command(*args):
        result = subprocess.run([sys.executable, "-I", "-m", "labgoblin.cli", "--project", str(project), *args, "--json"],
                                cwd=root, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not result.stderr, result.stderr
        return json.loads(result.stdout)

    command("init", "--non-interactive", "--agent", "copilot", "--ledger", str(ledger))
    command("machine", "configure", "--ledger", str(ledger), "--cpus", "2", "--memory-mb", "4096", "--headroom-mb", "0")
    command("batch-submit", "--file", str(project / "batch.json"))
    command("run", "--no-agent")
    state = State.open(project / ".labgoblin")
    status = command("status")
    means = {item["experiment_id"]: item["metrics"]["mean"] for item in status["results"]["attempts"]}
    assert means == {"baseline": 4, "replication": 4, "shift": 5}, means
    report = command("report", "--no-agent")
    assert report
    with state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM invocations").fetchone()[0] == 0
        before = list(conn.iterdump())
    with DashboardServer(("127.0.0.1", 0), str(project / "labgoblin.toml"), chat=False) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            for route in ("/", "/jobs", "/resources", "/journal", "/reports", "/static/dashboard.js", "/static/dashboard.css"):
                with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}{route}", timeout=5) as response:
                    assert response.status == 200 and response.read()
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()
    with state.db.read() as conn:
        assert list(conn.iterdump()) == before
    machine = ResourceLedger(ledger)
    assert not machine.rows()
    with machine.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM consumer_runs").fetchone()[0] == 0
    assert state.campaign()["invocations"] == 0 and state.campaign()["controller"] is None
    for asset in ("dashboard.js", "dashboard-chat.js", "dashboard.css"):
        assert files("labgoblin").joinpath("static", asset).read_bytes()
    print(json.dumps({"means": means, "research_invocations": 0, "observer_invocations": 0,
                      "remaining_grants": 0, "installed_package": labgoblin.__file__, "project": str(project)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    run(parser.parse_args().root.resolve())
