"""Fenced, independently owned launch and receipt recovery."""

from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from labgoblin.evidence import atomic_json, contained, hash_file, publish_bytes, read_bytes, read_json
from labgoblin.processes import background_options, own_handle, process_state
from labgoblin.protocol import LaunchEnvelope, LaunchReceipt, PreExecutionError, UncertainExecution, canonical, fingerprint
from labgoblin.scheduler import ResourceLedger
from labgoblin.state import State


RUNTIME_FILES = (
    "__init__.py", "protocol.py", "config.py", "db.py", "state.py", "evidence.py",
    "processes.py", "scheduler.py", "worker.py", "backends.py", "payload.py",
    "workspace.py", "agent.py", "agent_policy.py", "agent_worker.py",
    "journal.py", "results.py", "reporting.py", "dashboard_data.py", "dashboard_chat.py", "paths.py",
)
BOOTSTRAP = b"""from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from labgoblin.processes import bound_diagnostics
bound_diagnostics()
if sys.argv[1] in ("--build-supervisor", "--build-api"):
    from labgoblin.backends import build_main
    raise SystemExit(build_main(sys.argv[1], sys.argv[2]))
if sys.argv[1] in ("--observer-supervisor", "--observer-sdk"):
    from labgoblin.dashboard_chat import worker_main
    raise SystemExit(worker_main(sys.argv[1], sys.argv[2]))
if sys.argv[1] == "--payload":
    from labgoblin.payload import main
    digest = sys.argv[sys.argv.index("--digest") + 1] if "--digest" in sys.argv else None
    raise SystemExit(main(sys.argv[-1], inspect="--inspect" in sys.argv[2:-1], expected_digest=digest))
from labgoblin.worker import main
raise SystemExit(main(sys.argv[1]))
"""


def launch_directory(envelope: LaunchEnvelope) -> Path:
    return contained(Path(envelope.root), Path("launches") / envelope.key.nonce)


def prepare_runtime(state: State, envelope: LaunchEnvelope, *, root: Path | None = None) -> LaunchEnvelope:
    source = Path(__file__).parent
    bodies = {name: read_bytes(source / name, 1024 * 1024) for name in RUNTIME_FILES}
    hashes = {name: hashlib.sha256(body).hexdigest() for name, body in bodies.items()}
    manifest = {"files": hashes, "bootstrap": hashlib.sha256(BOOTSTRAP).hexdigest(),
                "namespace": "labgoblin"}
    runtime_id = fingerprint(manifest)
    root = contained(root or state.root, Path("runtime") / runtime_id)
    for name, body in bodies.items():
        publish_bytes(root / "labgoblin" / name, body)
    publish_bytes(root / "bootstrap.py", BOOTSTRAP)
    return replace(envelope, metadata={**envelope.metadata, "runtime": {
        "id": runtime_id, "root": str(root), **manifest,
    }})


def verify_runtime(state: State, envelope: LaunchEnvelope):
    runtime = envelope.metadata.get("runtime")
    if not isinstance(runtime, dict) or not isinstance(runtime.get("files"), dict):
        raise PreExecutionError("Launch lacks a frozen helper identity")
    namespace = runtime.get("namespace")
    if namespace != "labgoblin":
        raise PreExecutionError("Frozen helper namespace is unsupported")
    if set(runtime["files"]) != set(RUNTIME_FILES):
        raise PreExecutionError("Frozen helper manifest is incomplete")
    root = contained(state.root / "runtime", runtime["root"])
    manifest = {"files": runtime["files"], "bootstrap": runtime["bootstrap"], "namespace": namespace}
    if fingerprint(manifest) != runtime["id"]:
        raise PreExecutionError("Frozen helper manifest identity is invalid")
    for name, expected in runtime["files"].items():
        if Path(name).name != name or name not in RUNTIME_FILES:
            raise PreExecutionError("Frozen helper manifest contains an unsupported path")
        if hash_file(root / namespace / name, 1024 * 1024) != expected:
            raise PreExecutionError(f"Frozen helper changed: {name}")
    if hash_file(root / "bootstrap.py", 65536) != runtime["bootstrap"]:
        raise PreExecutionError("Frozen worker bootstrap changed")


def flush_releases(state: State):
    with state.db.read() as conn:
        rows = list(conn.execute("SELECT token FROM allocations WHERE state='release_pending'"))
    if not rows:
        return
    path, ledger_id = state.ledger_identity()
    if not ledger_id:
        raise ValueError("Pending releases have no recorded machine-ledger identity")
    ledger = ResourceLedger(path, expected_id=ledger_id)
    for row in rows:
        state.release_pending(row["token"])
        ledger.release(row["token"], owner_id=state.id)
        state.released(row["token"])


def publish_receipt(state: State, envelope: LaunchEnvelope, receipt: LaunchReceipt):
    if receipt.key != envelope.key or receipt.envelope_digest != envelope.digest:
        raise ValueError("Executor returned a receipt for another launch")
    data = asdict(receipt)
    directory = launch_directory(envelope)
    publish_bytes(directory / "receipt.json", canonical(data))
    state.finish_launch(envelope.key.nonce, data)


def _not_started(state: State, envelope: LaunchEnvelope, error: BaseException):
    receipt = LaunchReceipt(envelope.key, envelope.digest, "not_started", True, 0,
                            reason=f"{type(error).__name__}: {str(error)[:4000]}", executed=False)
    publish_receipt(state, envelope, receipt)


def _uncertain(state: State, envelope: LaunchEnvelope, error: BaseException):
    detail = f"{type(error).__name__}: {str(error)[:4000]}"
    if not state.launch_problem(envelope.key.nonce, detail):
        return
    atomic_json(launch_directory(envelope) / "diagnostic.json",
                {"key": asdict(envelope.key), "error": detail, "observed": time.time()})


def start(state: State, envelope: LaunchEnvelope) -> LaunchEnvelope:
    import psutil
    envelope = prepare_runtime(state, envelope)
    directory = launch_directory(envelope)
    directory.mkdir(parents=True, exist_ok=True)
    publish_bytes(directory / "envelope.json", canonical(asdict(envelope)))
    state.arm(envelope)
    out = err = None
    try:
        out = (directory / "supervisor.stdout.log").open("xb")
        err = (directory / "supervisor.stderr.log").open("xb")
    except OSError as error:
        if out is not None:
            out.close()
        _not_started(state, envelope, error)
        flush_releases(state)
        raise PreExecutionError(str(error)) from error
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", str(Path(envelope.metadata["runtime"]["root"]) / "bootstrap.py"),
             str(directory / "envelope.json")],
            cwd=state.root.parent, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
            **background_options(independent=True))
    except (OSError, ValueError) as error:
        out.close()
        err.close()
        _not_started(state, envelope, error)
        flush_releases(state)
        raise PreExecutionError(str(error)) from error
    try:
        out.close()
        err.close()
        handle = {"pid": process.pid, "created": psutil.Process(process.pid).create_time(),
                  "token": envelope.key.nonce}
        state.attach_launcher(envelope, handle)
    except (OSError, psutil.Error, ValueError, sqlite3.Error) as error:
        launch = state.launch(envelope.key.nonce)
        if launch["phase"] == "quiescent" or (
                launch["supervisor"] and process_state(json.loads(launch["supervisor"])) == "alive"):
            return envelope
        _uncertain(state, envelope, error)
        raise UncertainExecution("Supervisor was created but identity attachment is unresolved") from error
    return envelope


def execute(envelope: LaunchEnvelope, executor=None) -> int:
    state = State.open(Path(envelope.state_path).parent)
    handle = own_handle(envelope.key.nonce)
    if not state.claim_launch(envelope, handle):
        return 0
    entered = False
    try:
        verify_runtime(state, envelope)
        atomic_json(launch_directory(envelope) / "supervisor.json", handle)
        if executor is None:
            if envelope.kind == "attempt":
                from labgoblin.backends import supervise
            else:
                from labgoblin.agent_policy import supervise
            executor = supervise
        entered = True
        receipt = executor(envelope)
        if not isinstance(receipt, LaunchReceipt):
            raise ValueError("Executor did not return a typed quiescent receipt")
        publish_receipt(state, envelope, receipt)
        result = 0 if receipt.status == "completed" else 1
    except PreExecutionError as error:
        _not_started(state, envelope, error)
        result = 1
    except (OSError, ValueError, RuntimeError, ImportError, sqlite3.Error) as error:
        if not entered:
            _not_started(state, envelope, error)
        else:
            _uncertain(state, envelope, error)
        result = 1
    flush_releases(state)
    return result


def recover_allocations(state: State):
    path, ledger_id = state.ledger_identity()
    with state.db.read() as conn:
        rows = [dict(r) for r in conn.execute("""SELECT * FROM allocations
            WHERE state IN ('requested','granted','attached','release_pending')""")]
    if not rows:
        return
    if not ledger_id:
        raise ValueError("Recorded allocations have no ledger identity")
    ledger = ResourceLedger(path, expected_id=ledger_id)
    campaign = state.campaign()
    for row in rows:
        if row["state"] == "release_pending":
            continue
        grant = ledger.grant(row["token"])
        if row["state"] == "attached":
            if not grant or grant["state"] != "granted":
                state.blocker(f"allocation-{row['token']}", "allocation",
                              "Authorized work has lost its recorded machine grant; manual recovery required",
                              row["work_id"])
            continue
        if (row["revision"] != campaign["revision"] or row["generation"] != campaign["generation"]
                or campaign["operator_mode"] not in ("ready", "running")
                or (grant and grant["state"] in ("released", "rejected"))):
            state.release_pending(row["token"])
        elif grant and grant["state"] == "granted":
            state.granted(row["token"])
    flush_releases(state)


def reconcile(state: State, inspector=None) -> dict:
    recovered = []
    errors = []
    for launch in state.active_launches():
        envelope = LaunchEnvelope.parse(json.loads(launch["envelope"]))
        directory = launch_directory(envelope)
        try:
            receipt_path = directory / "receipt.json"
            if not receipt_path.exists():
                receipt_path = directory / "backend-receipt.json"
                if (receipt_path.exists() and envelope.metadata.get("runner", {}).get("kind") == "docker"
                        and (inspector is None or inspector(envelope) != "dead")):
                    continue
            if receipt_path.exists():
                if receipt_path.name == "backend-receipt.json":
                    from labgoblin.backends import read_receipt
                    receipt = read_receipt(envelope)
                    if receipt is None:
                        raise UncertainExecution("Backend receipt disappeared before qualification")
                else:
                    receipt = LaunchReceipt.parse(read_json(receipt_path))
                state.finish_launch(envelope.key.nonce, asdict(receipt))
                recovered.append(envelope.key.work_id)
                continue
            handle = json.loads(launch["supervisor"]) if launch["supervisor"] else None
            if handle is None and (directory / "supervisor.json").exists():
                handle = read_json(directory / "supervisor.json", 16384)
                state.attach_supervisor(envelope, handle)
            if process_state(handle) == "alive":
                continue
            if (launch["phase"] == "armed" and launch["launcher"]
                    and process_state(json.loads(launch["launcher"])) == "alive"):
                continue
            if inspector is not None and inspector(envelope) == "dead":
                elapsed = max(envelope.timeout_seconds, time.time() - launch["created"])
                receipt = LaunchReceipt(
                    envelope.key, envelope.digest, "interrupted", True, elapsed,
                    reason="Verified owned tree exited without a terminal receipt",
                    metadata={"elapsed_basis": "conservative estimate after receipt loss"})
                publish_receipt(state, envelope, receipt)
                recovered.append(envelope.key.work_id)
            else:
                raise UncertainExecution("Armed launch has no verified live supervisor or quiescent receipt; grant retained")
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
            _uncertain(state, envelope, error)
            errors.append({"work_id": envelope.key.work_id, "error": str(error)})
    recover_allocations(state)
    return {"recovered": recovered, "unresolved": errors}


def main(manifest: str) -> int:
    try:
        envelope = LaunchEnvelope.parse(read_json(Path(manifest)))
        return execute(envelope)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        print(f"Worker could not establish a durable outcome: {type(error).__name__}: {str(error)[:4000]}",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
