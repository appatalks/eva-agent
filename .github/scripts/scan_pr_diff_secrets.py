#!/usr/bin/env python3
"""CLI compatibility entry point for Eva's shared credential diff scanner."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from workspace_diff_guard import PATTERNS, main, scan_diff, scan_text, sensitive_path


if __name__ == "__main__":
    raise SystemExit(main())
