"""One model-free controller used to exercise actual controller death."""

import json
from pathlib import Path
import sys

from labgoblin import runtime
from labgoblin.storage import read_json


root = Path(sys.argv[1]).resolve()
if read_json(root / "fixture-provider.json").get("model_free") is not True:
    raise ValueError("This helper is only for an explicitly model-free fixture")
runtime.provider.capabilities = lambda: {
    "command": [sys.executable, str(Path(__file__).with_name("fixed_copilot.py"))]}
result = runtime.run(root)
print(json.dumps(result))
raise SystemExit(0 if result["status"] == "completed" else 1)
