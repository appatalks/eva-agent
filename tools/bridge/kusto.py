"""Bridge domain: kusto."""

import json
import os
import re
import sys
import threading
import time
import urllib.parse
from bridge import config as _cfg
from bridge import state as _st


def _refresh_kusto_token():
    """Try to refresh the cached Kusto token using the stored credential. Returns True if refreshed."""
    # global statement removed — writes go to _st.*
    if _st.egress_mode != "cloud":
        return False
    if not _st.kusto_credential:
        return False
    try:
        prior = _st.kusto_token_cache
        token = _st.kusto_credential.get_token("https://kusto.kusto.windows.net/.default")
        _st.kusto_token_cache = token.token
        _st.kusto_table_columns_cache = {}
        refresh_state = "updated" if token.token != prior else "unchanged"
        print(f"[Bridge] Kusto token refreshed ({refresh_state}, length: {len(token.token)})")
        return True
    except Exception as e:
        print(f"[Bridge] Token refresh failed: {e}")
        return False


def _inject_kusto_token(mcp_config):
    """Inject cached Kusto token into MCP config if kusto-mcp-server is present."""
    # global statement removed — writes go to _st.*
    if not mcp_config or "kusto-mcp-server" not in mcp_config:
        return mcp_config

    _refresh_kusto_token()

    if _st.kusto_token_cache:
        if "env" not in mcp_config["kusto-mcp-server"]:
            mcp_config["kusto-mcp-server"]["env"] = {}
        mcp_config["kusto-mcp-server"]["env"]["KUSTO_ACCESS_TOKEN"] = _st.kusto_token_cache

    return mcp_config


def _ensure_kusto_token():
    """Ensure the bridge has a Kusto token for direct bridge-side Kusto calls."""
    # global statement removed — writes go to _st.*
    if _st.egress_mode != "cloud":
        return False, f"Kusto disabled by EVA_EGRESS_MODE={_st.egress_mode}"
    if _st.kusto_token_cache:
        return True, ""
    if _refresh_kusto_token():
        return True, ""
    # Try MSAL silent refresh before falling through to device code
    if _try_kusto_silent_auth():
        return True, ""
    try:
        from azure.identity import DeviceCodeCredential, TokenCachePersistenceOptions
        cache_opts = TokenCachePersistenceOptions(allow_unencrypted_storage=True)
        credential = DeviceCodeCredential(cache_persistence_options=cache_opts)
        token = credential.get_token("https://kusto.kusto.windows.net/.default")
        if token and getattr(token, "token", None):
            _st.kusto_token_cache = token.token
            _st.kusto_credential = credential
            print(f"[Bridge] Kusto token obtained for direct query calls (length: {len(token.token)})")
            return True, ""
        return False, "Kusto token request returned no token"
    except Exception as error:
        return False, str(error)



def _try_kusto_silent_auth():
    """Attempt MSAL silent token refresh from cached credentials. Returns True if successful."""
    # global statement removed — writes go to _st.*
    if _st.egress_mode != "cloud":
        return False
    try:
        import msal as _msal
        _cache_path = os.path.expanduser("~/.azure/msal_token_cache.json")
        if not os.path.isfile(_cache_path):
            return False
        _msal_cache = _msal.SerializableTokenCache()
        with open(_cache_path) as _cf:
            _msal_cache.deserialize(_cf.read())
        _app = _msal.PublicClientApplication(
            "04b07795-8ddb-461a-bbee-02f9e1bf7b46",
            authority="https://login.microsoftonline.com/organizations",
            token_cache=_msal_cache
        )
        _accounts = _app.get_accounts()
        if not _accounts:
            return False
        msal_cred = _MSALSilentCredential(
            app=_app,
            account=_accounts[0],
            token_cache=_msal_cache,
            cache_path=_cache_path,
            default_scopes=["https://kusto.kusto.windows.net/.default"],
        )
        token = msal_cred.get_token("https://kusto.kusto.windows.net/.default")
        if token and getattr(token, "token", None):
            _st.kusto_token_cache = token.token
            _st.kusto_credential = msal_cred
            print(f"[Bridge] Kusto token refreshed silently from MSAL cache (length: {len(token.token)})")
            return True
        return False
    except ImportError:
        return False
    except Exception as e:
        print(f"[Bridge] MSAL silent auth failed: {e}")
        return False


def _split_kusto_seed_blocks(seed_text):
    """Split seed KQL into executable management command blocks."""
    import re
    blocks = []
    for raw_block in re.split(r"\n\s*\n", seed_text):
        lines = []
        for line in raw_block.splitlines():
            if line.strip().startswith("//"):
                continue
            lines.append(line)
        block = "\n".join(lines).strip()
        if block:
            blocks.append(block)
    return blocks



def _is_kusto_schema_block(block):
    """True when a seed block defines a table rather than ingesting rows.

    Used by the schema-only seed path so existing databases can be backfilled
    with any missing tables without re-ingesting (and duplicating) seed rows.
    """
    first_line = ""
    for line in (block or "").splitlines():
        stripped = line.strip()
        if stripped:
            first_line = stripped.lower()
            break
    return first_line.startswith(".create")



def _normalize_kusto_cluster_url(cluster_url):
    """Normalize a Kusto cluster URL for policy comparisons."""
    return str(cluster_url or "").strip().rstrip("/").lower()



def _same_kusto_cluster(left, right):
    return _normalize_kusto_cluster_url(left) == _normalize_kusto_cluster_url(right)


# ---------------------------------------------------------------------------
# HTTP Server — exposes the ACP client as an OpenAI-compatible endpoint
# ---------------------------------------------------------------------------

_st.acp_client = _st.acp_client  # alias; mutable state lives in bridge.state
# Warm client pool: keep one live Copilot CLI per model so switching between the
# cognition draft model and the reviewer model does not tear down and respawn the
# CLI on every turn. Keyed by model name; bounded by _ACP_POOL_MAX (LRU eviction).
_st.acp_pool = _st.acp_pool
_st.acp_pool_order = _st.acp_pool_order
_st.acp_pool_lock = _st.acp_pool_lock
_ACP_POOL_MAX = _cfg.ACP_POOL_MAX
# _kusto_token_cache -> _st.kusto_token_cache
# _kusto_credential -> _st.kusto_credential
# _last_interaction_date -> _st.last_interaction_date
# _cognition_enabled -> _st.cognition_enabled
# _session_exchange_count -> _st.session_exchange_count
# _session_conversation_buffer -> _st.session_conversation_buffer
# _cognition_launch_iso -> _st.cognition_launch_iso
# _cognition_launch_id -> _st.cognition_launch_id
_st.cognition_candidate_counts = _st.cognition_candidate_counts
_st.candidate_history_cache = _st.candidate_history_cache
_CANDIDATE_HISTORY_TTL_SECONDS = _cfg.CANDIDATE_HISTORY_TTL_SECONDS
_CONVO_CONTENT_CAP = _cfg.CONVO_CONTENT_CAP
_ARTIFACTS_DIR = _cfg.ARTIFACTS_DIR
_KUSTO_CLUSTER_CACHE_PATH = _cfg.KUSTO_CLUSTER_CACHE_PATH
_MCP_CONFIG_CACHE_PATH = _cfg.MCP_CONFIG_CACHE_PATH
_ALERTS_CONFIG_PATH = _cfg.ALERTS_CONFIG_PATH
_NOTIFY_PATH = _cfg.NOTIFY_PATH
# _kusto_table_columns_cache -> _st.kusto_table_columns_cache
_kusto_database_locked = _st.kusto_database_locked
# _active_kusto_db -> _st.active_kusto_db
# _active_kusto_cluster -> _st.active_kusto_cluster
# _bridge_bind_address -> _st.bridge_bind_address
_LMSTUDIO_ALLOWED_PORTS = _cfg.LMSTUDIO_ALLOWED_PORTS
_HTTP_CONTENT_TYPE_RE = _cfg.HTTP_CONTENT_TYPE_RE

# ── Semantic memory (embeddings) ───────────────────────────────────────
# Recall ranks stored facts by semantic similarity to the user's message.
# Embeddings are computed on demand via the OpenAI embeddings API and cached
# on disk keyed by text hash, so the Knowledge table needs no schema change and
# facts written by any path (regex backstop or the LLM ingest tool) are covered.
# _openai_api_key_cache -> _st.openai_api_key_cache
_EMBEDDING_MODEL = _cfg.EMBEDDING_MODEL
_EMBEDDING_CACHE_PATH = _cfg.EMBEDDING_CACHE_PATH
# _embedding_cache -> _st.embedding_cache
_st.embedding_cache_lock = _st.embedding_cache_lock
# _embedding_disabled_logged -> _st.embedding_disabled_logged
_SEMANTIC_MIN_SCORE = _cfg.SEMANTIC_MIN_SCORE
_SEMANTIC_POOL_SIZE = _cfg.SEMANTIC_POOL_SIZE

# ── Memory backend selection ───────────────────────────────────────────────
# "kusto" = Azure Data Explorer (default, existing behavior)
# "sqlite" = local SQLite file via tools/sqlite_memory.py
# _memory_backend -> _st.memory_backend
# _sqlite_mem -> _st.sqlite_mem
_MEMORY_BACKEND_PREF_PATH = _cfg.MEMORY_BACKEND_PREF_PATH


class _MSALSilentCredential:
    """Credential wrapper that refreshes tokens from MSAL cache without interactive prompts."""

    def __init__(self, app, account, token_cache, cache_path, default_scopes):
        self._app = app
        self._account = account
        self._cache = token_cache
        self._cache_path = cache_path
        self._default_scopes = list(default_scopes)

    def _persist_cache(self):
        if self._cache.has_state_changed:
            with open(self._cache_path, "w") as cache_file:
                cache_file.write(self._cache.serialize())

    def get_token(self, *scopes):
        active_scopes = list(scopes) if scopes else list(self._default_scopes)
        result = self._app.acquire_token_silent(active_scopes, account=self._account)
        if not result or "access_token" not in result:
            result = self._app.acquire_token_silent(active_scopes, account=self._account, force_refresh=True)
        if not result or "access_token" not in result:
            details = "no access_token returned"
            if isinstance(result, dict):
                details = result.get("error_description") or result.get("error") or details
            raise RuntimeError(f"MSAL silent token refresh failed: {details}")

        self._persist_cache()
        token_value = result["access_token"]
        expires_on = int(result.get("expires_on", 0) or 0)
        return type("Token", (), {"token": token_value, "expires_on": expires_on})()


# ---------------------------------------------------------------------------
# Cognition Layer — memory injection, reflection, day lifecycle
# ---------------------------------------------------------------------------


def _kusto_query_direct(cluster_url, database, query, is_mgmt=False):
    """Execute a Kusto query directly (bypasses MCP). Returns text result or None on error."""
    # global statement removed — writes go to _st.*
    if _st.egress_mode != "cloud" or not _st.kusto_token_cache:
        return None
    import requests as _requests_mod
    endpoint = "mgmt" if is_mgmt else "query"
    url = f"{cluster_url}/v1/rest/{endpoint}"
    headers = {"Authorization": f"Bearer {_st.kusto_token_cache}", "Content-Type": "application/json"}
    payload = {"csl": query, "db": database}

    # Retry up to 3 times with fresh sessions for transient SSL errors
    for attempt in range(3):
        try:
            session = _requests_mod.Session()
            resp = session.post(url, json=payload, headers=headers, timeout=15)
            session.close()
            if resp.status_code == 200:
                data = resp.json()
                tables = data.get("Tables", [])
                if tables:
                    rows = tables[0].get("Rows", [])
                    cols = [c["ColumnName"] for c in tables[0].get("Columns", [])]
                    if rows:
                        return [dict(zip(cols, row)) for row in rows]
                return []
            elif resp.status_code == 401 and attempt == 0 and _refresh_kusto_token():
                print("[Cognition] Kusto query got 401, retrying with refreshed token")
                headers["Authorization"] = f"Bearer {_st.kusto_token_cache}"
                continue
            elif resp.status_code == 401:
                print("[Cognition] Kusto query still unauthorized after refresh; verify tenant/account RBAC for cluster/database")
            else:
                err_text = resp.text[:200].replace("\n", " ").strip()
                query_preview = query[:120].replace("\n", " ")
                print(f"[Cognition] Kusto query HTTP {resp.status_code}: {err_text}")
                print(f"[Cognition] Failed query: {query_preview}")
            return None
        except (_requests_mod.exceptions.SSLError, _requests_mod.exceptions.ConnectionError) as e:
            if attempt < 2:
                print(f"[Cognition] Kusto SSL retry {attempt+1}/3: {e}")
                time.sleep(1)
            else:
                print(f"[Cognition] Kusto query failed after 3 retries: {e}")
                return None
        except Exception as e:
            print(f"[Cognition] Kusto query error: {e}")
            return None



def _short_kusto_error(value):
    if isinstance(value, (dict, list)):
        text = json.dumps(value)
    else:
        text = str(value or "")
    return text[:300]



def _kusto_query_with_error(cluster_url, database, query, is_mgmt=False):
    """Execute a Kusto query and return (rows, error_text) for seed diagnostics."""
    # global statement removed — writes go to _st.*
    if _st.egress_mode != "cloud":
        return None, f"Kusto disabled by EVA_EGRESS_MODE={_st.egress_mode}"
    if not _st.kusto_token_cache:
        return None, "Kusto token is not available"
    import requests as _requests_mod
    endpoint = "mgmt" if is_mgmt else "query"
    url = f"{cluster_url}/v1/rest/{endpoint}"
    headers = {"Authorization": f"Bearer {_st.kusto_token_cache}", "Content-Type": "application/json"}
    payload = {"csl": query, "db": database}

    for attempt in range(3):
        try:
            session = _requests_mod.Session()
            resp = session.post(url, json=payload, headers=headers, timeout=15)
            session.close()
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError as error:
                    return None, f"Kusto returned invalid JSON: {_short_kusto_error(error)}"
                exceptions = data.get("Exceptions", [])
                if exceptions:
                    return None, _short_kusto_error(exceptions[0])
                one_api = data.get("OneApiErrors", [])
                if one_api:
                    return None, _short_kusto_error(one_api[0])
                tables = data.get("Tables", [])
                if tables:
                    rows = tables[0].get("Rows", [])
                    cols = [c["ColumnName"] for c in tables[0].get("Columns", [])]
                    if rows:
                        return [dict(zip(cols, row)) for row in rows], ""
                return [], ""
            if resp.status_code == 401 and attempt == 0 and _refresh_kusto_token():
                headers["Authorization"] = f"Bearer {_st.kusto_token_cache}"
                continue
            error_text = resp.text[:300] if resp.text else "empty response"
            return None, f"Kusto API error {resp.status_code}: {error_text}"
        except (_requests_mod.exceptions.SSLError, _requests_mod.exceptions.ConnectionError) as error:
            if attempt < 2:
                time.sleep(1)
                continue
            return None, f"Kusto connection error: {_short_kusto_error(error)}"
        except Exception as error:
            return None, f"Kusto query error: {_short_kusto_error(error)}"



def _get_table_columns(cluster_url, database, table):
    """Return known table columns from Kusto schema, cached per cluster/db/table.
    Returns list of column names, or None if the table does not exist.
    Negative results (table not found) are cached to avoid repeated queries."""
    key = (cluster_url, database, table)
    cached = _st.kusto_table_columns_cache.get(key)
    if cached is not None:
        # Empty list means table confirmed non-existent
        return cached if cached else None

    schema_rows = _kusto_query_direct(
        cluster_url,
        database,
        f".show table {table} cslschema",
        is_mgmt=True,
    )
    if not schema_rows:
        # Cache negative result so we don't re-query on every call
        _st.kusto_table_columns_cache[key] = []
        return None

    # .show table X cslschema returns a single row with a Schema column containing
    # comma-separated "name:type" pairs. Parse the column names from it.
    schema_str = schema_rows[0].get("Schema", "") if schema_rows else ""
    if not schema_str:
        # Fallback: try extracting ColumnName from each row (older Kusto versions)
        cols = [str(r.get("ColumnName", "")).strip() for r in schema_rows if r.get("ColumnName")]
    else:
        cols = [pair.split(":")[0].strip() for pair in schema_str.split(",") if ":" in pair]
    if not cols:
        _st.kusto_table_columns_cache[key] = []
        return None

    _st.kusto_table_columns_cache[key] = cols
    return cols


def _canonical_kusto_ingest_string(value):
    """Canonical text representation used by Kusto inline CSV ingest."""
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")


def _kusto_ingest_direct(cluster_url, database, table, columns, rows_data):
    """Ingest data directly into Kusto via .ingest inline."""
    # global statement removed — writes go to _st.*
    if _st.egress_mode != "cloud" or not _st.kusto_token_cache:
        return False

    table_columns = _get_table_columns(cluster_url, database, table)
    if table_columns:
        # Preserve table schema order for positional CSV ingest.
        resolved_columns = [c for c in table_columns if c in columns]
        dropped = [c for c in columns if c not in table_columns]
        if dropped:
            print(f"[Cognition] Ingest {table}: dropping unknown columns for current schema: {', '.join(dropped)}")
        if not resolved_columns:
            print(f"[Cognition] Ingest {table}: no matching columns found in table schema")
            return False
    else:
        resolved_columns = list(columns)

    import requests as _requests_mod
    rows_csv = []
    for row_obj in rows_data:
        vals = []
        for col in resolved_columns:
            v = row_obj.get(col, "")
            if v is None:
                vals.append("")
            elif isinstance(v, bool):
                vals.append("true" if v else "false")
            elif isinstance(v, (int, float)):
                vals.append(str(v))
            elif isinstance(v, (dict, list)):
                # Dynamic column: serialize to JSON, then CSV-quote with "" escaping
                j = json.dumps(v)
                vals.append('"' + j.replace('"', '""') + '"')
            else:
                s = _canonical_kusto_ingest_string(v)
                # CSV-quote any string containing commas or quotes
                if ',' in s or '"' in s:
                    vals.append('"' + s.replace('"', '""') + '"')
                else:
                    vals.append(s)
        rows_csv.append(",".join(vals))

    cmd = f".ingest inline into table {table} <|\n" + "\n".join(rows_csv)
    if rows_csv:
        print(f"[Cognition] Ingest {table}: {len(rows_csv)} rows ({len(resolved_columns)} cols)")
    url = f"{cluster_url}/v1/rest/mgmt"
    headers = {"Authorization": f"Bearer {_st.kusto_token_cache}", "Content-Type": "application/json"}

    for attempt in range(3):
        try:
            session = _requests_mod.Session()
            resp = session.post(url, json={"csl": cmd, "db": database}, headers=headers, timeout=15)
            session.close()
            if resp.status_code == 200:
                # Check for errors in the response body (Kusto returns 200 even on ingest parse errors)
                try:
                    body = resp.json()
                    exceptions = body.get("Exceptions", [])
                    if exceptions:
                        print(f"[Cognition] Kusto ingest error in response: {exceptions[0][:200]}")
                        return False
                    # Also check OneApiErrors
                    one_api = body.get("OneApiErrors", [])
                    if one_api:
                        print(f"[Cognition] Kusto ingest OneApiError: {one_api[0]}")
                        return False
                except Exception:
                    pass
                return True
            elif resp.status_code == 401 and attempt == 0 and _refresh_kusto_token():
                print("[Cognition] Kusto ingest got 401, retrying with refreshed token")
                headers["Authorization"] = f"Bearer {_st.kusto_token_cache}"
                continue
            elif resp.status_code == 401:
                print("[Cognition] Kusto ingest still unauthorized after refresh; verify tenant/account RBAC for cluster/database")
            else:
                print(f"[Cognition] Kusto ingest failed ({resp.status_code}): {resp.text[:500]}")
                return False
        except (_requests_mod.exceptions.SSLError, _requests_mod.exceptions.ConnectionError) as e:
            if attempt < 2:
                print(f"[Cognition] Kusto ingest SSL retry {attempt+1}/3: {e}")
                time.sleep(1)
            else:
                print(f"[Cognition] Kusto ingest failed after 3 retries: {e}")
                return False
        except Exception as e:
            print(f"[Cognition] Kusto ingest error: {e}")
            return False

# ---------------------------------------------------------------------------
# Memory routing — dispatches to Kusto or SQLite based on _memory_backend
# ---------------------------------------------------------------------------


def _get_kusto_config():
    """Get Kusto cluster URL and database from the running MCP config."""
    if not _st.acp_client or not _st.acp_client.mcp_config:
        return None, None
    kusto_cfg = _st.acp_client.mcp_config.get("kusto-mcp-server", {})
    env = kusto_cfg.get("env", {})
    cluster = env.get("KUSTO_CLUSTER_URL", "") or _st.active_kusto_cluster
    if _kusto_database_locked:
        db = _get_locked_kusto_database()
    else:
        db = env.get("KUSTO_DATABASE", "") or _st.active_kusto_db
    if not db and not _kusto_database_locked:
        db = "Eva"
    return cluster, db



def _get_locked_kusto_database():
    if not _kusto_database_locked:
        return ""
    return (_st.active_kusto_db or os.environ.get("KUSTO_DATABASE", "")).strip()



def _capture_active_kusto_env(mcp_config):
    """Track the Kusto config currently posted to the bridge."""
    # global statement removed — writes go to _st.*
    kusto_cfg = (mcp_config or {}).get("kusto-mcp-server", {})
    env = kusto_cfg.get("env", {}) if isinstance(kusto_cfg, dict) else {}
    _st.active_kusto_db = str(env.get("KUSTO_DATABASE", "") or os.environ.get("KUSTO_DATABASE", "")).strip()
    _st.active_kusto_cluster = str(env.get("KUSTO_CLUSTER_URL", "") or os.environ.get("KUSTO_CLUSTER_URL", "")).strip()
    # Persist / restore cluster URL from local cache file
    if _st.active_kusto_cluster:
        _persist_kusto_cluster(_st.active_kusto_cluster)
    else:
        cached = _load_cached_kusto_cluster()
        if cached:
            _st.active_kusto_cluster = cached
            print(f"[Bridge] Kusto cluster restored from cache: {cached}")



def _persist_kusto_cluster(cluster_url):
    """Save the Kusto cluster URL to a local cache file for future startups."""
    try:
        os.makedirs(os.path.dirname(_KUSTO_CLUSTER_CACHE_PATH), exist_ok=True)
        with open(_KUSTO_CLUSTER_CACHE_PATH, "w") as f:
            f.write(cluster_url.strip())
    except OSError:
        pass



def _load_cached_kusto_cluster():
    """Load a previously cached Kusto cluster URL."""
    try:
        if os.path.isfile(_KUSTO_CLUSTER_CACHE_PATH):
            with open(_KUSTO_CLUSTER_CACHE_PATH) as f:
                url = f.read().strip()
            if url and url.startswith("https://"):
                return url
    except OSError:
        pass
    return ""


_MCP_SECRET_ENV_MARKERS = ("TOKEN", "KEY", "SECRET", "PAT", "PASSWORD", "CREDENTIAL")


