"""Local stdio protocol fixture. This program never contacts a model provider."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import uuid

from copilot._sdk_protocol_version import get_sdk_protocol_version


ROOT = Path.cwd().resolve()
CONFIG = json.loads((ROOT / "fixture-provider.json").read_text(encoding="utf-8"))
if CONFIG.get("model_free") is not True or CONFIG.get("root") != str(ROOT):
    raise ValueError("The fixed provider requires an exact, explicitly model-free fixture root")

LOCK = threading.Lock()
SESSION = None
REQUESTS = 0
ABORT = threading.Event()
TOOL_DONE = threading.Event()
TOOL_RESULT = None
TOOLS = []
ROLE = "researcher"
COACH_INDEX = 0


def emit(value):
    body = json.dumps(value).encode()
    with LOCK:
        sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        sys.stdout.buffer.flush()


def event(kind, data):
    emit({"jsonrpc": "2.0", "method": "session.event", "params": {
        "sessionId": SESSION, "event": {"id": str(uuid.uuid4()), "timestamp": datetime.now(timezone.utc).isoformat(),
                                      "type": kind, "data": data}}})


def invoke_tool(name, arguments):
    global TOOL_RESULT
    TOOL_RESULT = None
    TOOL_DONE.clear()
    request_id = str(uuid.uuid4())
    event("external_tool.requested", {
        "requestId": request_id, "sessionId": SESSION, "toolCallId": request_id,
        "toolName": name, "arguments": arguments})
    until = time.monotonic() + 60
    while time.monotonic() < until:
        if ABORT.is_set():
            return None
        if TOOL_DONE.wait(0.05):
            with (ROOT / f"fixture-tools-{SESSION}.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"name": name, "arguments": arguments, "result": TOOL_RESULT}) + "\n")
            if TOOL_RESULT.get("resultType") != "success":
                raise RuntimeError(f"Fixed tool request failed: {TOOL_RESULT}")
            return TOOL_RESULT
    raise RuntimeError("Fixed external tool request never completed")


def exercise():
    event("assistant.turn_start", {"turnId": str(uuid.uuid4())})
    if ROLE == "inspector":
        if CONFIG.get("mode") == "inspection_wait":
            ABORT.wait(120)
            return
        if invoke_tool("labgoblin_inspect_read", {"scope": "latest", "path": "report.md"}) is None:
            return
        if CONFIG.get("image"):
            if invoke_tool("labgoblin_inspect_read", {"scope": "latest", "path": CONFIG["image"], "image": True}) is None:
                return
        event("assistant.message", {"messageId": str(uuid.uuid4()),
                                   "content": CONFIG.get("answer", "The retained report is evidence [E2], not independent scientific validation.")})
        event("session.idle", {})
        return
    if ROLE == "adviser":
        (ROOT / "fixture-adviser-ready").write_text(SESSION, encoding="utf-8")
        if CONFIG.get("mode") == "consult_wait":
            ABORT.wait(120)
            return
        if invoke_tool("labgoblin_read_evidence", {"path": "research-notes.md"}) is None:
            return
        if CONFIG.get("mode") == "closure_critic":
            if CONFIG.get("image") and invoke_tool("labgoblin_read_image", {"path": CONFIG["image"]}) is None:
                return
            answer = ("The retained exact interior-point counterexample answers the original all-real question; "
                      "no compelling continuation remains within the checked scope."
                      if (ROOT / "artifacts" / "closure-counterexample.json").exists() else
                      "The original all-real goal is unanswered: x=1/2 is a feasible untested control. "
                      "Compute x*x-x there rather than treating two endpoint zeros as enough evidence.")
            event("assistant.message", {"messageId": str(uuid.uuid4()), "content": answer})
            event("session.idle", {})
            return
        event("assistant.message", {"messageId": str(uuid.uuid4()),
                                   "content": "Counterexample: x=1/2 gives x*x-x=-1/4. The two samples do not prove nonnegativity."
                                              if CONFIG.get("mode") == "claim_review" else
                                              "Model-free advisory fixture: inspect the positive control before generalizing. "
                                              "Source: research-notes.md. No external literature was retrieved."})
        event("session.idle", {})
        return
    if ROLE == "coach":
        if CONFIG.get("mode") == "progress_reviews":
            if invoke_tool("labgoblin_read_evidence", {"path": f"artifacts/review-control-{COACH_INDEX}.json"}) is None:
                return
        if CONFIG.get("mode") == "closing_challenge" and COACH_INDEX == 2:
            if invoke_tool("labgoblin_read_evidence", {"path": "artifacts/closing-control.json"}) is None:
                return
        if CONFIG.get("mode") == "claim_review" and COACH_INDEX == 2:
            if invoke_tool("labgoblin_read_evidence", {"path": "artifacts/claim-counterexample.json"}) is None:
                return
        if COACH_INDEX == 1 and CONFIG.get("caller") in ("coach", "both"):
            question = ({"question": "Do the two sampled zeros establish x*x-x >= 0 for every real x, justifying stopping?",
                         "purpose": "claim_review"} if CONFIG.get("mode") == "claim_review" else
                        {"question": "Does the known control justify ending this investigation?"})
            if invoke_tool("labgoblin_consult", {**question, "evidence_paths": ["research-notes.md"]}) is None:
                return
        decision = "continue" if COACH_INDEX == 1 and CONFIG.get("continue") else "accept"
        if "review_decisions" in CONFIG:
            decision = CONFIG["review_decisions"][COACH_INDEX - 1]
        args = {
                "decision": decision, "reason": "Fixed stopping assessment", "coverage": "Synthetic fixture evidence",
                "question": "Check the remaining control." if decision == "continue" else ""}
        if COACH_INDEX > 1:
            progress = CONFIG.get("review_progress", ["none"] * COACH_INDEX)[COACH_INDEX - 2]
            args.update(progress=progress, progress_reason=(
                f"Inspected artifacts/review-control-{COACH_INDEX}.json: the changed offset discriminates the control."
                if progress == "meaningful" else "The fixed control supplies no new discriminating evidence."))
        if CONFIG.get("mode") == "claim_review":
            args.update(reason="The all-real claim needs a counterexample check." if COACH_INDEX == 1 else
                        "The retained exact counterexample refutes the original all-real claim.",
                        question="Does the all-real claim survive an interior-point check?" if COACH_INDEX == 1 else "",
                        coverage="Inspected the targeted review." if COACH_INDEX == 1 else
                        "Read artifacts/claim-counterexample.json; exact researcher computation retained.")
            if COACH_INDEX > 1:
                args.update(progress="meaningful",
                            progress_reason="artifacts/claim-counterexample.json contains a new exact refutation, not review prose.")
        if decision == "checkpoint":
            args["checkpoint_reason"] = CONFIG["checkpoint_reason"]
        if CONFIG.get("mode") == "closure_critic" and COACH_INDEX > 1:
            args.update(decision="accept", question="", progress="none")
            if REQUESTS == 2:
                args["critique_response"] = (
                    "The critic's interior-point objection matters to the original all-real goal. "
                    "An exact retained computation, not a narrowed report, is needed."
                    if COACH_INDEX == 2 else
                    "Inspected artifacts/closure-counterexample.json: x=1/2 gives -1/4. "
                    "This refutes the original all-real claim and resolves the critic's objection.")
                if COACH_INDEX == 2:
                    args.update(decision="continue", question="Does the all-real claim survive the feasible interior-point control?")
                else:
                    if invoke_tool("labgoblin_read_evidence", {"path": "artifacts/closure-counterexample.json"}) is None:
                        return
                    args.update(progress="meaningful",
                                progress_reason="The new exact counterexample refutes the original claim; critic prose alone did not.")
        if invoke_tool("labgoblin_review_decision", args) is None:
            return
        event("assistant.message", {"messageId": str(uuid.uuid4()), "content": "The fixed stopping review is recorded."})
        event("session.idle", {})
        return
    if CONFIG.get("mode") == "wait":
        ABORT.wait(120)
        return
    if CONFIG.get("mode") == "wait_for_release":
        while not (ROOT / "finish-fixture").exists():
            if ABORT.wait(0.05):
                return
    output = ROOT / "artifacts"
    output.mkdir(exist_ok=True)
    selected = shutil.which("python")
    (ROOT / "python-selection.json").write_text(json.dumps({
        "resolved": selected, "parent": sys.executable}), encoding="utf-8")
    child = subprocess.Popen([selected, "-c", "import sys;print(sys.executable)"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    (ROOT / "python-child.json").write_text(json.dumps({"pid": child.pid}), encoding="utf-8")
    try:
        stdout, stderr = child.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        (ROOT / "python-timeout.json").write_text(json.dumps({"pid": child.pid, "returncode": child.poll()}))
        child.kill()
        child.communicate(timeout=5)
        raise
    if child.returncode:
        raise RuntimeError(f"Selected Python failed: {child.returncode}: {stderr}")
    selected_python = stdout.strip()
    if CONFIG.get("mode") == "closing_challenge" and REQUESTS == 2:
        control = subprocess.check_output([
            selected_python, "-I", "-c",
            "import json; values=[2,4,6]; print(json.dumps({'baseline':sum(values)/3,"
            "'shifted':sum(v+1 for v in values)/3}))",
        ], stdin=subprocess.DEVNULL, text=True, timeout=10)
        (output / "closing-control.json").write_text(control, encoding="utf-8")
    if CONFIG.get("mode") == "progress_reviews":
        offset = 1 if REQUESTS < 3 else 2
        control = subprocess.check_output([
            selected_python, "-I", "-c",
            "import json; values=[2,4,6]; print(json.dumps({'baseline':sum(values)/3,"
            f"'shifted':sum(v+{offset} for v in values)/3}}))",
        ], stdin=subprocess.DEVNULL, text=True, timeout=10)
        (output / f"review-control-{REQUESTS}.json").write_text(control, encoding="utf-8")
    result = {"means": [4, 4, 5], "model_free_fixture": True, "python_executable": selected_python}
    (output / "metrics.json").write_text(json.dumps(result), encoding="utf-8")
    (ROOT / "report.md").write_text("Fixed protocol qualification: means are 4, 4 and 5. No model call occurred.\n",
                                  encoding="utf-8")
    (ROOT / "research-notes.md").write_text("Retain the deterministic fixture results.\n", encoding="utf-8")
    if CONFIG.get("mode") == "claim_review":
        claim = "Claim: x*x-x is nonnegative for every real x because it is zero at x=0 and x=1."
        if REQUESTS == 1:
            (ROOT / "research-notes.md").write_text(claim, encoding="utf-8")
            (ROOT / "report.md").write_text(claim, encoding="utf-8")
        else:
            control = subprocess.check_output([
                selected_python, "-I", "-c",
                "import json;from fractions import Fraction;x=Fraction(1,2);"
                "print(json.dumps({'x':str(x),'value':str(x*x-x)}))",
            ], stdin=subprocess.DEVNULL, text=True, timeout=10)
            (output / "claim-counterexample.json").write_text(control, encoding="utf-8")
            (ROOT / "report.md").write_text("The all-real nonnegativity claim is false: at x=1/2 the value is -1/4.\n",
                                          encoding="utf-8")
    if CONFIG.get("mode") == "closure_critic":
        claim = "Claim: two zeros at x=0 and x=1 justify ending investigation of whether x*x-x >= 0 for all real x."
        if REQUESTS < 3:
            (ROOT / "research-notes.md").write_text(claim, encoding="utf-8")
            (ROOT / "report.md").write_text(claim, encoding="utf-8")
        else:
            control = subprocess.check_output([
                selected_python, "-I", "-c",
                "import json;from fractions import Fraction;x=Fraction(1,2);"
                "print(json.dumps({'x':str(x),'value':str(x*x-x)}))",
            ], stdin=subprocess.DEVNULL, text=True, timeout=10)
            (output / "closure-counterexample.json").write_text(control, encoding="utf-8")
            for name in ("research-notes.md", "report.md"):
                (ROOT / name).write_text("The exact x=1/2 counterexample gives -1/4; the all-real claim is false.\n", encoding="utf-8")
    if CONFIG.get("mode", "").startswith("consult") and REQUESTS == 1 and CONFIG.get("caller", "researcher") in ("researcher", "both"):
        if invoke_tool("labgoblin_consult", {"question": "What does this known-answer control establish?",
                                           "evidence_paths": ["research-notes.md"]}) is None:
            return
    event("assistant.message", {"messageId": str(uuid.uuid4()), "content": "The fixed fixture completed; see report.md."})
    event("session.idle", {})


def exercise_checked():
    try:
        exercise()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        event("session.error", {"errorType": "fixture_failure", "message": str(error)})
        event("session.idle", {})


while True:
    header = sys.stdin.buffer.readline()
    if not header:
        break
    if not header.startswith(b"Content-Length:"):
        raise ValueError(f"Unexpected RPC header: {header!r}")
    size = int(header.split(b":", 1)[1])
    if size > 1024 * 1024 or sys.stdin.buffer.readline() != b"\r\n":
        raise ValueError("Invalid bounded RPC frame")
    request = json.loads(sys.stdin.buffer.read(size))
    method, params = request["method"], request.get("params", {})
    with (ROOT / "fixture-rpc.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"method": method, "session_id": params.get("sessionId")}) + "\n")
    if method == "connect":
        result = {"ok": True, "protocolVersion": get_sdk_protocol_version(), "version": "model-free-fixture"}
    elif method == "session.create":
        SESSION = params["sessionId"]
        TOOLS = params.get("tools", [])
        names = {tool["name"] for tool in TOOLS}
        ROLE = ("inspector" if "labgoblin_inspect_read" in names else "coach" if "labgoblin_review_decision" in names
                else "adviser" if "labgoblin_read_evidence" in names else "researcher")
        if ROLE == "coach":
            counter = ROOT / "fixture-coach-count"
            COACH_INDEX = int(counter.read_text()) + 1 if counter.exists() else 1
            counter.write_text(str(COACH_INDEX), encoding="utf-8")
        (ROOT / f"fixture-session-{SESSION}.json").write_text(json.dumps(params), encoding="utf-8")
        result = {"sessionId": SESSION, "workspacePath": str(ROOT)}
    elif method == "session.tools.initializeAndValidate":
        result = {}
    elif method == "session.tools.getCurrentMetadata":
        result = {"tools": [{"name": tool["name"], "description": tool.get("description", "")} for tool in TOOLS] +
                           ([] if ROLE == "inspector" else [{"name": "web_fetch", "description": "Model-free fixture; no actual HTTP request"}])}
    elif method == "session.tools.handlePendingToolCall":
        TOOL_RESULT = params.get("result", {"resultType": "failure", "error": params.get("error")})
        TOOL_DONE.set()
        result = {"handled": True}
    elif method == "session.permissions.setMode":
        result = {"success": True, "mode": params["mode"]}
    elif method in ("session.options.update", "session.detach", "session.abort", "runtime.shutdown"):
        result = {"success": True}
    elif method == "session.usage.getMetrics":
        if REQUESTS and (CONFIG.get("mode") == "missing_usage" or
                         ROLE == "adviser" and CONFIG.get("mode") == "consult_missing_usage"):
            emit({"jsonrpc": "2.0", "id": request["id"],
                  "error": {"code": -32000, "message": "Deliberate final usage failure"}})
            continue
        result = {
            "codeChanges": {"filesModified": [], "filesModifiedCount": 0, "linesAdded": 0, "linesRemoved": 0},
            "lastCallInputTokens": 0, "lastCallOutputTokens": 0, "modelMetrics": {},
            "sessionStartTime": "2026-10-14T00:00:00Z", "totalApiDurationMs": 0,
            "totalPremiumRequestCost": 0, "totalUserRequests": REQUESTS,
            "totalNanoAiu": REQUESTS * CONFIG.get("cost", 10_000_000_000),
        }
    elif method == "session.send":
        REQUESTS += 1
        if ROLE == "inspector" or CONFIG.get("mode") in ("closing_challenge", "progress_reviews", "claim_review", "closure_critic"):
            (ROOT / f"fixture-prompt-{SESSION}-{REQUESTS}.txt").write_text(params["prompt"], encoding="utf-8")
        result = {"messageId": str(uuid.uuid4())}
    else:
        emit({"jsonrpc": "2.0", "id": request.get("id"),
              "error": {"code": -32601, "message": f"Unimplemented model-free method: {method}"}})
        continue
    emit({"jsonrpc": "2.0", "id": request["id"], "result": result})
    if method == "session.send":
        threading.Thread(target=exercise_checked, daemon=True).start()
    elif method == "session.abort":
        ABORT.set()
        event("session.idle", {"aborted": True})
    elif method == "runtime.shutdown":
        break
