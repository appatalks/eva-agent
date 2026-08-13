"""Pure route matching for small, fixed bridge HTTP method tables."""

import urllib.parse


PATCH_ROUTES = (
    ("/v1/goals/", "_goals_patch"),
    ("/v1/memory/atoms/", "_memory_atom_patch"),
    ("/v1/skills/", "_skills_patch"),
    ("/v1/cron/", "_cron_update"),
)


def match_patch_route(path):
    """Return ``(handler_name, decoded_id)`` for a known PATCH path."""
    for prefix, handler_name in PATCH_ROUTES:
        if path.startswith(prefix):
            return handler_name, urllib.parse.unquote(path[len(prefix):])
    return None