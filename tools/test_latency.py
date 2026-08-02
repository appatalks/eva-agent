#!/usr/bin/env python3
"""Production-shaped latency probe for Eva's AIG bridge.

The probe measures one fast request or the draft/reviewer/revision pipeline.
Revision is sent only after a REQUEST_CHANGES verdict. NDJSON timings are
measured from the HTTP wire, with cold/warm labels and optional server metrics.
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
import uuid

DEFAULT_PROMPT = "What is 2 + 2?"
DEFAULT_SYSTEM = "You are a helpful assistant. Be brief."


class BridgeUnavailable(RuntimeError):
    pass


def _request_payload(prompt, model, session_id, route, internal=False, no_tools=False,
                    inject_memory=False, retrieve_data=False, messages=None,
                    stage="fast", warm_label="cold"):
    return {
        "messages": messages or [
            {"role": "system", "content": DEFAULT_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "user_message": prompt,
        "model": model,
        "internal": internal,
        "no_tools": no_tools,
        "inject_memory": inject_memory,
        "recall_query": prompt if inject_memory else "",
        "retrieve_data": retrieve_data,
        "session_id": session_id,
        "stream": True,
        "production": True,
        "latency_probe": True,
        "latency_stage": stage,
        "latency_warm": warm_label,
        "route": route,
        "github_pat": "",
        "lmstudio_base_url": "",
        "lmstudio_model": "",
    }


def _extract_content(body):
    choices = body.get("choices") if isinstance(body, dict) else None
    if choices:
        message = choices[0].get("message", {}) or {}
        return str(message.get("content", "") or "")
    return ""


def _response_metrics(body):
    if not isinstance(body, dict):
        return {}
    metrics = body.get("metrics")
    if isinstance(metrics, dict):
        return metrics
    metrics = {}
    for key in ("usage", "route", "model", "model_used", "prompt_budget", "components", "tokens"):
        if key in body:
            metrics[key] = body[key]
    return metrics


def stream_call(bridge_url, payload, timeout=120):
    """POST one production request and return privacy-safe wire timings."""
    url = bridge_url.rstrip("/") + "/v1/aig/chat"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/x-ndjson"},
        method="POST",
    )
    started = time.perf_counter()
    first_chunk = None
    chunks = []
    body = {}
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            if "ndjson" in content_type or payload.get("stream"):
                for raw_line in response:
                    if not raw_line.strip():
                        continue
                    event = json.loads(raw_line.decode("utf-8"))
                    if event.get("type") == "chunk":
                        if first_chunk is None:
                            first_chunk = time.perf_counter()
                        chunks.append(str(event.get("text", "") or ""))
                    elif event.get("type") == "done":
                        body = event.get("response") or {}
                        if isinstance(event.get("metrics"), dict):
                            body = dict(body)
                            body["metrics"] = event["metrics"]
            else:
                body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise BridgeUnavailable(f"bridge unavailable at {url}: {error}") from error
    total_ms = round((time.perf_counter() - started) * 1000.0, 1)
    content = "".join(chunks) or _extract_content(body)
    metrics = _response_metrics(body)
    return {
        "stage": payload.get("latency_stage", "unknown"),
        "route": metrics.get("route", payload.get("route", "")),
        "model": metrics.get("model", body.get("model", payload.get("model", ""))),
        "ttft_ms": round((first_chunk - started) * 1000.0, 1) if first_chunk else None,
        "total_ms": total_ms,
        "completion_ms": total_ms,
        "chunk_count": len(chunks),
        "response_chars": len(content),
        "metrics": metrics,
        "content": content,
    }


def _parse_verdict(content):
    match = re.search(r"VERDICT\s*:\s*(APPROVE|REQUEST[_\- ]?CHANGES|BLOCKED)", content or "", re.I)
    if not match:
        return "REQUEST_CHANGES"
    value = match.group(1).upper().replace("-", "_").replace(" ", "_")
    return "REQUEST_CHANGES" if value == "REQUEST_CHANGES" else value


def run_fast_pass(bridge_url, model, prompt, session_id, route, timeout, warm_label):
    payload = _request_payload(
        prompt, model, session_id, route, stage="fast", warm_label=warm_label
    )
    return {"mode": "fast", "calls": [stream_call(bridge_url, payload, timeout)]}


def run_cognition_pass(bridge_url, eva_model, reviewer_model, prompt, session_id,
                       route, timeout, warm_label):
    calls = []
    draft_payload = _request_payload(
        prompt, eva_model, session_id, route, internal=True,
        inject_memory=True, retrieve_data=True, stage="draft", warm_label=warm_label,
    )
    draft = stream_call(bridge_url, draft_payload, timeout)
    calls.append(draft)
    review_prompt = (
        f"User message:\n{prompt}\n\nEva draft:\n{draft['content']}\n\n"
        "Review the draft. First line MUST be VERDICT: APPROVE or "
        "VERDICT: REQUEST_CHANGES. Only request material changes."
    )
    review_payload = _request_payload(
        review_prompt, reviewer_model, session_id, route, internal=True,
        no_tools=True, messages=[
            {"role": "system", "content": "You are a text-only reviewer. Do not use tools."},
            {"role": "user", "content": review_prompt},
        ], stage="review", warm_label=warm_label,
    )
    review = stream_call(bridge_url, review_payload, timeout)
    review["verdict"] = _parse_verdict(review["content"])
    calls.append(review)
    if review["verdict"] == "REQUEST_CHANGES":
        revise_prompt = (
            f"User message:\n{prompt}\n\nPrevious draft:\n{draft['content']}\n\n"
            f"Reviewer feedback:\n{review['content']}\n\nProduce the revised final answer."
        )
        revise_payload = _request_payload(
            revise_prompt, eva_model, session_id, route, internal=True,
            inject_memory=True, stage="revision", warm_label=warm_label,
        )
        calls.append(stream_call(bridge_url, revise_payload, timeout))
    return {"mode": "cognition", "verdict": review["verdict"], "calls": calls}


def fetch_telemetry(bridge_url, timeout=5):
    """Read optional server metrics without making the probe depend on them."""
    url = bridge_url.rstrip("/") + "/v1/telemetry?limit=50"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def run_probe(args):
    session_id = args.session_id or "latency-" + uuid.uuid4().hex[:12]
    results = []
    modes = [args.mode] if args.mode != "both" else ["fast", "cognition"]
    for mode in modes:
        for repetition in range(max(1, args.repetitions)):
            # The harness does not restart the bridge, so it cannot claim a
            # process-level cold start. Server pool telemetry reports real hits.
            warm_label = "first-observed" if repetition == 0 else "subsequent"
            if mode == "fast":
                result = run_fast_pass(
                    args.bridge, args.eva_model, args.prompt, session_id,
                    args.route, args.timeout, warm_label,
                )
            else:
                result = run_cognition_pass(
                    args.bridge, args.eva_model, args.reviewer_model, args.prompt,
                    session_id, args.route, args.timeout, warm_label,
                )
            result.update({"repetition": repetition + 1, "warm": warm_label, "session_id": session_id})
            for call in result["calls"]:
                call.pop("content", None)
            results.append(result)
    telemetry = fetch_telemetry(args.bridge)
    return {"bridge": args.bridge, "session_id": session_id, "results": results, "telemetry": telemetry}


def _print_report(report):
    print(f"Bridge: {report['bridge']}")
    print(f"Session: {report['session_id']}")
    for result in report["results"]:
        print(f"\n{result['mode']} repetition {result['repetition']} ({result['warm']})")
        if result.get("verdict"):
            print(f"  verdict: {result['verdict']}")
        for call in result["calls"]:
            ttft = f"{call['ttft_ms']:.1f}ms" if call["ttft_ms"] is not None else "n/a"
            print(f"  {call['stage']}: TTFT={ttft} total={call['total_ms']:.1f}ms chunks={call['chunk_count']}")
            if call["metrics"]:
                print(f"    metrics: {json.dumps(call['metrics'], sort_keys=True)}")
    if report.get("telemetry", {}).get("summary"):
        print("\nServer telemetry summary:")
        print(json.dumps(report["telemetry"]["summary"], indent=2, sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Production-shaped Eva AIG latency probe")
    parser.add_argument("--bridge", default="http://localhost:8888")
    parser.add_argument("--eva-model", default="gpt-5.6-luna")
    parser.add_argument("--reviewer-model", default="gpt-5.6-terra")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--session-id", default="")
    parser.add_argument("--route", default="fast/basic-arithmetic")
    parser.add_argument("--mode", choices=("fast", "cognition", "both"), default="both")
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--threshold-ttft", type=float, default=None)
    parser.add_argument("--threshold-total", type=float, default=None)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    try:
        report = run_probe(args)
    except BridgeUnavailable as error:
        message = {"error": str(error), "bridge": args.bridge}
        if args.as_json:
            print(json.dumps(message))
        else:
            print("Latency probe could not run: " + str(error), file=sys.stderr)
            print("Start the bridge and retry, for example: python3 tools/acp_bridge.py --port 8888", file=sys.stderr)
        return 2
    failures = []
    calls = [call for result in report["results"] for call in result["calls"]]
    if args.threshold_ttft is not None and any(call["ttft_ms"] is not None and call["ttft_ms"] > args.threshold_ttft for call in calls):
        failures.append("TTFT threshold exceeded")
    if args.threshold_total is not None and any(call["total_ms"] > args.threshold_total for call in calls):
        failures.append("total latency threshold exceeded")
    if args.as_json:
        report["threshold_failures"] = failures
        print(json.dumps(report, sort_keys=True))
    else:
        _print_report(report)
        if failures:
            print("\nThresholds: " + "; ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
