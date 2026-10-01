"""Check clean artifacts without importing the source package."""

import configparser
from email.parser import Parser
from pathlib import Path
import sys
import tarfile
import zipfile


def check(directory):
    wheels, sources = list(directory.glob("*.whl")), list(directory.glob("*.tar.gz"))
    assert len(wheels) == len(sources) == 1, "Expected exactly one wheel and sdist"
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        metadata = Parser().parsestr(archive.read(next(n for n in names if n.endswith(".dist-info/METADATA"))).decode())
        assert metadata["Name"] == "labgoblin"
        assert "github-copilot-sdk==1.0.15" in metadata.get_all("Requires-Dist")
        entry = configparser.ConfigParser()
        entry.read_string(archive.read(next(n for n in names if n.endswith(".dist-info/entry_points.txt"))).decode())
        assert dict(entry["console_scripts"]) == {"labgoblin": "labgoblin.cli:main"}
        for module in ("initialization", "setup_assistant", "setup_runtime", "state", "worker", "config"):
            assert f"labgoblin/{module}.py" in names
        for asset in ("dashboard.js", "dashboard-chat.js", "dashboard.css"):
            assert f"labgoblin/static/{asset}" in names
        assert any(n.endswith("share/labgoblin/examples/local-synthetic/batch.json") for n in names)
        assert not any(n.startswith("xgenius/") or "/xgenius/" in n for n in names)
    with tarfile.open(sources[0]) as archive:
        names = archive.getnames()
        assert any(n.endswith("/tests/installed_smoke.py") for n in names)
        assert any(n.endswith("/labgoblin/setup_assistant.py") for n in names)
        assert not any("/xgenius/" in n for n in names)
    print("Distribution identity, core SDK, assets and canonical-only package contents verified.")


if __name__ == "__main__":
    check(Path(sys.argv[1]))
