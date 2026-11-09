#!/usr/bin/env python3
"""Render a bounded, non-notifying advisory from an untrusted AI review."""

import argparse
import html
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from workspace_diff_guard import scan_text
from readiness_verdict import parse_verdict

MARKER = "<!-- eva-pr-readiness-advisory:"
MAX_FINDING_CHARS = 8000


def render_advisory(review_bytes, head_sha, evidence, scan, model, codeql):
    if not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise ValueError("Invalid reviewed commit.")
    heading = "Review unavailable"
    findings = ""
    if evidence != "success":
        findings = "Evidence could not be collected, so no AI review was performed."
    elif scan != "success":
        findings = "Credential screening did not pass; no AI review was performed. Inspect the workflow and security checks."
    elif model != "success":
        findings = "The AI review did not complete. Inspect the workflow run or retry it."
    elif scan_text(review_bytes.decode("utf-8", errors="replace"), "review"):
        findings = "The AI output was withheld because it may contain credential material."
    else:
        parsed = parse_verdict(review_bytes)
        if not parsed["valid"]:
            findings = "The AI review returned no valid verdict. Inspect the workflow run or retry it."
        else:
            verdict = parsed["verdict"]
            heading = {
                "APPROVE": "No blocking findings reported",
                "REQUEST_CHANGES": "Changes suggested",
                "NEEDS_MAINTAINER": "Maintainer review suggested",
            }[verdict]
            lines = review_bytes.decode("utf-8").splitlines()
            index = lines.index("VERDICT: " + verdict)
            findings = "\n".join(lines[index + 1:]).strip() or "No additional findings were provided."
            findings = "".join(char for char in findings if char in "\n\t" or ord(char) >= 32)
            if len(findings) > MAX_FINDING_CHARS:
                findings = findings[:MAX_FINDING_CHARS] + "\n\n[Findings truncated.]"
            findings = html.escape(findings, quote=False).replace("@", "@\u200b")
    note = ""
    if evidence == "success" and codeql != "available":
        note = "\n\nCode-scanning alerts could not be retrieved; their status is unknown."
    return (
        MARKER + head_sha + " -->\n"
        "### PR readiness advisory — " + heading + "\n\n"
        "Reviewed commit: `" + head_sha + "`\n\n"
        "This AI-generated feedback is advisory and may be incorrect. "
        "Required CI and security checks remain independent.\n\n"
        + findings + note + "\n"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--review", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--scan", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--codeql", required=True)
    args = parser.parse_args()
    path = Path(args.review)
    review = path.read_bytes() if path.is_file() else b""
    Path(args.output).write_text(
        render_advisory(review, args.head_sha, args.evidence, args.scan, args.model, args.codeql),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
