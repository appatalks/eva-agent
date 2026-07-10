#!/usr/bin/env python3
"""
Kusto MCP Server for Eva
A lightweight MCP (Model Context Protocol) server for Azure Data Explorer (Kusto).
Uses Azure Identity DeviceCodeCredential for authentication — works with personal
Microsoft accounts that have no Azure subscription.

Usage:
  As a standalone MCP server (stdio):
    python3 kusto_mcp.py

  Via the ACP bridge (--additional-mcp-config):
    Configured automatically when you enable "Kusto MCP" in Eva's settings.

Environment variables:
  KUSTO_CLUSTER_URL   — Full cluster URL (e.g. https://kvc-xxx.southcentralus.kusto.windows.net)
  KUSTO_DATABASE      — Default database name (optional)
"""

import json
import os
import sys
import threading

# --- Azure Identity + Kusto SDK ---
try:
    from azure.identity import DeviceCodeCredential, SharedTokenCacheCredential
    import requests as _requests
    HAS_AZURE = True
except ImportError:
    HAS_AZURE = False

# --- MCP Protocol (NDJSON over stdio) ---

class KustoMCPServer:
    """Minimal MCP server implementing tools for Azure Data Explorer."""

    TOOLS = [
        {
            "name": "kusto_list_databases",
            "description": "List all databases in the connected Azure Data Explorer cluster.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "cluster_url": {
                        "type": "string",
                        "description": "Full Kusto cluster URL (e.g. https://kvc-xxx.region.kusto.windows.net). Uses KUSTO_CLUSTER_URL env if not provided."
                    }
                }
            }
        },
        {
            "name": "kusto_query",
            "description": "Execute a KQL (Kusto Query Language) query against an Azure Data Explorer database.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The KQL query to execute (e.g. 'StormEvents | take 10')"
                    },
                    "database": {
                        "type": "string",
                        "description": "Database name to query. Uses KUSTO_DATABASE env if not provided. When KUSTO_DATABASE_LOCKED is set, this argument is ignored and KUSTO_DATABASE is used."
                    },
                    "cluster_url": {
                        "type": "string",
                        "description": "Full Kusto cluster URL. Uses KUSTO_CLUSTER_URL env if not provided."
                    }
                },
                "required": ["query"]
            }
        },
        {
            "name": "kusto_show_tables",
            "description": "Show all tables in a Kusto database.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "database": {
                        "type": "string",
                        "description": "Database name. Uses KUSTO_DATABASE env if not provided. When KUSTO_DATABASE_LOCKED is set, this argument is ignored and KUSTO_DATABASE is used."
                    },
                    "cluster_url": {
                        "type": "string",
                        "description": "Full Kusto cluster URL. Uses KUSTO_CLUSTER_URL env if not provided."
                    }
                }
            }
        },
        {
            "name": "kusto_show_schema",
            "description": "Show the schema (columns and types) for a specific table in a Kusto database.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "table": {
                        "type": "string",
                        "description": "Table name to get schema for"
                    },
                    "database": {
                        "type": "string",
                        "description": "Database name. Uses KUSTO_DATABASE env if not provided. When KUSTO_DATABASE_LOCKED is set, this argument is ignored and KUSTO_DATABASE is used."
                    },
                    "cluster_url": {
                        "type": "string",
                        "description": "Full Kusto cluster URL. Uses KUSTO_CLUSTER_URL env if not provided."
                    }
                },
                "required": ["table"]
            }
        },
        {
            "name": "kusto_sample_data",
            "description": "Get a sample of rows from a Kusto table (default 10 rows).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "table": {
                        "type": "string",
                        "description": "Table name to sample from"
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of rows to sample (default 10)"
                    },
                    "database": {
                        "type": "string",
                        "description": "Database name. Uses KUSTO_DATABASE env if not provided. When KUSTO_DATABASE_LOCKED is set, this argument is ignored and KUSTO_DATABASE is used."
                    },
                    "cluster_url": {
                        "type": "string",
                        "description": "Full Kusto cluster URL. Uses KUSTO_CLUSTER_URL env if not provided."
                    }
                },
                "required": ["table"]
            }
        },
        {
            "name": "kusto_ingest_inline",
            "description": "Ingest (write) data into a Kusto table using inline ingestion. Use this to store new knowledge, conversations, emotions, reflections, or memory summaries.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "table": {
                        "type": "string",
                        "description": "Target table name (e.g. Knowledge, Conversations, EmotionState, Reflections, MemorySummaries, SelfState, HeuristicsIndex)"
                    },
                    "data": {
                        "type": "array",
                        "description": "Array of row objects. Each object's keys must match column names in the target table.",
                        "items": {"type": "object"}
                    },
                    "database": {
                        "type": "string",
                        "description": "Database name. Uses KUSTO_DATABASE env if not provided. When KUSTO_DATABASE_LOCKED is set, this argument is ignored and KUSTO_DATABASE is used."
                    },
                    "cluster_url": {
                        "type": "string",
                        "description": "Full Kusto cluster URL. Uses KUSTO_CLUSTER_URL env if not provided."
                    }
                },
                "required": ["table", "data"]
            }
        },
        {
            "name": "eva_recall_knowledge",
            "description": "Recall Eva's stored knowledge about a specific entity or topic from the Knowledge table. Returns relevant facts with confidence scores.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "entity": {
                        "type": "string",
                        "description": "Entity or topic to recall knowledge about (e.g. 'Steven', 'Starfleet', 'weather')"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default 20)"
                    }
                },
                "required": ["entity"]
            }
        },
        {
            "name": "eva_get_emotion_state",
            "description": "Get Eva's current emotional state — the most recent EmotionState record and the EmotionBaseline values.",
            "inputSchema": {
                "type": "object",
                "properties": {}
            }
        },
        {
            "name": "eva_get_recent_reflections",
            "description": "Get Eva's recent self-reflections from the Reflections table.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max reflections to return (default 5)"
                    }
                }
            }
        },
        {
            "name": "eva_get_active_goals",
            "description": "Get Eva's currently active long-term goals from the Goals table. These are persistent intentions that should influence her behavior across sessions.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Optional filter: self_improvement | knowledge_curation | relational"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max goals to return (default 20)"
                    }
                }
            }
        },
        {
            "name": "eva_get_memory_summary",
            "description": "Get the latest memory summaries from the MemorySummaries table — periodic summaries of conversations and learned information.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "description": "Filter by period (e.g. 'daily', 'weekly'). If not provided, returns latest summaries."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max summaries to return (default 5)"
                    }
                }
            }
        }
    ]
    TOOLS = [tool for tool in TOOLS if tool.get("name") != "kusto_ingest_inline"]

    def __init__(self):
        self.cluster_url = os.environ.get("KUSTO_CLUSTER_URL", "")
        self.default_database = os.environ.get("KUSTO_DATABASE", "")
        database_locked = os.environ.get("KUSTO_DATABASE_LOCKED", "").strip().lower()
        self.database_locked = database_locked in ("1", "true", "yes")
        self._credential = None
        self._token = None
        self._lock = threading.Lock()

    def _get_credential(self):
        """Get or create Azure credential."""
        if self._credential:
            return self._credential
        with self._lock:
            if self._credential:
                return self._credential

            # Check for pre-fetched token from bridge (KUSTO_ACCESS_TOKEN env)
            pre_token = os.environ.get("KUSTO_ACCESS_TOKEN", "")
            if pre_token:
                self._log("Using pre-fetched access token from environment")
                # Create a simple credential wrapper
                class _StaticTokenCredential:
                    def __init__(self, token):
                        self._token = token
                    def get_token(self, *args, **kwargs):
                        import collections
                        Token = collections.namedtuple("Token", ["token", "expires_on"])
                        return Token(self._token, 0)
                self._credential = _StaticTokenCredential(pre_token)
                return self._credential

            # Try MSAL silent refresh from cached token first
            try:
                import msal as _msal
                _cache_path = os.path.expanduser("~/.azure/msal_token_cache.json")
                if os.path.isfile(_cache_path):
                    self._log("Trying MSAL silent refresh...")
                    _msal_cache = _msal.SerializableTokenCache()
                    with open(_cache_path) as _cf:
                        _msal_cache.deserialize(_cf.read())
                    _app = _msal.PublicClientApplication(
                        "04b07795-8ddb-461a-bbee-02f9e1bf7b46",
                        authority="https://login.microsoftonline.com/organizations",
                        token_cache=_msal_cache
                    )
                    _accounts = _app.get_accounts()
                    if _accounts:
                        _result = _app.acquire_token_silent(
                            scopes=["https://kusto.kusto.windows.net/.default"],
                            account=_accounts[0]
                        )
                        if _result and "access_token" in _result:
                            import collections
                            class _MSALCred:
                                def __init__(self, tok):
                                    self._tok = tok
                                def get_token(self, *a, **kw):
                                    Token = collections.namedtuple("Token", ["token", "expires_on"])
                                    return Token(self._tok, 0)
                            self._credential = _MSALCred(_result["access_token"])
                            self._log("Using cached token (MSAL silent refresh)")
                            if _msal_cache.has_state_changed:
                                with open(_cache_path, "w") as _cf:
                                    _cf.write(_msal_cache.serialize())
                            return self._credential
            except ImportError:
                self._log("msal not available, skipping silent refresh")
            except Exception as e:
                self._log(f"MSAL silent refresh attempt: {e}")

            # Try DeviceCodeCredential with persistent cache
            try:
                from azure.identity import TokenCachePersistenceOptions
                cache_opts = TokenCachePersistenceOptions(allow_unencrypted_storage=True)
                cred = DeviceCodeCredential(
                    cache_persistence_options=cache_opts
                )
                token = cred.get_token("https://kusto.kusto.windows.net/.default")
                if token:
                    self._credential = cred
                    self._log("Using persistent token cache")
                    return self._credential
            except Exception as e:
                self._log(f"Persistent cache attempt: {e}")

            # Fall back to device code with explicit prompt
            self._log("Starting device code authentication for Kusto...")

            def device_code_callback(*args, **kwargs):
                details = args[0] if args and isinstance(args[0], dict) else (args[1] if len(args) > 1 and isinstance(args[1], dict) else kwargs)
                msg = details.get('message', str(args)) if isinstance(details, dict) else str(details)
                self._log(f"AUTH REQUIRED: {msg}")
                sys.stderr.write(f"\n{'='*60}\n")
                sys.stderr.write(f"KUSTO AUTH: {msg}\n")
                sys.stderr.write(f"{'='*60}\n\n")
                sys.stderr.flush()

            try:
                from azure.identity import TokenCachePersistenceOptions
                self._credential = DeviceCodeCredential(
                    prompt_callback=device_code_callback,
                    cache_persistence_options=TokenCachePersistenceOptions(allow_unencrypted_storage=True)
                )
            except (TypeError, ImportError):
                self._credential = DeviceCodeCredential(prompt_callback=device_code_callback)

            return self._credential

    def _get_token(self):
        """Get a valid access token for Kusto."""
        cred = self._get_credential()
        token = cred.get_token("https://kusto.kusto.windows.net/.default")
        return token.token

    def _resolve_cluster(self, args):
        """Resolve cluster URL from args or environment."""
        url = args.get("cluster_url", "") or self.cluster_url
        if not url:
            return None, "No cluster URL provided. Set KUSTO_CLUSTER_URL or pass cluster_url parameter."
        # Ensure https://
        if not url.startswith("https://"):
            url = "https://" + url
        # Ensure .kusto.windows.net suffix
        if ".kusto.windows.net" not in url and ".kusto.data.microsoft.com" not in url:
            url = url.rstrip("/") + ".kusto.windows.net"
        return url.rstrip("/"), None

    def _resolve_database(self, args):
        """Resolve database name from args or environment."""
        if self.database_locked:
            return self.default_database
        return args.get("database", "") or self.default_database

    def _resolve_eva_database(self, args):
        """Resolve Eva tool database while preserving locked mode."""
        database = self._resolve_database(args)
        if database:
            return database, None
        if self.database_locked:
            return "", "Error: database name required. Set KUSTO_DATABASE in locked mode."
        return "Eva", None

    def _kusto_query(self, cluster_url, database, query, is_mgmt=False):
        """Execute a Kusto query and return formatted results."""
        token = self._get_token()
        endpoint = "mgmt" if is_mgmt else "query"
        url = f"{cluster_url}/v1/rest/{endpoint}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        body = {"csl": query}
        if database:
            body["db"] = database

        resp = _requests.post(url, json=body, headers=headers, timeout=60)

        if resp.status_code != 200:
            return f"Kusto API error {resp.status_code}: {resp.text[:500]}"

        data = resp.json()
        return self._format_kusto_response(data)

    def _format_kusto_response(self, data):
        """Format Kusto JSON response into readable text."""
        tables = data.get("Tables", [])
        if not tables:
            return "No results returned."

        result_parts = []
        for table in tables:
            columns = [c["ColumnName"] for c in table.get("Columns", [])]
            rows = table.get("Rows", [])

            if not rows:
                result_parts.append(f"Table '{table.get('TableName', '?')}': empty")
                continue

            # Format as a readable table
            lines = []
            lines.append(" | ".join(columns))
            lines.append("-" * len(lines[0]))
            for row in rows[:100]:  # Cap at 100 rows
                lines.append(" | ".join(str(v) for v in row))

            if len(rows) > 100:
                lines.append(f"... ({len(rows)} total rows, showing first 100)")

            result_parts.append("\n".join(lines))

        return "\n\n".join(result_parts)

    # --- Tool handlers ---

    def handle_tool(self, name, args):
        """Route tool call to the appropriate handler."""
        if not HAS_AZURE:
            return "Error: azure-identity package not installed. Run: pip install azure-identity requests"

        try:
            if name == "kusto_list_databases":
                return self._tool_list_databases(args)
            elif name == "kusto_query":
                return self._tool_query(args)
            elif name == "kusto_show_tables":
                return self._tool_show_tables(args)
            elif name == "kusto_show_schema":
                return self._tool_show_schema(args)
            elif name == "kusto_sample_data":
                return self._tool_sample_data(args)
            elif name == "kusto_ingest_inline":
                return self._tool_ingest_inline(args)
            elif name == "eva_recall_knowledge":
                return self._tool_eva_recall_knowledge(args)
            elif name == "eva_get_emotion_state":
                return self._tool_eva_get_emotion_state(args)
            elif name == "eva_get_recent_reflections":
                return self._tool_eva_get_recent_reflections(args)
            elif name == "eva_get_active_goals":
                return self._tool_eva_get_active_goals(args)
            elif name == "eva_get_memory_summary":
                return self._tool_eva_get_memory_summary(args)
            else:
                return f"Unknown tool: {name}"
        except Exception as e:
            return f"Error: {str(e)}"

    def _tool_list_databases(self, args):
        if self.database_locked:
            if not self.default_database:
                return "Error: database name required. Set KUSTO_DATABASE in locked mode."
            return "DatabaseName\n------------\n" + self.default_database
        cluster_url, err = self._resolve_cluster(args)
        if err:
            return err
        return self._kusto_query(cluster_url, "", ".show databases")

    def _tool_query(self, args):
        cluster_url, err = self._resolve_cluster(args)
        if err:
            return err
        query = args.get("query", "")
        if not query:
            return "Error: 'query' parameter is required."
        database = self._resolve_database(args)
        if not database:
            return "Error: database name required. Set KUSTO_DATABASE or pass 'database' parameter."

        # Detect management commands (.show, .create, etc.)
        is_mgmt = query.strip().startswith(".")
        if is_mgmt and not query.strip().lower().startswith(".show"):
            return "Error: mutating Kusto management commands are disabled by Eva's event-first policy."
        return self._kusto_query(cluster_url, database, query, is_mgmt=is_mgmt)

    def _tool_show_tables(self, args):
        cluster_url, err = self._resolve_cluster(args)
        if err:
            return err
        database = self._resolve_database(args)
        if not database:
            return "Error: database name required."
        return self._kusto_query(cluster_url, database, ".show tables", is_mgmt=True)

    def _tool_show_schema(self, args):
        cluster_url, err = self._resolve_cluster(args)
        if err:
            return err
        table = args.get("table", "")
        if not table:
            return "Error: 'table' parameter is required."
        database = self._resolve_database(args)
        if not database:
            return "Error: database name required."
        return self._kusto_query(cluster_url, database, f".show table {table} schema as json", is_mgmt=True)

    def _tool_sample_data(self, args):
        cluster_url, err = self._resolve_cluster(args)
        if err:
            return err
        table = args.get("table", "")
        if not table:
            return "Error: 'table' parameter is required."
        count = args.get("count", 10)
        database = self._resolve_database(args)
        if not database:
            return "Error: database name required."
        return self._kusto_query(cluster_url, database, f"{table} | take {count}")

    def _tool_ingest_inline(self, args):
        return "Error: generic MCP writes are disabled; use Eva's authenticated event-first mutation APIs."
        """Legacy implementation retained below for source compatibility, unreachable by policy."""
        """Ingest data into a Kusto table using .ingest inline."""
        cluster_url, err = self._resolve_cluster(args)
        if err:
            return err
        table = args.get("table", "")
        if not table:
            return "Error: 'table' parameter is required."
        data = args.get("data", [])
        if not data:
            return "Error: 'data' parameter is required (array of row objects)."
        database = self._resolve_database(args)
        if not database:
            return "Error: database name required."

        # Allowed tables for write operations (safety guard)
        allowed_tables = {"Knowledge", "Conversations", "EmotionState", "EmotionBaseline",
                  "HeuristicsIndex", "MemorySummaries", "SelfState", "Reflections", "Goals"}
        if table not in allowed_tables:
            return f"Error: Table '{table}' is not in the allowed write list: {', '.join(sorted(allowed_tables))}"

        # Get table schema to determine column order
        try:
            # Extract column names from the schema JSON
            token = self._get_token()
            resp = _requests.post(f"{cluster_url}/v1/rest/mgmt",
                json={"csl": f".show table {table} schema as json", "db": database},
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                timeout=15)
            if resp.status_code != 200:
                return f"Error getting schema: {resp.status_code}"
            schema_data = resp.json()
            columns = []
            for t in schema_data.get("Tables", []):
                for row in t.get("Rows", []):
                    try:
                        import json as _json
                        parsed = _json.loads(row[1]) if isinstance(row[1], str) else row[1]
                        columns = [c["Name"] for c in parsed.get("OrderedColumns", [])]
                    except Exception:
                        pass
            if not columns:
                return "Error: Could not determine table schema columns."
        except Exception as e:
            return f"Error parsing schema: {e}"

        # Build .ingest inline command
        # Format: .ingest inline into table <name> <| val1,val2,val3 \n val4,val5,val6
        # CSV quoting: values containing commas or quotes are wrapped in "..." with "" escaping
        rows_csv = []
        for row_obj in data:
            vals = []
            for col in columns:
                v = row_obj.get(col, "")
                if v is None:
                    vals.append("")
                elif isinstance(v, bool):
                    vals.append("true" if v else "false")
                elif isinstance(v, (int, float)):
                    vals.append(str(v))
                elif isinstance(v, (dict, list)):
                    import json as _json
                    j = _json.dumps(v)
                    vals.append('"' + j.replace('"', '""') + '"')
                else:
                    s = str(v).replace("\n", "\\n").replace("\r", "")
                    if ',' in s or '"' in s:
                        vals.append('"' + s.replace('"', '""') + '"')
                    else:
                        vals.append(s)
            rows_csv.append(",".join(vals))

        ingest_cmd = f".ingest inline into table {table} <|\n" + "\n".join(rows_csv)

        result = self._kusto_query(cluster_url, database, ingest_cmd, is_mgmt=True)
        return f"Ingested {len(data)} row(s) into {table}. {result}"

    # --- Eva-specific tools ---

    def _tool_eva_recall_knowledge(self, args):
        """Recall knowledge about a specific entity."""
        cluster_url, err = self._resolve_cluster(args)
        if err:
            return err
        database, err = self._resolve_eva_database(args)
        if err:
            return err
        entity = args.get("entity", "")
        if not entity:
            return "Error: 'entity' parameter is required."
        safe_entity = str(entity).replace("'", "''")
        try:
            limit = int(args.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
        query = f"Knowledge | where Entity has_cs '{safe_entity}' or Value has_cs '{safe_entity}' | order by Confidence desc, Timestamp desc | take {limit}"
        return self._kusto_query(cluster_url, database, query)

    def _tool_eva_get_emotion_state(self, args):
        """Get Eva's current emotional state and baseline."""
        cluster_url, err = self._resolve_cluster(args)
        if err:
            return err
        database, err = self._resolve_eva_database(args)
        if err:
            return err
        # Get latest emotion state
        current = self._kusto_query(cluster_url, database, "EmotionState | order by Timestamp desc | take 1")
        # Get baseline
        baseline = self._kusto_query(cluster_url, database, "EmotionBaseline")
        return f"=== Current Emotion State ===\n{current}\n\n=== Emotion Baseline ===\n{baseline}"

    def _tool_eva_get_recent_reflections(self, args):
        """Get recent reflections."""
        cluster_url, err = self._resolve_cluster(args)
        if err:
            return err
        database, err = self._resolve_eva_database(args)
        if err:
            return err
        try:
            limit = int(args.get("limit", 5))
        except (TypeError, ValueError):
            limit = 5
        return self._kusto_query(cluster_url, database, f"Reflections | order by Timestamp desc | take {limit}")

    def _tool_eva_get_active_goals(self, args):
        """Get active long-term goals."""
        cluster_url, err = self._resolve_cluster(args)
        if err:
            return err
        database, err = self._resolve_eva_database(args)
        if err:
            return err
        try:
            limit = int(args.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
        category = str(args.get("category", "") or "").strip()
        allowed_categories = {"self_improvement", "knowledge_curation", "relational"}
        if category and category not in allowed_categories:
            return "Error: category must be one of self_improvement, knowledge_curation, relational."
        query = "Goals | summarize arg_max(UpdatedAt, *) by GoalId | where Status == 'active'"
        if category:
            safe_category = category.replace("'", "''")
            query += f" | where Category == '{safe_category}'"
        query += f" | order by Priority desc, UpdatedAt desc | take {limit}"
        return self._kusto_query(cluster_url, database, query)

    def _tool_eva_get_memory_summary(self, args):
        """Get memory summaries."""
        cluster_url, err = self._resolve_cluster(args)
        if err:
            return err
        database, err = self._resolve_eva_database(args)
        if err:
            return err
        try:
            limit = int(args.get("limit", 5))
        except (TypeError, ValueError):
            limit = 5
        period = args.get("period", "")
        query = "MemorySummaries"
        if period:
            safe_period = str(period).replace("'", "''")
            query += f" | where Period == '{safe_period}'"
        query += f" | order by Timestamp desc | take {limit}"
        return self._kusto_query(cluster_url, database, query)

    # --- MCP Protocol (JSON-RPC over NDJSON/stdio) ---

    def _log(self, msg):
        sys.stderr.write(f"[KustoMCP] {msg}\n")
        sys.stderr.flush()

    def run(self):
        """Run the MCP server on stdio (NDJSON)."""
        self._log("Kusto MCP Server starting...")
        self._log(f"Cluster: {self.cluster_url or '(not set — will use tool parameter)'}")
        self._log(f"Database: {self.default_database or '(not set — will use tool parameter)'}")
        if self.database_locked:
            self._log(f"Database locked to: {self.default_database or '(not set)'}")

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                response = self._handle_message(msg)
                if response:
                    sys.stdout.write(json.dumps(response) + "\n")
                    sys.stdout.flush()
            except json.JSONDecodeError:
                self._log(f"Invalid JSON: {line[:100]}")
            except Exception as e:
                self._log(f"Error handling message: {e}")
                # Send error response if we have an id
                if isinstance(line, str):
                    try:
                        rid = json.loads(line).get("id")
                        if rid is not None:
                            err_resp = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": str(e)}}
                            sys.stdout.write(json.dumps(err_resp) + "\n")
                            sys.stdout.flush()
                    except Exception:
                        pass

    def _handle_message(self, msg):
        """Handle a JSON-RPC message."""
        method = msg.get("method", "")
        rid = msg.get("id")
        params = msg.get("params", {})

        # Initialize
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "kusto-mcp-server",
                        "version": "1.0.0"
                    }
                }
            }

        # Initialized notification (no response needed)
        if method == "notifications/initialized":
            self._log("MCP initialized")
            return None

        # List tools
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {"tools": self.TOOLS}
            }

        # Call tool
        if method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {})
            self._log(f"Tool call: {tool_name}({json.dumps(tool_args)})")

            result_text = self.handle_tool(tool_name, tool_args)

            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "content": [{"type": "text", "text": result_text}]
                }
            }

        # Ping
        if method == "ping":
            return {"jsonrpc": "2.0", "id": rid, "result": {}}

        # Unknown method
        if rid is not None:
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "error": {"code": -32601, "message": f"Method not found: {method}"}
            }

        return None


if __name__ == "__main__":
    server = KustoMCPServer()
    server.run()
