"""A bounded local responder benchmark, without shell execution or user prompts."""

import json
import math
import time
import urllib.request

from bridge.utils import _validate_lmstudio_base_url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def benchmark_local(base_url, model=""):
    base, error = _validate_lmstudio_base_url(base_url)
    if error:
        raise ValueError(error)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    selected = str(model or "").strip()[:240]
    if not selected:
        from bridge.security_urls import observe_request
        observe_request("GET", base + "/models")
        with opener.open(base + "/models", timeout=3) as response:
            models = json.loads(response.read(262144)).get("data") or []
        selected = next((str(item.get("id") or "")[:240] for item in models if isinstance(item, dict) and item.get("id")), "")
    if not selected:
        raise ValueError("LM Studio did not report a loaded model.")
    payload = {
        "model": selected, "stream": False, "max_tokens": 128, "temperature": 0.2,
        "messages": [
            {"role": "system", "content": "You are running a bounded throughput test. Output text only."},
            {"role": "user", "content": "Write a short neutral description of a quiet library, about 150 words."},
        ],
    }
    request = urllib.request.Request(base + "/chat/completions", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    started = time.perf_counter()
    from bridge.security_urls import observe_request
    observe_request("POST", base + "/chat/completions")
    with opener.open(request, timeout=60) as response:
        data = json.loads(response.read(2097152))
    elapsed = time.perf_counter() - started
    usage = data.get("usage") if isinstance(data, dict) else None
    tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
    if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens <= 0 or not math.isfinite(elapsed) or elapsed <= 0:
        return {"status": "unavailable", "message": "The benchmark ran, but the provider did not report usable completion-token usage. No tokens/s value was estimated."}
    return {
        "status": "measured", "model": str(data.get("model") or selected)[:240].replace("[[", "[ [").replace("]]", "] ]"),
        "completion_tokens": tokens, "elapsed_seconds": round(elapsed, 3),
        "tokens_per_second": round(tokens / elapsed, 2), "requested_token_limit": 128,
        "scope": "Provider-reported completion tokens divided by total request time, including queueing, prefill and transport; not engine-only decode speed.",
    }
