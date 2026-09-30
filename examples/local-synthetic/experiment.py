"""Dependency-free, deterministic data for the local runtime example."""

import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args()
    values = [2 + args.offset, 4 + args.offset, 6 + args.offset]
    metrics = {"mean": sum(values) / len(values), "count": len(values), "offset": args.offset}
    output = Path(os.environ["XGENIUS_OUTPUT_DIR"])
    (output / "metrics.json").write_text(json.dumps(metrics, allow_nan=False), encoding="utf-8")
    print(json.dumps(metrics, allow_nan=False))


if __name__ == "__main__":
    main()
