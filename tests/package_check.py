"""Check a freshly built wheel and sdist against the replacement's actual source."""

from pathlib import Path
import sys
import tarfile
import zipfile


root = Path(__file__).resolve().parents[1]
dist = Path(sys.argv[1])
wheels, sources = list(dist.glob("*.whl")), list(dist.glob("*.tar.gz"))
assert len(wheels) == len(sources) == 1, (wheels, sources)
expected = {f"labgoblin/{path.name}": path.read_bytes() for path in (root / "labgoblin").glob("*.py")}
assets = {f"labgoblin/inspection_assets/{path.name}": path.read_bytes()
          for path in (root / "labgoblin" / "inspection_assets").iterdir() if path.is_file()}
assert len(assets) == 3
with zipfile.ZipFile(wheels[0]) as archive:
    names = set(archive.namelist())
    actual = {name for name in names if name.startswith("labgoblin/") and name.endswith(".py")}
    assert actual == set(expected), (actual - set(expected), set(expected) - actual)
    for name, body in expected.items():
        assert archive.read(name) == body, name
    assert {name for name in names if name.startswith("labgoblin/inspection_assets/")} == set(assets)
    for name, body in assets.items():
        assert archive.read(name) == body, name
    assert not any("/static/" in name or name.startswith("tests/") or "auto-cleanrl-report" in name
                   or "auto-craftax-report" in name for name in names)
    metadata = archive.read(next(name for name in names if name.endswith(".dist-info/METADATA"))).decode()
    assert "Version: 3.0.0" in metadata
    assert "Requires-Dist: github-copilot-sdk==1.0.15" in metadata
    assert not any(f"Requires-Dist: {name}" in metadata for name in ("docker", "rich", "markdown-it-py"))
    for name in ("experiment.py", "research_goal.md", "README.md"):
        assert any(item.endswith(f"/examples/local-synthetic/{name}") for item in names)
    assert any(name.endswith("/LICENSE") for name in names)
with tarfile.open(sources[0]) as archive:
    names = archive.getnames()
    for name, body in {**expected, **assets}.items():
        member = next(item for item in names if item.endswith("/" + name))
        assert archive.extractfile(member).read() == body, name
    assert any(name.endswith("/tests/fixed_copilot.py") for name in names)
    assert any(name.endswith("/docs/runtime-protocol.md") for name in names)
    assert not any("/static/" in name or "auto-cleanrl-report" in name or "auto-craftax-report" in name for name in names)
print(f"Distribution identity verified: {len(expected)} runtime modules; no retired runtime or historical report bundles")
