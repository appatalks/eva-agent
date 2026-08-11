#!/usr/bin/env python3
"""Strictly parse a Terra PR-readiness verdict from untrusted output."""

import argparse
import hashlib
import json
import re
from pathlib import Path


_VERDICT = re.compile(r"VERDICT: (APPROVE|REQUEST_CHANGES|NEEDS_MAINTAINER)")


def parse_verdict(review_bytes):
    """Return one exact verdict line, or a non-trusting failure kind."""
    try:
        review = bytes(review_bytes).decode("utf-8")
    except UnicodeDecodeError:
        return {"valid": False, "kind": "malformed"}

    if not review.strip():
        return {"valid": False, "kind": "empty"}

    lines = review.splitlines()
    verdicts = [match for line in lines if (match := _VERDICT.fullmatch(line))]
    if len(verdicts) != 1:
        return {"valid": False, "kind": "malformed"}

    return {"valid": True, "kind": "valid", "verdict": verdicts[0].group(1)}


def write_diagnostic(review_bytes, result, output_path):
    """Write non-sensitive response metadata for an invalid verdict artifact."""
    Path(output_path).write_text(json.dumps({
        "response_kind": result["kind"],
        "response_bytes": len(review_bytes),
        "nonempty_lines": sum(bool(line.strip()) for line in review_bytes.splitlines()),
        "response_sha256": hashlib.sha256(review_bytes).hexdigest(),
    }, separators=(",", ":")) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("review_path")
    parser.add_argument("--diagnostic-output")
    args = parser.parse_args()
    review = Path(args.review_path).read_bytes()
    result = parse_verdict(review)
    if args.diagnostic_output:
        write_diagnostic(review, result, args.diagnostic_output)
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()