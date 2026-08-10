#!/usr/bin/env python3
"""Parse strict maintainer readiness commands without exposing comment content."""

import json
import re
import sys


_COMMAND = re.compile(r"/eva-readiness (approve|request-changes) ([0-9a-f]{40})\Z")
_DECISIONS = {
    "approve": {"state": "success", "description": "Maintainer approved readiness"},
    "request-changes": {"state": "failure", "description": "Maintainer requested changes"},
}


def parse_command(body):
    match = _COMMAND.fullmatch(str(body or ""))
    if not match:
        return {"valid": False}
    decision, head_sha = match.groups()
    result = dict(_DECISIONS[decision])
    result.update({"valid": True, "decision": decision, "head_sha": head_sha})
    return result


if __name__ == "__main__":
    print(json.dumps(parse_command(sys.stdin.read()), separators=(",", ":")))