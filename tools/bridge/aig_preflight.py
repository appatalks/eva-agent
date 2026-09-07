"""Pure ACP preflight planning for Eva AIG requests."""

import re

_GREETING_RE = re.compile(
    r"^(hi|hey|hello|howdy|yo|sup|good morning|good evening|good afternoon|thanks|thank you|ok|okay|bye|goodbye|see you|great|cool|nice|sure|yes|no|nah|yep|nope)\b"
)
_META_RE = re.compile(
    r"^(how are you|how do you feel|what is your name|who are you|what can you do|tell me about yourself)\b"
)
_BRIEFING_RE = re.compile(r"\b(?:morning|daily)\s+(?:briefing|report|update)\b")


def plan_aig_preflight(
    routing_message,
    request_type,
    fast_route,
    internal,
    force_retrieve,
    acp_available,
    local_mode,
    no_tools,
    needs_preflight,
    select_tool_profile,
):
    """Return deterministic retrieval/preflight decisions without I/O."""
    message = str(routing_message or "")
    message_lower = message.lower()
    message_stripped = re.sub(r"[^\w\s]", "", message_lower).strip()
    words = message_stripped.split()
    skip_acp = False
    acp_route = "default"
    briefing_request = bool(_BRIEFING_RE.search(message_lower))
    tool_request = request_type in {
        "news-search", "weather-search", "financial-data", "web-search",
        "github-data", "kusto-query", "kusto-operator",
    } and not briefing_request

    if no_tools:
        skip_acp = True
        acp_route = "no-tools"
    elif fast_route:
        skip_acp = True
        acp_route = "fast/" + fast_route
    elif internal and not force_retrieve and not tool_request:
        skip_acp = True
        acp_route = "internal-cognition"
    elif not acp_available and not local_mode:
        skip_acp = True
        acp_route = "acp-unavailable"
    elif len(words) <= 4 and _GREETING_RE.match(message_stripped):
        skip_acp = True
        acp_route = "greeting/trivial"
    elif len(words) <= 6 and _META_RE.match(message_stripped):
        skip_acp = True
        acp_route = "meta-question"

    needs_tools = not briefing_request and not skip_acp and needs_preflight(message_lower, request_type)
    tool_profile = select_tool_profile(
        message, request_type, fast_route=fast_route, no_tools=no_tools
    ) if needs_tools else "none"
    escalation = "fast-responder" if fast_route else (
        "acp-preflight" if needs_tools else "direct-responder"
    )
    if not skip_acp and not needs_tools:
        skip_acp = True
        acp_route = "direct/general"

    return {
        "skip_acp": skip_acp,
        "acp_route": acp_route,
        "briefing_request": briefing_request,
        "needs_acp_tools": needs_tools,
        "tool_profile": tool_profile,
        "escalation": escalation,
    }


def should_fallback_local_tool_to_acp(local_mode, tool_required, local_result, responder_provider, acp_available):
    """Use ACP when automatic/pinned ACP routing outlives unavailable local tool-calling."""
    return bool(
        local_mode and tool_required and not local_result
        and responder_provider == "acp" and acp_available
    )