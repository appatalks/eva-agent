"""Pure request normalization for the AIG bridge handler."""


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

    if responder_provider == "openai" and not openai_api_key:
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
        "responder_provider": responder_provider,
        "model_for_response": model_for_response,
        "max_completion_tokens": max_completion_tokens,
        "reasoning_effort": raw_reasoning_effort,
        "acp_auto_approve": acp_auto_approve,
        "stream_requested": data.get("stream") is True,
    }