#!/usr/bin/env python3
"""
SQLite MCP Server for Eva

Local-memory replacement for kusto_mcp.py. Implements the same tool names and
signatures so the ACP bridge and Copilot CLI can use it without changes.

Usage:
  As a standalone MCP server (stdio):
    python3 sqlite_mcp.py

  Via the ACP bridge (--additional-mcp-config):
    Configured automatically when memory backend is set to "sqlite".

Environment variables:
  EVA_MEMORY_DB  -- Path to the SQLite database file (default: ~/.eva/memory.db)
"""

import json
import contextlib
import os
import re
import sys

# Import the core SQLite memory module (same directory).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sqlite_memory import SqliteMemory
from bridge.mcp_protocol import (
    MCPProtocolError,
    MAX_MCP_FRAME_BYTES,
    decode_request_line,
    encode_response_line,
    fixed_tool_schema,
    validate_fixed_tool_arguments,
)


class SqliteMCPServer:
    """Minimal MCP server implementing Eva memory tools over SQLite."""

    TOOLS = [
        {
            "name": "kusto_list_databases",
            "description": "List databases. With the local SQLite backend this returns the database file path.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "kusto_query",
            "description": "Execute a SQL query against the local Eva memory database.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "SQL query to execute (e.g. 'SELECT * FROM Knowledge LIMIT 10')",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "kusto_show_tables",
            "description": "Show all tables in the Eva memory database.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "kusto_show_schema",
            "description": "Show the schema (columns and types) for a specific table.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "table": {
                        "type": "string",
                        "description": "Table name to get schema for",
                    },
                },
                "required": ["table"],
            },
        },
        {
            "name": "kusto_sample_data",
            "description": "Get a sample of rows from a table (default 10 rows).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "table": {
                        "type": "string",
                        "description": "Table name to sample from",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of rows to sample (default 10)",
                    },
                },
                "required": ["table"],
            },
        },
        {
            "name": "kusto_ingest_inline",
            "description": "Insert data into a memory table. Use this to store new knowledge, conversations, emotions, reflections, or memory summaries.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "table": {
                        "type": "string",
                        "description": "Target table name (e.g. Knowledge, Conversations, EmotionState, Reflections, MemorySummaries)",
                    },
                    "data": {
                        "type": "array",
                        "description": "Array of row objects. Keys must match column names.",
                        "items": {"type": "object"},
                    },
                },
                "required": ["table", "data"],
            },
        },
        {
            "name": "eva_recall_knowledge",
            "description": "Recall stored knowledge about an entity or topic from the Knowledge table. Uses full-text search for relevance ranking.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "entity": {
                        "type": "string",
                        "description": "Entity or topic to recall knowledge about",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default 20)",
                    },
                },
                "required": ["entity"],
            },
        },
        {
            "name": "eva_get_emotion_state",
            "description": "Get Eva's current emotional state and baseline values.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "eva_get_recent_reflections",
            "description": "Get Eva's recent self-reflections.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max reflections to return (default 5)",
                    },
                },
            },
        },
        {
            "name": "eva_get_active_goals",
            "description": "Get Eva's currently active long-term goals.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Optional filter: self_improvement | knowledge_curation | relational",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max goals to return (default 20)",
                    },
                },
            },
        },
        {
            "name": "eva_get_memory_summary",
            "description": "Get the latest memory summaries.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "description": "Filter by period (optional)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max summaries to return (default 5)",
                    },
                },
            },
        },
    ]
    TOOLS = [
        tool for tool in TOOLS
        if tool.get("name") not in ("kusto_ingest_inline", "kusto_query")
    ]
    for _tool in TOOLS:
        _tool["inputSchema"] = fixed_tool_schema(_tool["name"])

    def __init__(self):
        from bridge import config as bridge_config
        db_path = bridge_config.configured_memory_db_path()
        with contextlib.redirect_stdout(sys.stderr):
            self._mem = SqliteMemory(db_path)
        self._log(f"SQLite memory: {self._mem.db_path}")

    def _log(self, msg):
        sys.stderr.write(f"[sqlite-mcp] {msg}\n")
        sys.stderr.flush()

    # ── Formatting ──────────────────────────────────────────────────────────

    def _format_rows(self, rows, max_rows=100):
        """Format list-of-dicts into readable text (same output style as kusto_mcp)."""
        if not rows:
            return "No results returned."
        cols = list(rows[0].keys())
        lines = [" | ".join(cols), "-" * (len(cols) * 15)]
        for row in rows[:max_rows]:
            lines.append(" | ".join(str(row.get(c, "")) for c in cols))
        if len(rows) > max_rows:
            lines.append(f"... ({len(rows)} total rows, showing first {max_rows})")
        return "\n".join(lines)

    # ── Tool handlers ───────────────────────────────────────────────────────

    def handle_tool(self, name, args):
        if name in ("kusto_query", "kusto_ingest_inline"):
            return "Error: generic SQL is disabled; use a fixed read-only tool."
        try:
            args = validate_fixed_tool_arguments(name, args)
            if name == "kusto_list_databases":
                return "DatabaseName\n------------\nlocal"

            elif name == "kusto_show_tables":
                tables = self._mem.list_tables()
                lines = ["TableName", "----------"]
                lines.extend(tables)
                return "\n".join(lines)

            elif name == "kusto_show_schema":
                table = args.get("table", "")
                if not isinstance(table, str) or re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_]{0,127}", table
                ) is None:
                    return "Error: 'table' must be a valid identifier."
                schema = self._mem.get_schema(table)
                if not schema:
                    return f"Table '{table}' not found."
                lines = ["ColumnName | ColumnType", "---------- | ----------"]
                for col_name, col_type in schema:
                    lines.append(f"{col_name} | {col_type}")
                return "\n".join(lines)

            elif name == "kusto_sample_data":
                table = args.get("table", "")
                if not isinstance(table, str) or re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_]{0,127}", table
                ) is None:
                    return "Error: 'table' must be a valid identifier."
                count = args.get("count", 10)
                if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100:
                    return "Error: 'count' must be an integer from 1 to 100."
                rows = self._mem.sample_rows(table, count)
                return self._format_rows(rows)

            elif name == "eva_recall_knowledge":
                return self._tool_recall_knowledge(args)

            elif name == "eva_get_emotion_state":
                return self._tool_get_emotion_state(args)

            elif name == "eva_get_recent_reflections":
                return self._tool_get_reflections(args)

            elif name == "eva_get_active_goals":
                return self._tool_get_goals(args)

            elif name == "eva_get_memory_summary":
                return self._tool_get_summary(args)

            else:
                return "Error: unsupported tool"

        except MCPProtocolError:
            return "Error: invalid or unsupported tool arguments"
        except Exception:
            return "Error: tool execution failed"

    def _tool_ingest(self, args):
        return "Error: generic MCP writes are disabled; use Eva's authenticated event-first mutation APIs."

    def _tool_recall_knowledge(self, args):
        entity = args.get("entity", "")
        if not entity:
            return "Error: 'entity' parameter is required."
        limit = int(args.get("limit", 20))
        rows = self._mem.fts_search("Knowledge", entity, limit=limit)
        if not rows:
            # Fallback: direct column match
            rows = self._mem.query(
                "SELECT * FROM Knowledge WHERE Entity LIKE ? OR Value LIKE ? "
                "ORDER BY Confidence DESC, Timestamp DESC LIMIT ?",
                (f"%{entity}%", f"%{entity}%", limit),
            )
        return self._format_rows(rows)

    def _tool_get_emotion_state(self, args):
        current = self._mem.query(
            "SELECT * FROM EmotionState ORDER BY Timestamp DESC LIMIT 1"
        )
        baseline = self._mem.query("SELECT * FROM EmotionBaseline")
        parts = []
        parts.append("=== Current Emotion State ===")
        parts.append(self._format_rows(current))
        parts.append("\n=== Emotion Baseline ===")
        parts.append(self._format_rows(baseline))
        return "\n".join(parts)

    def _tool_get_reflections(self, args):
        limit = int(args.get("limit", 5))
        rows = self._mem.query(
            "SELECT * FROM Reflections ORDER BY Timestamp DESC LIMIT ?",
            (limit,),
        )
        return self._format_rows(rows)

    def _tool_get_goals(self, args):
        limit = int(args.get("limit", 20))
        category = str(args.get("category", "") or "").strip()
        allowed = {"self_improvement", "knowledge_curation", "relational"}
        if category and category not in allowed:
            return "Error: category must be one of self_improvement, knowledge_curation, relational."

        sql = (
            "SELECT * FROM Goals WHERE Status = 'active'"
        )
        params = []
        if category:
            sql += " AND Category = ?"
            params.append(category)
        sql += " ORDER BY Priority DESC, UpdatedAt DESC LIMIT ?"
        params.append(limit)
        rows = self._mem.query(sql, tuple(params))
        return self._format_rows(rows)

    def _tool_get_summary(self, args):
        limit = int(args.get("limit", 5))
        period = str(args.get("period", "") or "").strip()
        if period:
            rows = self._mem.query(
                "SELECT * FROM MemorySummaries WHERE Period LIKE ? "
                "ORDER BY Timestamp DESC LIMIT ?",
                (f"%{period}%", limit),
            )
        else:
            rows = self._mem.query(
                "SELECT * FROM MemorySummaries ORDER BY Timestamp DESC LIMIT ?",
                (limit,),
            )
        return self._format_rows(rows)

    # ── MCP stdio protocol ─────────────────────────────────────────────────

    def run(self):
        """Run the MCP server over NDJSON stdio."""
        self._log("Starting SQLite MCP server (stdio)...")

        while True:
            line = sys.stdin.buffer.readline(MAX_MCP_FRAME_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_MCP_FRAME_BYTES or not line.endswith(b"\n"):
                break
            if not line.strip():
                continue
            try:
                msg = decode_request_line(line)
            except MCPProtocolError:
                continue

            method = msg.get("method", "")
            msg_id = msg.get("id")
            params = msg.get("params", {})

            if method == "initialize":
                self._respond(msg_id, {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "sqlite-mcp-server",
                        "version": "1.0.0",
                    },
                })

            elif method == "notifications/initialized":
                pass  # no response needed

            elif method == "tools/list":
                self._respond(msg_id, {"tools": self.TOOLS})

            elif method == "tools/call":
                tool_name = params.get("name", "")
                try:
                    tool_args = validate_fixed_tool_arguments(
                        tool_name, params.get("arguments", {})
                    )
                except MCPProtocolError:
                    self._respond_error(msg_id, -32602, "Invalid tool arguments")
                    continue
                with contextlib.redirect_stdout(sys.stderr):
                    result_text = self.handle_tool(tool_name, tool_args)
                self._respond(msg_id, {
                    "content": [{"type": "text", "text": result_text}],
                })

            elif msg_id is not None:
                # Unknown method with an id -- respond with error
                self._respond_error(msg_id, -32601, f"Method not found: {method}")

    def _respond(self, msg_id, result):
        resp = {"jsonrpc": "2.0", "id": msg_id, "result": result}
        sys.stdout.write(encode_response_line(resp))
        sys.stdout.flush()

    def _respond_error(self, msg_id, code, message):
        resp = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": code, "message": message},
        }
        sys.stdout.write(encode_response_line(resp))
        sys.stdout.flush()


if __name__ == "__main__":
    server = SqliteMCPServer()
    server.run()
