"""Installed-wheel acceptance with actual owned trees and a model-free SDK server."""

import argparse
import json
from pathlib import Path
import shutil
import sys
import threading
from urllib.request import Request, urlopen

import labgoblin
from labgoblin import inspector, payload, runtime
from labgoblin.inspection_evidence import Evidence
from labgoblin.inspection_server import InspectionServer
from labgoblin.config import Config
from labgoblin.scheduler import ResourceLedger
from labgoblin.storage import hash_file, publish_json, read_json


parser = argparse.ArgumentParser()
parser.add_argument("--root", type=Path, required=True)
args = parser.parse_args()
assert sys.flags.isolated, "Use python -I to exclude source imports"
assert Path(labgoblin.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()), labgoblin.__file__
assert labgoblin.__version__ == "3.0.0"
root = args.root.resolve()
root.mkdir()
example = Path(sys.prefix) / "share" / "labgoblin" / "examples" / "local-synthetic"
assert (example / "experiment.py").is_file(), example
project = root / "project"
project.mkdir()
for name in ("experiment.py", "research_goal.md"):
    shutil.copyfile(example / name, project / name)
metrics = root / "standalone-metrics.json"
payload.probe([sys.executable, "-I", str(project / "experiment.py"), "--offset", "1", "--output", str(metrics)])
assert read_json(metrics)["mean"] == 5
ledger = ResourceLedger.create(root / "machine.db")
ledger.configure(1, 2048, headroom_mb=512)
record = runtime.initialize(project, Config(cpus=1, memory_mb=1024, timeout_seconds=60,
                                           coach=False, max_ai_credits=300, ledger=str(ledger.path)))
provider = root / "fixed-copilot.py"
shutil.copyfile(Path(__file__).with_name("fixed_copilot.py"), provider)
publish_json(project / "fixture-provider.json", {"model_free": True, "root": str(project)})
runtime.provider.capabilities = lambda: {"command": [sys.executable, str(provider)]}
result = runtime.run(project)
assert result["status"] == "completed", result
assert result["usage"]["totalNanoAiu"] == 10_000_000_000
snapshot = project / ".labgoblin" / "runs" / result["id"] / "snapshot"
assert read_json(snapshot / "artifacts" / "metrics.json")["means"] == [4, 4, 5]
assert runtime.status(project)["budget"]["remaining"] == 290
assert not ledger.rows()
consulting = root / "consulting"
runtime.initialize(consulting, Config(cpus=1, memory_mb=1024, timeout_seconds=60,
                                     coach=True, consultant=True, max_ai_credits=1000, ledger=str(ledger.path)),
                   goal="Qualify a model-free research and coach consultation.")
publish_json(consulting / "fixture-provider.json", {"model_free": True, "root": str(consulting),
                                                    "mode": "consult", "caller": "both", "continue": True})
advised = runtime.run(consulting)
assert advised["status"] == "completed", advised
assert advised["usage"]["totalNanoAiu"] == 60_000_000_000
assert len([s for s in advised["usage"]["sessions"] if s["role"].startswith("consult-")]) == 2
assert len(list((consulting / "artifacts" / "consultations").glob("*.md"))) == 2
assert not ledger.rows()
claim_review = root / "claim-review"
runtime.initialize(claim_review, Config(cpus=1, memory_mb=1024, timeout_seconds=60,
                                       coach=True, consultant=True, max_ai_credits=1000, ledger=str(ledger.path)),
                   goal="Determine whether x*x-x is nonnegative for all real x.")
publish_json(claim_review / "fixture-provider.json", {"model_free": True, "root": str(claim_review),
                                                      "mode": "claim_review", "caller": "coach", "continue": True})
reviewed = runtime.run(claim_review)
assert reviewed["status"] == "completed", reviewed
assert reviewed["usage"]["totalNanoAiu"] == 50_000_000_000
claim_run = claim_review / ".labgoblin" / "runs" / reviewed["id"]
assert read_json(claim_run / "snapshot" / "artifacts" / "claim-counterexample.json") == {"x": "1/2", "value": "-1/4"}
claim_decisions = reviewed["metadata"]["provider"]["reviews"]
assert [decision["decision"] for decision in claim_decisions] == ["continue", "accept"]
claim_brief = claim_decisions[0]["consultations"][0]
assert claim_brief["purpose"] == "claim_review"
assert hash_file(claim_run / "snapshot" / claim_brief["artifact"]) == claim_brief["artifact_sha256"]
assert claim_decisions[1]["consultations"] == []
assert not ledger.rows()
closing = root / "closing"
runtime.initialize(closing, Config(cpus=1, memory_mb=1024, timeout_seconds=60,
                                  coach=True, max_ai_credits=300, ledger=str(ledger.path)),
                   goal="Qualify useful work after provisional stopping approval.")
publish_json(closing / "fixture-provider.json", {"model_free": True, "root": str(closing),
                                               "mode": "closing_challenge"})
challenged = runtime.run(closing)
assert challenged["status"] == "completed", challenged
assert challenged["usage"]["totalNanoAiu"] == 40_000_000_000
assert [s["prompts"] for s in challenged["usage"]["sessions"]] == [2, 1, 1]
closing_run = closing / ".labgoblin" / "runs" / challenged["id"]
assert read_json(closing_run / "snapshot" / "artifacts" / "closing-control.json") == {"baseline": 4, "shifted": 5}
assert [read_json(closing_run / f"review-{index}.json")["decision"] for index in range(2)] == ["accept", "accept"]
assert not ledger.rows()
progress = root / "progress"
runtime.initialize(progress, Config(cpus=1, memory_mb=1024, timeout_seconds=180,
                                   coach=True, max_stalled_reviews=2, max_ai_credits=300, ledger=str(ledger.path)),
                   goal="Qualify progress-aware continuation without renewing spending or the closing challenge.")
publish_json(progress / "fixture-provider.json", {
    "model_free": True, "root": str(progress), "mode": "progress_reviews",
    "review_decisions": ["accept", "continue", "continue", "continue", "accept"],
    "review_progress": ["none", "meaningful", "none", "none"],
})
progressed = runtime.run(progress)
assert progressed["status"] == "completed", progressed
assert progressed["usage"]["totalNanoAiu"] == 100_000_000_000
assert [s["prompts"] for s in progressed["usage"]["sessions"]] == [5, 1, 1, 1, 1, 1]
progress_run = progress / ".labgoblin" / "runs" / progressed["id"]
assert [read_json(progress_run / f"review-{i}.json")["stalled_reviews"] for i in range(5)] == [0, 1, 0, 1, 2]
assert read_json(progress_run / "snapshot" / "artifacts" / "review-control-3.json") == {"baseline": 4, "shifted": 6}
assert runtime.status(progress)["budget"]["remaining"] == 200
assert not ledger.rows()
stalled = root / "stalled"
runtime.initialize(stalled, Config(cpus=1, memory_mb=1024, timeout_seconds=120,
                                  coach=True, max_stalled_reviews=2, max_ai_credits=300, ledger=str(ledger.path)),
                   goal="Qualify unfinished checkpoints separately from operator dependencies.")
publish_json(stalled / "fixture-provider.json", {
    "model_free": True, "root": str(stalled), "review_decisions": ["continue"] * 3,
})
checkpoint = runtime.run(stalled)
assert checkpoint["status"] == "checkpointed", checkpoint
assert checkpoint["metadata"]["provider"]["checkpoint_reason"] == "stalled_reviews"
assert runtime.status(stalled)["runs"][-1]["checkpoint_reason"] == "stalled_reviews"
assert checkpoint["usage"]["totalNanoAiu"] == 60_000_000_000
assert not ledger.rows()
before = {str(path): hash_file(path) for path in project.rglob("*")
          if path.is_file() and path.name != ".labgoblin.lock"}
observer = inspector.Inspector(project, directory=root / "inspection")
observer.approve(100, model="model-free", timeout=60)
publish_json(observer.root / "fixture-provider.json", {"model_free": True, "root": str(observer.root)})
inspected = observer.ask("Explain the retained outcome", command=[sys.executable, str(provider)])
assert inspected["status"] == "completed", inspected
assert observer.source(inspected["id"], "E2")["result"]["scope"] == result["id"]
assert observer.status()["inspection"]["budget"]["remaining"] == 90
assert runtime.status(project)["budget"]["remaining"] == 290
assert before == {str(path): hash_file(path) for path in project.rglob("*")
                  if path.is_file() and path.name != ".labgoblin.lock"}
evidence = Evidence(closing, runtime.project_record(closing)[2]["id"])
assert len(evidence.execution({"run": challenged["id"]})["reviews"]) == 2
assert evidence.session({"run": challenged["id"]})["events"]
server = InspectionServer(observer)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    for route in ("/", "/inspector.js", "/inspector.css", "/api/status", "/api/history"):
        with urlopen(Request(server.origin + route, headers={"Authorization": "Bearer " + server.token}), timeout=5) as response:
            assert response.status == 200 and response.read()
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
assert not ledger.rows()
print(json.dumps({"model_free": True, "package": labgoblin.__file__, "project_id": record["id"],
                  "run_id": result["id"], "mean": read_json(metrics)["mean"], "status": result["status"],
                  "consultation_run_id": advised["id"], "consultation_status": advised["status"],
                  "claim_review_run_id": reviewed["id"], "claim_review_status": reviewed["status"],
                  "closing_run_id": challenged["id"], "closing_status": challenged["status"],
                  "progress_run_id": progressed["id"], "progress_status": progressed["status"],
                  "checkpoint_run_id": checkpoint["id"], "checkpoint_status": checkpoint["status"],
                  "inspection_run_id": inspected["id"], "inspection_status": inspected["status"]}))
