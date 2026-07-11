"""Bridge domain: memory."""

import hashlib
import json
import os
import re
import threading
from bridge import config as _cfg
from bridge import state as _st
from bridge.kusto import _kusto_query_direct, _kusto_ingest_direct, _get_kusto_config, _ensure_kusto_token

_EMBEDDING_CACHE_PATH = _cfg.EMBEDDING_CACHE_PATH
_EMBEDDING_MODEL = _cfg.EMBEDDING_MODEL
_ENTITY_IGNORE_WORDS = _cfg.ENTITY_IGNORE_WORDS
_MEMORY_BACKEND_PREF_PATH = _cfg.MEMORY_BACKEND_PREF_PATH

def _resolve_memory_backend():
    """Return the active memory backend name, checking persisted preference."""
    # global statement removed — writes go to _st.*
    if _st.memory_backend not in ("kusto", "sqlite"):
        # Check persisted preference
        try:
            if os.path.isfile(_MEMORY_BACKEND_PREF_PATH):
                with open(_MEMORY_BACKEND_PREF_PATH) as f:
                    saved = f.read().strip().lower()
                if saved in ("kusto", "sqlite"):
                    _st.memory_backend = saved
        except Exception:
            pass
    if _st.memory_backend not in ("kusto", "sqlite"):
        _st.memory_backend = "kusto"
    return _st.memory_backend


def _get_sqlite_mem():
    """Return the global SqliteMemory instance, creating it on first use."""
    # global statement removed — writes go to _st.*
    if _st.sqlite_mem is None:
        with _st.sqlite_mem_lock:
            if _st.sqlite_mem is None:
                from sqlite_memory import SqliteMemory
                db_path = os.environ.get("EVA_MEMORY_DB", os.path.expanduser("~/.eva/memory.db"))
                _st.sqlite_mem = SqliteMemory(db_path)
                print(f"[Bridge] SQLite memory initialized: {_st.sqlite_mem.db_path}")
    return _st.sqlite_mem


def _set_memory_backend(backend):
    """Switch the active memory backend and persist the choice."""
    # global statement removed — writes go to _st.*
    if backend not in ("kusto", "sqlite"):
        return False
    with _st.memory_backend_lock:
        _st.memory_backend = backend
        try:
            os.makedirs(os.path.dirname(_MEMORY_BACKEND_PREF_PATH), exist_ok=True)
            with open(_MEMORY_BACKEND_PREF_PATH, "w") as f:
                f.write(backend)
        except Exception as e:
            print(f"[Bridge] Failed to persist memory backend preference: {e}")
        print(f"[Bridge] Memory backend set to: {backend}")
        return True

# Synonyms expand a query term so lexical recall matches differently-worded facts
# (e.g. "playlist" should surface a fact stored under relation "favorite_songs").
_MEMORY_SYNONYMS = {
    "playlist": ["playlist", "playlists", "song", "songs", "music", "track", "tracks", "tunes"],
    "song": ["song", "songs", "track", "tracks", "music", "playlist"],
    "music": ["music", "song", "songs", "playlist", "tracks", "artist", "band"],
    "trip": ["trip", "travel", "vacation", "holiday", "journey"],
    "favorite": ["favorite", "favourite", "favorites", "favourites"],
    "pet": ["pet", "pets", "dog", "cat"],
    "kid": ["kid", "kids", "child", "children", "son", "daughter"],
    "job": ["job", "work", "employer", "company", "occupation", "career"],
    "home": ["home", "location", "address", "city", "based"],
    "phone": ["phone", "mobile", "cell"],
    "email": ["email"],
    "birthday": ["birthday", "birthdate", "born"],
}



def _set_openai_key_from(data):
    """Cache the OpenAI API key from a request body or environment for embeddings.
    Background threads (reflection/recall) reuse the cached value."""
    # global statement removed — writes go to _st.*
    key = ""
    if isinstance(data, dict):
        key = (data.get("openai_api_key") or "").strip()
    if not key:
        key = os.environ.get("OPENAI_API_KEY", "").strip()
    if key:
        _st.openai_api_key_cache = key
    return _st.openai_api_key_cache



def _load_embedding_cache():
    # global statement removed — writes go to _st.*
    if _st.embedding_cache is not None:
        return _st.embedding_cache
    with _st.embedding_cache_lock:
        if _st.embedding_cache is not None:
            return _st.embedding_cache
        try:
            with open(_EMBEDDING_CACHE_PATH) as f:
                loaded = json.load(f)
            _st.embedding_cache = loaded if isinstance(loaded, dict) else {}
        except Exception:
            _st.embedding_cache = {}
    return _st.embedding_cache



def _save_embedding_cache():
    cache = _st.embedding_cache
    if cache is None:
        return
    try:
        os.makedirs(os.path.dirname(_EMBEDDING_CACHE_PATH), exist_ok=True)
        tmp = _EMBEDDING_CACHE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, _EMBEDDING_CACHE_PATH)
    except Exception as e:
        print(f"[Cognition] Embedding cache save failed: {e}")



def _embed_texts(texts):
    """Return {text: vector} for the given texts using a persistent cache and a
    single batched OpenAI embeddings call for cache misses. Returns whatever is
    available (possibly empty) without raising, so recall degrades to lexical."""
    # global statement removed — writes go to _st.*
    import hashlib
    key = _st.openai_api_key_cache or os.environ.get("OPENAI_API_KEY", "").strip()

    unique = []
    seen = set()
    for t in texts:
        t = (t or "").strip()
        if t and t not in seen:
            seen.add(t)
            unique.append(t)
    if not unique:
        return {}

    cache = _load_embedding_cache()
    result = {}
    missing = []
    for t in unique:
        h = hashlib.sha1(t.encode("utf-8")).hexdigest()
        vec = cache.get(h)
        if vec is not None:
            result[t] = vec
        else:
            missing.append(t)

    if missing:
        if _st.egress_mode != "cloud":
            if not _st.embedding_disabled_logged:
                print(f"[Cognition] {_st.egress_mode} mode: public embeddings disabled; recall uses cached/lexical match only")
                _st.embedding_disabled_logged = True
            return result
        if not key:
            if not _st.embedding_disabled_logged:
                print("[Cognition] No OpenAI key for embeddings; recall uses lexical match only")
                _st.embedding_disabled_logged = True
            return result
        try:
            import requests as _req
            resp = _req.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": _EMBEDDING_MODEL, "input": missing},
                timeout=30,
            )
            if resp.status_code == 200:
                for item in resp.json().get("data", []):
                    idx = item.get("index", -1)
                    emb = item.get("embedding")
                    if emb and 0 <= idx < len(missing):
                        t = missing[idx]
                        result[t] = emb
                        cache[hashlib.sha1(t.encode("utf-8")).hexdigest()] = emb
                _save_embedding_cache()
            else:
                print(f"[Cognition] Embedding API failed ({resp.status_code}): {resp.text[:160]}")
        except Exception as e:
            print(f"[Cognition] Embedding request error: {e}")
    return result



def _cosine_similarity(a, b):
    import math
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))



def _expand_query_terms(message):
    """Tokenize a message and expand each token with memory synonyms, dropping
    stopwords. Used to build a lexical Knowledge recall filter."""
    toks = [w.lower().strip("?.,!'\"()[]") for w in (message or "").split()]
    terms = set()
    for w in toks:
        if len(w) <= 2:
            continue
        terms.add(w)
        for base, syns in _MEMORY_SYNONYMS.items():
            if w == base or w in syns:
                terms.update(syns)
    return {t for t in terms if len(t) > 2 and t not in _ENTITY_IGNORE_WORDS}



def _memory_query(query_or_table, cluster_url=None, database=None, is_mgmt=False):
    """Backend-agnostic query. For Kusto, pass cluster_url/database and a KQL query.
    For SQLite, pass a SQL query (KQL management queries return sensible defaults).
    Returns list of dicts (same format as _kusto_query_direct)."""
    backend = _resolve_memory_backend()
    if backend == "sqlite":
        mem = _get_sqlite_mem()
        q = query_or_table.strip()
        # Handle Kusto management commands that the bridge uses
        if q.startswith(".show databases"):
            return [{"DatabaseName": "local", "path": mem.db_path}]
        if q.startswith(".show tables"):
            return [{"TableName": t} for t in mem.list_tables()]
        if q.startswith(".show table") and "cslschema" in q:
            # Extract table name from ".show table X cslschema"
            parts = q.split()
            tname = parts[2] if len(parts) > 2 else ""
            schema = mem.get_schema(tname)
            if not schema:
                return []
            schema_str = ", ".join(f"{c}:{t}" for c, t in schema)
            return [{"Schema": schema_str}]
        # Regular SQL query
        return mem.query(q) or []
    else:
        if not cluster_url:
            cluster_url, database = _get_kusto_config()
        if not cluster_url:
            return []
        return _kusto_query_direct(cluster_url, database, query_or_table, is_mgmt=is_mgmt) or []



def _memory_ingest(table, columns, rows_data, cluster_url=None, database=None):
    """Backend-agnostic ingest. Same signature as _kusto_ingest_direct."""
    backend = _resolve_memory_backend()
    if backend == "sqlite":
        mem = _get_sqlite_mem()
        return mem.ingest(table, columns, rows_data)
    else:
        if not cluster_url:
            cluster_url, database = _get_kusto_config()
        if not cluster_url:
            return False
        return _kusto_ingest_direct(cluster_url, database, table, columns, rows_data)



def _memory_fts_search(terms, limit=20):
    """Full-text search on Knowledge table. Only meaningful for SQLite backend;
    Kusto backend falls back to the existing lexical/semantic recall pipeline."""
    backend = _resolve_memory_backend()
    if backend == "sqlite":
        mem = _get_sqlite_mem()
        return mem.fts_search("Knowledge", terms, limit=limit)
    return []



def _memory_available():
    """Check whether the memory backend is configured and reachable."""
    backend = _resolve_memory_backend()
    if backend == "sqlite":
        return True  # SQLite is always available (file is created on demand)
    else:
        cluster, db = _get_kusto_config()
        return bool(cluster and db and _st.kusto_token_cache)

