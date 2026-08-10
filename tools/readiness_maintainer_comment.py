#!/usr/bin/env python3
"""Render a safe maintainer-summary comment from a Terra readiness verdict."""

import argparse
import json
import re


_CATEGORIES = {
    "identity-governance": (
        "Identity and governance",
        "Terra identified an identity or governance decision that needs maintainer judgment.",
    ),
    "security-boundary": (
        "Security boundary",
        "Terra identified a security or trust-boundary decision that needs maintainer judgment.",
    ),
    "release-policy": (
        "Release policy",
        "Terra identified a release, packaging, or deployment decision that needs maintainer judgment.",
    ),
    "product-scope": (
        "Product scope",
        "Terra identified a product-scope decision that needs maintainer judgment.",
    ),
    "test-coverage": (
        "Test coverage",
        "Terra identified a test-coverage decision that needs maintainer judgment.",
    ),
    "other": (
        "Maintainer judgment",
        "Terra identified a decision that needs maintainer judgment.",
    ),
}


def category_from_review(text):
    match = re.search(r"^MAINTAINER_CATEGORY:\s*([a-z-]+)\s*$", str(text or ""), re.MULTILINE)
    category = match.group(1) if match else "other"
    return category if category in _CATEGORIES else "other"


def trusted_marker_comment(comment, head_sha):
    user = (comment or {}).get("user") or {}
    return (
        user.get("login") == "github-actions[bot]"
        and user.get("type") == "Bot"
        and f"<!-- eva-readiness-maintainer:{head_sha} -->" in str((comment or {}).get("body") or "")
    )


def comment_body(review_text, head_sha):
    category = category_from_review(review_text)
    label, summary = _CATEGORIES[category]
    short_sha = str(head_sha or "")[:12]
    return (
        f"<!-- eva-readiness-maintainer:{head_sha} -->\n"
        "## Terra Needs Maintainer Review\n\n"
        f"**Decision area:** {label}\n\n"
        f"{summary}\n\n"
        f"Reviewed commit: `{short_sha}`. The readiness status remains pending until a maintainer decides "
        "whether to merge or request changes.\n\n"
        "To resolve this status for this exact commit, reply with one of:\n\n"
        f"`/eva-readiness approve {head_sha}`\n"
        f"`/eva-readiness request-changes {head_sha}`"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("review_path", nargs="?")
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--has-marker")
    args = parser.parse_args()
    if args.has_marker:
        with open(args.has_marker, encoding="utf-8", errors="replace") as handle:
            comments = json.load(handle)
        raise SystemExit(0 if any(trusted_marker_comment(comment, args.head_sha) for comment in comments) else 1)
    if not args.review_path:
        parser.error("review_path is required unless --has-marker is used")
    with open(args.review_path, encoding="utf-8", errors="replace") as handle:
        print(comment_body(handle.read(), args.head_sha))


if __name__ == "__main__":
    main()