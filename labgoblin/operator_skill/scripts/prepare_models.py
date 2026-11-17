"""Explicit one-time public-model preparation; never called by a review job."""

import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.request


def main(directory):
    root = Path(directory).absolute()
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / "models.json"
    entries = json.loads(manifest.read_text()) if manifest.exists() else {}
    if entries:
        for name, entry in entries.items():
            with (root / name).open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != entry["sha256"]:
                    raise ValueError(f"Existing model changed: {name}")
    for name in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
        if name in entries:
            continue
        url = f"https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/{name}"
        target = root / name
        temporary = root / (name + ".download")
        if target.exists():
            raise FileExistsError(f"Unmanifested model already exists: {target}")
        hashed, size = hashlib.sha256(), 0
        with urllib.request.urlopen(url, timeout=60) as response, temporary.open("xb") as output:
            while block := response.read(1024 * 1024):
                size += len(block)
                if size > 512 * 1024**2:
                    raise ValueError("Model exceeds the download bound")
                output.write(block)
                hashed.update(block)
        temporary.rename(target)
        entries[name] = {"url": url, "bytes": size, "sha256": hashed.hexdigest()}
        pending = root / "models.json.tmp"
        with pending.open("w", encoding="utf-8") as stream:
            json.dump(entries, stream, indent=2)
        os.replace(pending, manifest)
    print(json.dumps(entries, indent=2))


if __name__ == "__main__":
    main(sys.argv[1])
