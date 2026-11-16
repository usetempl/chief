#!/usr/bin/env python3
"""Verify (or intentionally update) the exact files copied into a private workspace."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("chief_setup", ROOT / "setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--write", action="store_true")
args = parser.parse_args()
files = {}
for name in sorted(setup.PACKAGE_FILES):
    path = ROOT / name
    if path.is_symlink() or not path.is_file():
        raise SystemExit("Invalid package source: " + name)
    files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
expected = {"version": (ROOT / "VERSION").read_text().strip(), "files": files}
path = ROOT / "MANIFEST.json"
if args.write:
    path.write_text(json.dumps(expected, indent=2) + "\n")
elif json.loads(path.read_text()) != expected:
    raise SystemExit("Manifest mismatch; review changes before running --write")
print(f"Verified {len(files)} package files")
