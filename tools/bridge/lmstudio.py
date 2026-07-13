"""Validated LM Studio transport shared by every bridge caller."""

import json
import math

from bridge.utils import _validate_lmstudio_base_url

_MODEL_RESPONSE_MAX_BYTES = 2 * 1024 * 1024
_CHAT_RESPONSE_MAX_BYTES = 4 * 1024 * 1024


def _strict_response_json(response, max_bytes):
    content_type = str(response.headers.get("Content-Type", "")).lower()
    if content_type.split(";", 1)[0].strip() != "application/json":
        raise ValueError("LM Studio returned a non-JSON content type")
    raw_length = response.headers.get("Content-Length")
    if raw_length is not None:
        if not str(raw_length).isdigit() or int(raw_length) > max_bytes:
            raise ValueError("LM Studio response is too large")
    chunks = []
    total = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("LM Studio response is too large")
        chunks.append(chunk)

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("LM Studio response has duplicate members")
            result[key] = value
        return result

    def finite_float(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("LM Studio response has a non-finite number")
        return parsed

    text = b"".join(chunks).decode("utf-8", errors="strict")
    return json.loads(
        text, object_pairs_hook=unique_object,
        parse_constant=lambda _value: (_ for _ in ()).throw(
            ValueError("LM Studio response has a non-standard number")
        ),
        parse_float=finite_float,
    )


def get_models(base_url, timeout=10):
    """GET the validated LM Studio model catalog without environment transport."""
    normalized, error = _validate_lmstudio_base_url(base_url)
    if error:
        return 0, None, error
    try:
        import requests
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(
                normalized.rstrip("/") + "/models",
                timeout=timeout, allow_redirects=False, stream=True,
            )
    except Exception as exc:
        return 0, None, str(exc)
    if 300 <= response.status_code < 400:
        return response.status_code, None, "LM Studio redirects are not allowed"
    if response.status_code != 200:
        return response.status_code, None, f"LM Studio returned HTTP {response.status_code}"
    try:
        data = _strict_response_json(response, _MODEL_RESPONSE_MAX_BYTES)
    except (UnicodeError, ValueError, json.JSONDecodeError):
        return response.status_code, None, "LM Studio returned invalid or oversized JSON"
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) > 1024:
        return response.status_code, None, "LM Studio returned an invalid model catalog"
    models = []
    for row in rows:
        model_id = row.get("id") if isinstance(row, dict) else None
        if (
            not isinstance(model_id, str) or not model_id
            or len(model_id) > 256 or any(ord(char) < 0x20 for char in model_id)
        ):
            return response.status_code, None, "LM Studio returned an invalid model catalog"
        models.append({"id": model_id})
    return response.status_code, {"data": models}, ""


def post_json(base_url, payload, timeout=120):
    """POST an OpenAI-compatible payload without following redirects.

    Returns ``(status_code, json_body, error)``. The endpoint is validated
    against the active egress policy before every request. Redirect responses
    are returned to the caller as errors rather than followed.
    """
    normalized, error = _validate_lmstudio_base_url(base_url)
    if error:
        return 0, None, error

    try:
        import requests
        with requests.Session() as session:
            session.trust_env = False
            response = session.post(
                normalized.rstrip("/") + "/chat/completions",
                json=payload,
                timeout=timeout,
                allow_redirects=False, stream=True,
            )
    except Exception as exc:
        return 0, None, str(exc)

    if 300 <= response.status_code < 400:
        return response.status_code, None, "LM Studio redirects are not allowed"
    if response.status_code != 200:
        return response.status_code, None, f"LM Studio returned HTTP {response.status_code}"
    try:
        return response.status_code, _strict_response_json(
            response, _CHAT_RESPONSE_MAX_BYTES
        ), ""
    except (UnicodeError, ValueError, json.JSONDecodeError):
        return response.status_code, None, "LM Studio returned invalid or oversized JSON"
