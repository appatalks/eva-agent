"""Validated LM Studio transport shared by every bridge caller."""

from bridge.utils import _validate_lmstudio_base_url


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
                allow_redirects=False,
            )
    except Exception as exc:
        return 0, None, str(exc)

    if 300 <= response.status_code < 400:
        return response.status_code, None, "LM Studio redirects are not allowed"
    if response.status_code != 200:
        return response.status_code, None, f"LM Studio returned HTTP {response.status_code}"
    try:
        return response.status_code, response.json(), ""
    except ValueError:
        return response.status_code, None, "LM Studio returned invalid JSON"
