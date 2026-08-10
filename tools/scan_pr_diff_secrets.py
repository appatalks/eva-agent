#!/usr/bin/env python3
"""Scan added pull-request diff lines for credential material without echoing it."""

import argparse
import json
import re
import shlex
from pathlib import PurePosixPath


PATTERNS = (
    ("private-key", re.compile(r"-----BEGIN (?:[A-Z0-9 ]*PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----")),
    ("openai-key", re.compile(r"\bsk-(?!FAKE)[A-Za-z0-9_-]{20,}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("bearer-token", re.compile(r"\bBearer\s+[A-Za-z0-9._~-]{20,}\b", re.IGNORECASE)),
)

SENSITIVE_NAMES = {
    ".env", ".env.local", ".env.production", "config.json", "config.local.js",
    "msal_token_cache.json", "auth.enc.json",
}
SENSITIVE_PARTS = {".azure", ".aws", ".ssh"}


def sensitive_path(path):
    candidate = PurePosixPath(path)
    return (
        candidate.name in SENSITIVE_NAMES
        or candidate.name.startswith(".env.")
        or bool(set(candidate.parts) & SENSITIVE_PARTS)
    )


def scan_diff(text):
    findings = []
    sensitive_paths = set()
    path = ""
    added_line = 0
    for diff_line, raw in enumerate(text.splitlines(), 1):
        if raw.startswith("diff --git "):
            try:
                fields = shlex.split(raw)
            except ValueError:
                fields = []
            if len(fields) >= 4 and fields[3].startswith("b/"):
                path = fields[3][2:]
                added_line = 0
                if sensitive_path(path) and path not in sensitive_paths:
                    sensitive_paths.add(path)
                    findings.append({"kind": "sensitive-file", "path": "sensitive-path", "diff_line": diff_line})
            continue
        if raw.startswith("+++ b/"):
            path = raw[6:]
            added_line = 0
            if sensitive_path(path) and path not in sensitive_paths:
                sensitive_paths.add(path)
                findings.append({"kind": "sensitive-file", "path": "sensitive-path", "diff_line": diff_line})
            continue
        if raw.startswith(("rename to ", "copy to ")):
            path = raw.split(" to ", 1)[1]
            if sensitive_path(path) and path not in sensitive_paths:
                sensitive_paths.add(path)
                findings.append({"kind": "sensitive-file", "path": "sensitive-path", "diff_line": diff_line})
            continue
        if raw.startswith("@@"):
            match = re.search(r"\+(\d+)", raw)
            added_line = int(match.group(1)) - 1 if match else 0
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            added_line += 1
            content = raw[1:]
            for kind, pattern in PATTERNS:
                if pattern.search(content):
                    findings.append({
                        "kind": kind,
                        "path": "pr-diff",
                        "line": added_line,
                        "diff_line": diff_line,
                    })
        elif raw.startswith(" "):
            added_line += 1
    return findings


def scan_text(text, source):
    """Scan non-diff untrusted text without retaining or returning matched values."""
    findings = []
    for line_number, content in enumerate(text.splitlines(), 1):
        for kind, pattern in PATTERNS:
            if pattern.search(content):
                findings.append({"kind": kind, "path": source, "line": line_number})
    return findings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("diff_path")
    parser.add_argument("--text-path", action="append", default=[])
    parser.add_argument("--json-output")
    args = parser.parse_args()

    with open(args.diff_path, encoding="utf-8", errors="replace") as handle:
        findings = scan_diff(handle.read())
    for path in args.text_path:
        with open(path, encoding="utf-8", errors="replace") as handle:
            findings.extend(scan_text(handle.read(), path))
    result = {"passed": not findings, "findings": findings}
    if args.json_output:
        with open(args.json_output, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2)
            handle.write("\n")
    if findings:
        for finding in findings:
            location = finding.get("path", "unknown")
            if finding.get("line"):
                location += ":" + str(finding["line"])
            print(f"credential finding: {finding['kind']} at {location}")
        return 1
    print("PR credential diff scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())