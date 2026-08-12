"""Deterministic model-policy recommendations for Eva request routing."""


def _clean_backend(value, fallback):
    backend = str(value or "").strip()
    return backend or fallback


def select_model_policy(mode, requested_backend, request_type, requires_tools, candidates, local_only=False):
    """Return a stable recommendation without inspecting credentials or making calls.

    ``candidates`` contains only availability facts and approved model identifiers.
    The caller remains responsible for provider-specific validation and execution.
    """
    mode = str(mode or "pinned").strip().lower()
    requested = _clean_backend(requested_backend, "gpt-5.6-luna")
    candidates = candidates if isinstance(candidates, dict) else {}
    acp_model = _clean_backend(candidates.get("acp_model"), requested)
    openai_model = _clean_backend(candidates.get("openai_model"), "gpt-5.6-luna")
    lmstudio_model = _clean_backend(candidates.get("lmstudio_model"), "local")
    acp_available = bool(candidates.get("acp_available"))
    openai_available = bool(candidates.get("openai_available"))
    lmstudio_available = bool(candidates.get("lmstudio_available"))

    if mode not in {"auto-balanced", "auto-fast"}:
        return {"provider": "pinned", "backend": requested, "reason": "pinned"}
    if local_only:
        if lmstudio_available:
            return {"provider": "lmstudio", "backend": "lmstudio", "model": lmstudio_model, "reason": "local-only"}
        return {"provider": "pinned", "backend": requested, "reason": "local-unavailable"}
    if requires_tools:
        if acp_available:
            return {"provider": "acp", "backend": acp_model, "reason": "tool-capability"}
        return {"provider": "pinned", "backend": requested, "reason": "tool-route-unavailable"}
    if mode == "auto-fast":
        if lmstudio_available:
            return {"provider": "lmstudio", "backend": "lmstudio", "model": lmstudio_model, "reason": "fast-local"}
        if openai_available:
            return {"provider": "openai", "backend": "openai:" + openai_model, "reason": "fast-direct"}
    if openai_available:
        return {"provider": "openai", "backend": "openai:" + openai_model, "reason": "balanced-direct"}
    if acp_available:
        return {"provider": "acp", "backend": acp_model, "reason": "balanced-acp"}
    if lmstudio_available:
        return {"provider": "lmstudio", "backend": "lmstudio", "model": lmstudio_model, "reason": "balanced-local"}
    return {"provider": "pinned", "backend": requested, "reason": "no-auto-candidate"}