"""Pure request normalization and conversational action continuity helpers."""

import re


_GITHUB_CONTINUATION_RE = re.compile(
    r"^\s*(?:(?:sounds? good|okay|ok|yes|approved?)[,;:]?\s*)?"
    r"(?:please\s+)?(?:proceed|continue|go ahead|finish(?: it)?|do it)\s*[.!?]*\s*$|"
    r"^\s*(?:i(?:'m| am)?\s+)?not\s+seeing\s+anything\s+(?:being\s+)?(?:run|happen|execute)[.!?]*\s*$",
    re.IGNORECASE,
)
_ACTION_STATUS_RE = re.compile(
    r"^\s*(?:did|have)\s+you\s+(?:do|done|finish|finished|complete|completed|submit|submitted|post|posted|create|created)\s+(?:it|that|the\s+(?:issue|task))\s*[?!.,]*\s*$|"
    r"^\s*is\s+(?:it|that|the\s+(?:issue|task))\s+(?:done|finished|complete|completed|submitted|posted|created)\s*[?!.,]*\s*$",
    re.IGNORECASE,
)
_GITHUB_ISSUE_URL_RE = re.compile(
    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/\d+"
)


def github_continuation_routing_message(messages, user_message, classify_request):
    """Restore a recent GitHub mutation intent only for explicit continuation turns."""
    current = str(user_message or "").strip()
    if not _GITHUB_CONTINUATION_RE.fullmatch(current):
        return current
    prior_users = []
    skipped_current = False
    for message in reversed(messages or []):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content", "")
        if not isinstance(content, str):
            continue
        if not skipped_current and content.strip() == current:
            skipped_current = True
            continue
        prior_users.append(content.strip())
        if len(prior_users) >= 6:
            break
    for prior in prior_users:
        if classify_request(prior.lower()) == "github-data" and github_mutation_request(prior):
            return prior + "\n\nExplicit user continuation: " + current
    return current


def github_mutation_request(message):
    """Return true for an explicit GitHub mutation, not read-only inspection."""
    text = str(message or "").lower()
    return bool(
        re.search(r"\b(?:github|github\.com)\b", text)
        and re.search(
            r"\b(?:create|open|submit|publish|post|update|edit|close|comment|merge|delete|remove|"
            r"push|trigger|rerun|dispatch)\b",
            text,
        )
        and re.search(r"\b(?:issue|pull\s+request|pr|comment|branch|workflow|release|repository|repo)\b", text)
    )


def github_issue_creation_request(message):
    """Return true only for explicit create/submit/publish GitHub issue requests."""
    text = str(message or "").lower()
    return bool(
        re.search(r"\b(?:github|github\.com)\b", text)
        and re.search(r"\b(?:create|open|submit|publish|post)\b", text)
        and re.search(r"\bissues?\b", text)
    )


def verified_github_issue_url(value):
    """Return the canonical issue URL contained in a tool receipt, if present."""
    match = _GITHUB_ISSUE_URL_RE.search(str(value or ""))
    return match.group(0) if match else ""


def verified_action_status_reply(messages, user_message):
    """Answer terse action-status questions only from the immediately preceding receipt."""
    if not _ACTION_STATUS_RE.fullmatch(str(user_message or "").strip()):
        return ""
    skipped_current = False
    for message in reversed(messages or []):
        if not isinstance(message, dict):
            continue
        content = message.get("content", "")
        if not isinstance(content, str):
            continue
        if message.get("role") == "user" and not skipped_current and content.strip() == str(user_message or "").strip():
            skipped_current = True
            continue
        if message.get("role") != "assistant":
            continue
        issue_url = verified_github_issue_url(content)
        if issue_url:
            return "Yes. The GitHub issue was created: " + issue_url
        if re.search(
            r"\b(?:not yet|has not been|hasn't been|was not|wasn't|no verified|no execution receipt|"
            r"could not|couldn't|failed|rejected)\b",
            content,
            re.IGNORECASE,
        ):
            return "No. I do not have a verified completion receipt for that action."
        return "I do not have a verified completion receipt for that action."
    return "I do not have a verified completion receipt for that action."


def normalize_aig_request(
    data,
    parse_backend,
    completion_token_limit,
    allowed_reasoning_efforts,
    openai_api_key,
):
    """Validate AIG request inputs and derive handler routing flags.

    The HTTP handler remains responsible for parsing request bodies and writing
    responses. This helper deliberately performs no I/O, telemetry, memory, or
    model execution so its compatibility rules can be tested in isolation.
    """
    messages = data.get("messages", [])
    user_message = data.get("user_message", "")
    translation_mode = bool(data.get("translation_mode"))
    native_terminal_candidate = bool(data.get("native_terminal_candidate"))
    native_terminal_plan = bool(data.get("native_terminal_plan")) or native_terminal_candidate
    internal = bool(data.get("internal")) or translation_mode or native_terminal_plan
    inject_memory = bool(data.get("inject_memory"))
    recall_query = (data.get("recall_query") or "").strip()
    no_tools = bool(data.get("no_tools")) or translation_mode or native_terminal_plan
    conversation_id = str(data.get("session_id") or data.get("conversation_id") or "").strip()[:120]
    requested_backend = data.get("model", "gpt-5.6-luna")
    responder_provider, model_for_response = parse_backend(requested_backend)
    model_policy_mode = str(data.get("model_policy_mode") or "pinned").strip().lower()
    if model_policy_mode not in {"pinned", "auto-balanced", "auto-fast"}:
        model_policy_mode = "pinned"

    if responder_provider == "openai" and not openai_api_key and model_policy_mode == "pinned":
        raise ValueError("An OpenAI API key is required for the selected Eva backend.")

    max_completion_tokens = completion_token_limit(data.get("max_completion_tokens"))
    raw_reasoning_effort = data.get("acp_reasoning_effort", "")
    if not isinstance(raw_reasoning_effort, str) or raw_reasoning_effort not in allowed_reasoning_efforts | {""}:
        raise ValueError("Unsupported acp_reasoning_effort")
    acp_auto_approve = data.get("acp_auto_approve", False)
    if not isinstance(acp_auto_approve, bool):
        raise ValueError("acp_auto_approve must be a boolean")

    if not user_message and messages:
        for message in reversed(messages):
            if message.get("role") == "user":
                user_message = message.get("content", "")
                break
    if not user_message:
        raise ValueError("No user message provided")

    return {
        "messages": messages,
        "user_message": user_message,
        "translation_mode": translation_mode,
        "native_terminal_candidate": native_terminal_candidate,
        "native_terminal_plan": native_terminal_plan,
        "internal": internal,
        "inject_memory": inject_memory,
        "recall_query": recall_query,
        "no_tools": no_tools,
        "conversation_id": conversation_id,
        "requested_backend": requested_backend,
        "model_policy_mode": model_policy_mode,
        "responder_provider": responder_provider,
        "model_for_response": model_for_response,
        "max_completion_tokens": max_completion_tokens,
        "reasoning_effort": raw_reasoning_effort,
        "acp_auto_approve": acp_auto_approve,
        "stream_requested": data.get("stream") is True,
    }