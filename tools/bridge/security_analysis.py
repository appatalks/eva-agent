"""Tool-free analysis of privacy-safe Security Officer event summaries."""

import json
import urllib.error
import urllib.request

from bridge import state
from bridge.acp_client import ACPClient


class SecurityAnalysisError(Exception):
    """A safe, user-visible analysis failure."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def _json_request(url, payload=None, headers=None):
    from bridge.security_urls import observe_request
    observe_request("POST" if payload is not None else "GET", url)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, headers=headers or {}, method="POST" if data is not None else "GET",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=90) as response:
            body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise SecurityAnalysisError("The selected model returned an oversized response.")
            result = json.loads(body)
            if not isinstance(result, dict):
                raise SecurityAnalysisError("The selected model returned an invalid response.")
            return result
    except urllib.error.HTTPError as error:
        raise SecurityAnalysisError(f"The selected model returned HTTP {error.code}.") from error
    except (OSError, ValueError, UnicodeError) as error:
        raise SecurityAnalysisError("The selected model could not complete the security analysis.") from error


def _response_text(response, convert):
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise SecurityAnalysisError("The selected model returned an invalid response.")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise SecurityAnalysisError("The selected model returned an invalid response.")
    if message.get("tool_calls") or choices[0].get("finish_reason") not in {None, "stop", "end_turn"}:
        raise SecurityAnalysisError("The selected model did not complete a tool-free security analysis.")
    return convert(message.get("content", ""))


def analyze(settings, summary):
    from bridge.core import (
        _lmstudio_message_text, _openai_chat_completions_url, _openai_chat_payload,
        _parse_aig_backend, _si_provider_config, _validate_lmstudio_base_url,
    )

    system = (
        "You are Eva's Security Officer reviewing a bounded, privacy-safe event summary. "
        "Use only the supplied counts and fixed boundary facts. Do not invent causes, "
        "attacks, filenames, network attempts, or successful actions. Explain notable "
        "events, uncertainty, and at most three practical next steps. A failed coding "
        "command alone does not establish an attack. Your analysis is advisory only."
    )
    user = "Security summary: " + json.dumps(summary, sort_keys=True)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    selected = settings["model"]
    try:
        provider, model = _parse_aig_backend(selected)
        if selected == "lmstudio":
            base, error = _validate_lmstudio_base_url(settings.get("lmstudio_base_url") or "http://localhost:1234/v1")
            if error:
                raise SecurityAnalysisError(error)
            model = settings.get("lmstudio_model", "").strip()
            if not model:
                discovery = _json_request(base.rstrip("/") + "/models")
                if not isinstance(discovery.get("data"), list):
                    raise SecurityAnalysisError("The selected SI endpoint returned an invalid model list.")
                model = next((
                    item.get("id") for item in discovery.get("data", [])
                    if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]
                ), "")
            if not model:
                raise SecurityAnalysisError("The selected SI endpoint has no loaded model.")
            if len(model) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in model):
                raise SecurityAnalysisError("The selected SI model identifier is invalid.")
            if settings.get("si_runtime", "direct") == "direct":
                response = _json_request(
                    base.rstrip("/") + "/chat/completions",
                    {"model": model, "messages": messages, "max_tokens": 512, "stream": False},
                    {"Content-Type": "application/json"},
                )
                text = _response_text(response, _lmstudio_message_text)
            elif settings.get("si_runtime") == "copilot":
                text = _acp_analysis(model, system, user, _si_provider_config(base, model, 512))
            else:
                raise SecurityAnalysisError("Unsupported SI execution runtime.")
        elif provider == "openai":
            key = settings.get("openai_api_key", "").strip()
            if not key:
                raise SecurityAnalysisError("The selected OpenAI provider requires an API key.")
            response = _json_request(
                _openai_chat_completions_url(), _openai_chat_payload(model, messages, "", 512),
                {"Content-Type": "application/json", "Authorization": "Bearer " + key},
            )
            text = _response_text(response, _lmstudio_message_text)
        else:
            text = _acp_analysis("" if model == "acp" else model, system, user)
        if not isinstance(text, str) or not text.strip():
            raise SecurityAnalysisError("The selected model returned no security analysis.")
        return text.strip()[:4000]
    except ValueError as error:
        raise SecurityAnalysisError(str(error)) from error


def _acp_analysis(model, system, user, provider_config=None):
    template = state.acp_client
    client = ACPClient(
        copilot_path=getattr(template, "copilot_path", "copilot") if template else "copilot",
        model=model or None, mcp_config={}, tool_profile="none", no_tools=True,
        provider_config=provider_config, isolated_reasoning=True,
    )
    try:
        client.start()
        result = client.prompt(
            system + "\n\n" + user, timeout=90, permission_mode="automation_no_tools",
        )
        if (not isinstance(result, dict) or result.get("error") or result.get("permission_cancelled")
                or result.get("stop_reason", "end_turn") != "end_turn"):
            raise SecurityAnalysisError("The selected ACP model could not complete tool-free security analysis.")
        return str(result.get("text") or "")
    except RuntimeError as error:
        raise SecurityAnalysisError("The selected ACP model is unavailable for restricted analysis.") from error
    finally:
        client.stop()
