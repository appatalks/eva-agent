"""Strict bounded MCP request framing shared by bundled stdio servers."""

import json
import math
import re

MAX_MCP_FRAME_BYTES = 1024 * 1024


class MCPProtocolError(ValueError):
    """An inbound MCP frame violates the supported release contract."""


FIXED_READ_TOOL_CONTRACTS = {
    "kusto_list_databases": ({}, frozenset()),
    "kusto_show_tables": ({}, frozenset()),
    "kusto_show_schema": ({"table": "identifier"}, frozenset({"table"})),
    "kusto_sample_data": ({"table": "identifier", "count": "count"}, frozenset({"table"})),
    "eva_recall_knowledge": ({"entity": "text", "limit": "count"}, frozenset({"entity"})),
    "eva_get_emotion_state": ({}, frozenset()),
    "eva_get_recent_reflections": ({"limit": "count"}, frozenset()),
    "eva_get_active_goals": ({"category": "category", "limit": "count"}, frozenset()),
    "eva_get_memory_summary": ({"period": "text", "limit": "count"}, frozenset()),
    "web_search": ({"query": "text", "max_results": "search_count"}, frozenset({"query"})),
    "web_search_news": ({"query": "text", "max_results": "search_count"}, frozenset({"query"})),
}


def validate_fixed_tool_arguments(tool_name, arguments):
    contract = FIXED_READ_TOOL_CONTRACTS.get(tool_name)
    if contract is None or not isinstance(arguments, dict):
        raise MCPProtocolError("unsupported MCP tool")
    fields, required = contract
    if set(arguments) - set(fields) or not required.issubset(arguments):
        raise MCPProtocolError("invalid MCP tool fields")
    for name, value in arguments.items():
        kind = fields[name]
        if kind == "identifier" and (
            not isinstance(value, str)
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", value) is None
        ):
            raise MCPProtocolError("invalid MCP identifier")
        if kind == "text" and (
            not isinstance(value, str) or not value or len(value) > 2000
            or "\x00" in value
        ):
            raise MCPProtocolError("invalid MCP text")
        if kind == "category" and value not in (
            "self_improvement", "knowledge_curation", "relational"
        ):
            raise MCPProtocolError("invalid MCP category")
        if kind == "count" and (
            isinstance(value, bool) or not isinstance(value, int)
            or not 1 <= value <= 100
        ):
            raise MCPProtocolError("invalid MCP count")
        if kind == "search_count" and (
            isinstance(value, bool) or not isinstance(value, int)
            or not 1 <= value <= 20
        ):
            raise MCPProtocolError("invalid MCP search count")
    return arguments


def fixed_tool_schema(tool_name):
    fields, required = FIXED_READ_TOOL_CONTRACTS[tool_name]
    properties = {
        name: {"type": "integer" if kind in ("count", "search_count") else "string"}
        for name, kind in fields.items()
    }
    return closed_schema(properties, sorted(required))


def decode_request_line(raw):
    if isinstance(raw, str):
        encoded = raw.encode("utf-8", errors="strict")
    elif isinstance(raw, bytes):
        encoded = raw
    else:
        raise MCPProtocolError("invalid MCP frame type")
    if not encoded or len(encoded) > MAX_MCP_FRAME_BYTES:
        raise MCPProtocolError("invalid MCP frame size")
    try:
        text = encoded.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise MCPProtocolError("invalid MCP UTF-8") from exc
    if not text:
        raise MCPProtocolError("empty MCP frame")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise MCPProtocolError("duplicate MCP member")
            result[key] = value
        return result

    def reject_constant(_value):
        raise MCPProtocolError("non-standard MCP number")

    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            raise MCPProtocolError("non-finite MCP number")
        return result

    try:
        message = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
            parse_float=finite_float,
        )
    except (json.JSONDecodeError, UnicodeError, RecursionError, ValueError) as exc:
        if isinstance(exc, MCPProtocolError):
            raise
        raise MCPProtocolError("invalid MCP JSON") from exc
    if not isinstance(message, dict):
        raise MCPProtocolError("MCP frame must be an object")
    if message.get("jsonrpc") != "2.0":
        raise MCPProtocolError("invalid MCP JSON-RPC version")
    method = message.get("method")
    if not isinstance(method, str) or not method or len(method) > 128:
        raise MCPProtocolError("invalid MCP method")
    has_id = "id" in message
    if has_id and (
        type(message["id"]) is not int or not 1 <= message["id"] <= 2147483647
    ):
        raise MCPProtocolError("invalid MCP id")
    if method == "notifications/initialized":
        if has_id or set(message) != {"jsonrpc", "method"}:
            raise MCPProtocolError("invalid MCP initialized notification")
        return message
    expected = {"jsonrpc", "method", "params"}
    if has_id:
        expected.add("id")
    if set(message) != expected or not isinstance(message.get("params"), dict):
        raise MCPProtocolError("invalid MCP request envelope")
    params = message["params"]

    if method == "initialize":
        if not has_id or set(params) != {
            "protocolVersion", "capabilities", "clientInfo"
        }:
            raise MCPProtocolError("invalid MCP initialize request")
        if (
            params.get("protocolVersion") != "2024-11-05"
            or not isinstance(params.get("capabilities"), dict)
            or not isinstance(params.get("clientInfo"), dict)
            or set(params["clientInfo"]) - {"name", "title", "version"}
            or not isinstance(params["clientInfo"].get("name"), str)
            or not params["clientInfo"]["name"]
            or not isinstance(params["clientInfo"].get("version"), str)
            or not params["clientInfo"]["version"]
        ):
            raise MCPProtocolError("invalid MCP initialize metadata")
    elif method == "tools/list":
        if not has_id or params:
            raise MCPProtocolError("MCP pagination is unsupported")
    elif method == "tools/call":
        if (
            not has_id or set(params) != {"name", "arguments"}
            or not isinstance(params.get("name"), str)
            or not params["name"] or len(params["name"]) > 128
            or not isinstance(params.get("arguments"), dict)
        ):
            raise MCPProtocolError("invalid MCP tool call")
    elif method == "ping":
        if not has_id or params:
            raise MCPProtocolError("invalid MCP ping")
    return message


def closed_schema(properties, required=()):
    schema = {
        "type": "object",
        "properties": dict(properties),
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


def encode_response_line(message):
    """Encode one bounded JSON-RPC response, replacing oversized results."""
    try:
        line = json.dumps(
            message, sort_keys=True, separators=(",", ":"), allow_nan=False
        ) + "\n"
    except (TypeError, ValueError, RecursionError):
        line = ""
    if line and len(line.encode("utf-8")) <= MAX_MCP_FRAME_BYTES:
        return line
    rid = message.get("id") if isinstance(message, dict) else None
    fallback = {
        "jsonrpc": "2.0", "id": rid,
        "error": {"code": -32001, "message": "MCP response exceeded the limit"},
    }
    return json.dumps(fallback, separators=(",", ":"), allow_nan=False) + "\n"
