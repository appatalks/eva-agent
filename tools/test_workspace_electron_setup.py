#!/usr/bin/env python3
"""Seed a temporary Eva workspace database for the packaged Electron E2E test."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from bridge.workspaces import WorkspaceStore


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: test_workspace_electron_setup.py <config-dir> <git-repository>")
    store = WorkspaceStore(sys.argv[1])
    try:
        print(json.dumps(store.register_project(sys.argv[2])))
    finally:
        store.close()


if __name__ == "__main__":
    main()
