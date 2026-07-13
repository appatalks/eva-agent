#!/usr/bin/env python3
"""ACP Bridge Server for Eva — thin shim.

The implementation lives in the ``bridge`` package (tools/bridge/).
This file exists so that existing references (service files, shell
scripts, standalone/main.js, test imports) keep working unchanged.
"""
import os
import sys

# Ensure the tools/ directory is on sys.path so ``import bridge`` resolves
# to tools/bridge/ regardless of the working directory.
_tools_dir = os.path.dirname(os.path.abspath(__file__))
if _tools_dir not in sys.path:
    sys.path.insert(0, _tools_dir)

# Re-export everything from the bridge package for backward-compatible
# importlib.util.spec_from_file_location("acp_bridge", "tools/acp_bridge.py")
# usage in tests.
from bridge.core import *  # noqa: F401,F403
from bridge.core import (  # noqa: F401
    main, _valid_artifact_name, _SKILL_COLUMNS, _GOAL_COLUMNS,
)

if __name__ == "__main__":
    main()
