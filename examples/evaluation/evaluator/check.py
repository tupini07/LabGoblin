"""Trusted synthetic evaluator: request path and result path are supplied by the host."""

import json
from pathlib import Path
import sys


request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
model = json.loads((Path(request["artifact"]) / "model.json").read_text(encoding="utf-8"))
rows = []
for case in request["cases"]:
    x, expected = case["data"]["x"], case["data"]["expected"]
    predicted = (model["lookup"].get(str(x), model["fallback"]) if "lookup" in model
                 else model["slope"] * x + model["intercept"])
    error = abs(predicted - expected)
    rows.append({
        "id": case["id"], "passed": error == 0, "metrics": {"absolute_error": error},
        "diagnostic": f"x={x}; expected={expected}; predicted={predicted}",
    })
with Path(sys.argv[2]).open("x", encoding="utf-8") as stream:
    json.dump({"version": 1, "cases": rows}, stream, allow_nan=False)
