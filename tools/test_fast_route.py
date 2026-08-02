#!/usr/bin/env python3
"""Deterministic fast-route and escalation contract tests."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from bridge.utils import (_classify_fast_route, _classify_request_type,
    _effective_routing_message,
    _is_passive_memory_recall, _needs_acp_preflight, _passive_recall_session_key,
    _select_acp_tool_profile)


def main():
    fast_cases = {
        "Hi Eva.": "greeting",
        "What is 2 + 2?": "basic-arithmetic",
        "calculate (12 / 3) + 1": "basic-arithmetic",
        "What is today's date?": "date-time",
        "What time is it right now?": "date-time",
    }
    for message, expected in fast_cases.items():
        assert _classify_fast_route(message) == expected, message

    non_fast_cases = [
        "Use cognition to solve a tank fill problem.",
        "What is the current stock price for AAPL?",
        "Search GitHub for a function.",
        "Send a Signal message saying hello.",
        "What is 2 + 2 in binary?",
        "What is the weather today?",
    ]
    for message in non_fast_cases:
        assert _classify_fast_route(message) == "", message

    assert _needs_acp_preflight(
        "search github for a function",
        _classify_request_type("search github for a function"),
    )
    assert _needs_acp_preflight(
        "what is the current stock price for aapl",
        _classify_request_type("what is the current stock price for aapl"),
    )
    for message in (
        "Do you remember your original design concept?",
        "What was the original concept behind your memory?",
        "What do you remember about Lily?",
    ):
        assert _is_passive_memory_recall(message), message
        assert _select_acp_tool_profile(message) == "none", message
    assert not _is_passive_memory_recall("Delete that memory")
    assert not _is_passive_memory_recall("Run a query against the memory table")
    assert _passive_recall_session_key("session-a") == "session-a:recall"
    anonymous_a = _passive_recall_session_key("")
    anonymous_b = _passive_recall_session_key(None)
    assert anonymous_a.startswith("anonymous-recall-")
    assert anonymous_b.startswith("anonymous-recall-")
    assert anonymous_a != anonymous_b

    wrapped_prompt = (
        "User message:\nDo you remember what we were working on before our session?\n"
        "Phrases like what's the news = answer inline."
    )
    recall_query = "Do you remember what we were working on before our session?"
    routed_recall = _effective_routing_message(wrapped_prompt, True, recall_query)
    assert routed_recall == recall_query
    assert _classify_request_type(routed_recall.lower()) == "general"
    assert _is_passive_memory_recall(routed_recall)
    assert not _needs_acp_preflight(routed_recall.lower(), "general")

    news_query = "What's the news today?"
    routed_news = _effective_routing_message(wrapped_prompt, True, news_query)
    assert _classify_request_type(routed_news.lower()) == "news-search"
    assert _needs_acp_preflight(routed_news.lower(), "news-search")
    print("fast route tests: PASS")


if __name__ == "__main__":
    main()
