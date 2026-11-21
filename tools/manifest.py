#!/usr/bin/env python3
"""Verify (or intentionally update) the public guides and executable sources for this release."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE_FILES = (
    "AGENTS.md", "CHANGELOG.md", "LICENSE", "README.md", "VERSION",
    "docs/architecture.md", "docs/architecture.mmd", "docs/core.md", "docs/device.md",
    "docs/dot.md", "docs/entry.md", "docs/start.md",
    "onboard.py", "snapshot.py", "runtime/alfred_board.py", "runtime/alfred_report.py",
    "runtime/alfred_tasks.py", "tools/build_guide.py", "tools/manifest.py",
)
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--write", action="store_true")
args = parser.parse_args()
files = {}
for name in sorted(RELEASE_FILES):
    path = ROOT / name
    if path.resolve() != path or not path.is_file():
        raise SystemExit("Invalid package source: " + name)
    files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
expected = {"version": (ROOT / "VERSION").read_text().strip(), "files": files}
path = ROOT / "MANIFEST.json"
if path.is_symlink():
    raise SystemExit("Manifest must be a regular file")
if args.write:
    path.write_text(json.dumps(expected, indent=2) + "\n")
elif json.loads(path.read_text()) != expected:
    raise SystemExit("Manifest mismatch; review changes before running --write")
print(f"Verified {len(files)} package files")
