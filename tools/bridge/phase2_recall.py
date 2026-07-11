"""Phase 2 iteration-2 runtime recall orchestration.

This module is deliberately local-only:
- reads immutable claims from the SQLite sidecar;
- never writes claims or legacy ``Knowledge`` rows;
- never imports or invokes an HTTP/provider client;
- uses only already-populated, fully validated embedding-cache rows;
- keeps shadow output byte-for-byte equal to legacy output;
- appends bounded untrusted JSON lines only in hybrid mode;
- records only low-cardinality metrics when explicitly configured ``local``.
"""

import datetime
import math
import struct
import time
import unicodedata
from dataclasses import dataclass

from bridge.phase2_metrics import MetricRecord, record_metric
from bridge.retrieval import (
    ALLOWED_CONSENT_SCOPES,
    ALLOWED_SENSITIVITIES,
    CANDIDATE_CAP,
    build_consent_fingerprint,
    content_hash,
    embedding_cache_key,
    lookup_embedding_cache,
    rank_candidates,
    render_untrusted_context,
    tokenize,
)


_CACHE_NAMESPACE_CAP = 8
_CONTEXT_CAP = 3072
_TERMINAL_RESOLUTIONS = ("deny", "supersede", "retract", "merge")
_SEMANTIC_MODES = frozenset({"off", "cache", "openai"})
_RECALL_MODES = frozenset({"shadow", "hybrid"})


@dataclass(frozen=True)
class RecallOutcome:
    """Low-cardinality internal result with no query text or claim identity."""

    section: str
    candidate_count: int
    result_count: int
    cache_hit: bool
    fallback_used: str


class _CandidateLimitExceeded(ValueError):
    """Internal signal that eligible claim input exceeded the work cap."""


def _utc_now(clock=None):
    now = clock() if clock else datetime.datetime.now(datetime.timezone.utc)
    if not isinstance(now, datetime.datetime):
        raise ValueError("clock must return datetime")
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    return now.astimezone(datetime.timezone.utc)


def _utc_iso(value):
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _query_scope(recall_mode, egress_mode, query_consent):
    """Return SQL eligibility for local shadow or prompt-bound hybrid reads."""
    if recall_mode == "hybrid" and egress_mode == "cloud":
        if query_consent is not True:
            return None, ()
        return (
            "c.ConsentScope='cloud_allowed' "
            "AND c.Sensitivity IN ('public','normal','private')",
            (),
        )

    # Session-scoped claims are excluded until a claim carries a session
    # identity that can be matched to the current request.
    return (
        "c.ConsentScope IN ('local_only','cloud_allowed') "
        "AND c.Sensitivity IN ('public','normal','private','secret')",
        (),
    )


def _read_candidates(conn, *, recall_mode, egress_mode, query_consent):
    scope_sql, params = _query_scope(recall_mode, egress_mode, query_consent)
    if scope_sql is None:
        return []

    terminal_placeholders = ",".join("?" for _ in _TERMINAL_RESOLUTIONS)
    sql = (
        "SELECT c.ClaimId,c.Subject,c.Predicate,c.Object,c.Confidence,c.Trust,"
        "c.DecayRate,c.Sensitivity,c.ConsentScope,c.ObservedAt,"
        "(SELECT COUNT(*) FROM MemoryClaimEvidence e "
        " WHERE e.ClaimId=c.ClaimId) AS EvidenceCount "
        "FROM MemorySemanticClaims c "
        f"WHERE {scope_sql} "
        "AND NOT EXISTS ("
        " SELECT 1 FROM MemoryClaimResolutions r "
        " WHERE r.ClaimId=c.ClaimId "
        f" AND r.Action IN ({terminal_placeholders})"
        ") ORDER BY c.ClaimId COLLATE BINARY ASC LIMIT ?"
    )
    cursor = conn.execute(
        sql,
        (*params, *_TERMINAL_RESOLUTIONS, CANDIDATE_CAP + 1),
    )
    columns = [description[0] for description in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    if len(rows) > CANDIDATE_CAP:
        raise _CandidateLimitExceeded("eligible candidate input exceeds cap")
    return rows


def _claim_text(candidate):
    return "\n".join(
        unicodedata.normalize("NFC", str(candidate[field]))
        for field in ("Subject", "Predicate", "Object")
    )


def _decode_f32(blob, dimensions):
    try:
        values = struct.unpack(f"<{dimensions}f", blob)
    except (struct.error, TypeError):
        return None
    if any(not math.isfinite(value) for value in values):
        return None
    return values


def _cosine_score(left, right):
    if left is None or right is None or len(left) != len(right):
        return None
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    score = dot / (left_norm * right_norm)
    if not math.isfinite(score):
        return None
    return max(0.0, min(1.0, score))


def _cache_namespaces(
    conn,
    *,
    query_hash,
    query_fingerprint,
    clock,
):
    """Return the first bounded set of fully validated query namespaces.

    Rows are considered in a deterministic order. Expired or corrupt rows do
    not consume the namespace cap; if more than eight valid namespaces exist,
    the first eight by canonical identity are used.
    """
    cursor = conn.execute(
        "SELECT CacheKey,Provider,Model,ModelVersion,Dimensions,Encoding "
        "FROM MemoryEmbeddingCache "
        "WHERE ObjectType='query' AND ObjectId=? AND ContentHash=? "
        "AND ConsentFingerprint=? "
        "ORDER BY Provider COLLATE BINARY,Model COLLATE BINARY,"
        "ModelVersion COLLATE BINARY,Dimensions,Encoding COLLATE BINARY",
        (
            query_hash,
            query_hash,
            query_fingerprint,
        ),
    )
    columns = [description[0] for description in cursor.description]
    valid = []
    seen = set()
    for row in cursor:
        namespace = dict(zip(columns, row))
        string_fields = ("Provider", "Model", "ModelVersion", "Encoding")
        if any(
            not isinstance(namespace[field], str)
            or namespace[field]
            != unicodedata.normalize("NFC", namespace[field]).strip()
            for field in string_fields
        ):
            continue
        identity = {
            "provider": namespace["Provider"],
            "model": namespace["Model"],
            "model_version": namespace["ModelVersion"],
            "dimensions": namespace["Dimensions"],
            "encoding": namespace["Encoding"],
        }
        try:
            expected_key = embedding_cache_key(
                object_type="query",
                object_id=query_hash,
                content_hash=query_hash,
                consent_fingerprint=query_fingerprint,
                **identity,
            )
            if namespace["CacheKey"] != expected_key or expected_key in seen:
                continue
            query_blob = lookup_embedding_cache(
                conn,
                object_type="query",
                object_id=query_hash,
                content_hash_hex=query_hash,
                consent_fingerprint=query_fingerprint,
                clock=clock,
                **identity,
            )
        except ValueError:
            continue
        query_vector = _decode_f32(query_blob, identity["dimensions"])
        if query_vector is None:
            continue
        seen.add(expected_key)
        valid.append((identity, query_vector))
        if len(valid) >= _CACHE_NAMESPACE_CAP:
            break
    return valid


def _apply_cached_semantics(
    conn,
    candidates,
    query,
    *,
    recall_mode,
    query_consent,
    egress_mode,
    clock,
):
    """Return copied candidates plus whether at least one semantic score hit."""
    normalized_query = unicodedata.normalize("NFC", query.strip())
    query_hash = content_hash(normalized_query)
    query_scope = (
        "cloud_allowed"
        if recall_mode == "hybrid" and egress_mode == "cloud"
        else "local_only"
    )
    query_fingerprint = build_consent_fingerprint(
        sensitivity="normal",
        consent_scope=query_scope,
        query_consent=query_consent,
    )

    for identity, query_vector in _cache_namespaces(
        conn,
        query_hash=query_hash,
        query_fingerprint=query_fingerprint,
        clock=clock,
    ):
        scored = []
        hit = False
        for candidate in candidates:
            copied = dict(candidate)
            sensitivity = candidate.get("Sensitivity")
            consent_scope = candidate.get("ConsentScope")
            if (
                sensitivity not in ALLOWED_SENSITIVITIES
                or consent_scope not in ALLOWED_CONSENT_SCOPES
            ):
                scored.append(copied)
                continue
            claim_text = _claim_text(candidate)
            claim_hash = content_hash(claim_text)
            claim_fingerprint = build_consent_fingerprint(
                sensitivity=sensitivity,
                consent_scope=consent_scope,
                query_consent=query_consent,
            )
            try:
                claim_blob = lookup_embedding_cache(
                    conn,
                    object_type="claim",
                    object_id=candidate["ClaimId"],
                    content_hash_hex=claim_hash,
                    consent_fingerprint=claim_fingerprint,
                    clock=clock,
                    **identity,
                )
            except (KeyError, ValueError):
                claim_blob = None
            claim_vector = _decode_f32(claim_blob, identity["dimensions"])
            semantic_score = _cosine_score(query_vector, claim_vector)
            if semantic_score is not None:
                copied["SemanticScore"] = semantic_score
                hit = True
            scored.append(copied)
        if hit:
            return scored, True

    return [dict(candidate) for candidate in candidates], False


def _recall(
    conn,
    user_message,
    *,
    recall_mode,
    semantic_mode,
    query_consent,
    egress_mode,
    now,
    clock,
):
    candidates = _read_candidates(
        conn,
        recall_mode=recall_mode,
        egress_mode=egress_mode,
        query_consent=query_consent,
    )

    cache_hit = False
    fallback_used = ""
    semantic_available = False
    if candidates and semantic_mode in ("cache", "openai"):
        candidates, cache_hit = _apply_cached_semantics(
            conn,
            candidates,
            user_message,
            recall_mode=recall_mode,
            query_consent=query_consent,
            egress_mode=egress_mode,
            clock=clock,
        )
        semantic_available = cache_hit
        if semantic_mode == "cache" and not cache_hit:
            fallback_used = "lexical_only"
        elif semantic_mode == "openai":
            # Provider egress is intentionally absent from iteration 2.
            fallback_used = "cache_only" if cache_hit else "lexical_only"

    ranked = rank_candidates(
        candidates,
        tokenize(user_message),
        _utc_iso(now),
        semantic_available=semantic_available,
    )
    section = ""
    if recall_mode == "hybrid" and ranked:
        rendered = render_untrusted_context(ranked, context_cap=_CONTEXT_CAP)
        if rendered:
            section = "[Memory — Phase 2 Recalled Claims]\n" + rendered

    return RecallOutcome(
        section=section,
        candidate_count=len(candidates),
        result_count=len(ranked),
        cache_hit=cache_hit,
        fallback_used=fallback_used,
    )


def _record_recall_metric(
    memory,
    *,
    recall_mode,
    semantic_mode,
    outcome,
    elapsed_ms,
    analytics_mode,
):
    if analytics_mode != "local":
        return
    try:
        metric = MetricRecord(
            recall_mode=recall_mode,
            semantic_mode=semantic_mode,
            candidate_count=min(CANDIDATE_CAP, outcome.candidate_count),
            result_count=outcome.result_count,
            semantic_egress=0,
            cache_hit=int(outcome.cache_hit),
            fallback_used=outcome.fallback_used,
            latency_ms=min(300000, max(0, int(elapsed_ms))),
        )
        with memory.transaction() as conn:
            record_metric(conn, metric)
    except Exception:
        # Metrics are observational and must never alter recall behavior.
        return


def augment_memory_context(
    legacy_context,
    user_message,
    memory,
    *,
    recall_mode,
    semantic_mode,
    analytics_mode,
    query_consent,
    egress_mode,
    clock=None,
    monotonic=None,
):
    """Return legacy context unchanged (shadow/error) or safely augmented.

    All configuration is passed explicitly from startup-frozen bridge state so
    tests can prove mode behavior without reloading process globals.
    """
    if recall_mode not in _RECALL_MODES:
        return legacy_context
    if semantic_mode not in _SEMANTIC_MODES:
        return legacy_context
    if analytics_mode not in ("off", "local"):
        return legacy_context
    if not isinstance(user_message, str) or not user_message.strip():
        return legacy_context
    if not isinstance(query_consent, bool):
        return legacy_context
    if egress_mode not in ("offline", "local-network", "cloud"):
        return legacy_context

    started = None
    if analytics_mode == "local":
        try:
            timer = time.perf_counter if monotonic is None else monotonic
            if not callable(timer):
                return legacy_context
            started = timer()
            if (
                isinstance(started, bool)
                or not isinstance(started, (int, float))
                or not math.isfinite(float(started))
            ):
                return legacy_context
        except Exception:
            return legacy_context
    candidate_count = 0
    try:
        now = _utc_now(clock)
        with memory.read_connection() as conn:
            outcome = _recall(
                conn,
                user_message,
                recall_mode=recall_mode,
                semantic_mode=semantic_mode,
                query_consent=query_consent,
                egress_mode=egress_mode,
                now=now,
                clock=(lambda: now),
            )
            candidate_count = outcome.candidate_count
    except _CandidateLimitExceeded:
        outcome = RecallOutcome("", CANDIDATE_CAP, 0, False, "error")
    except Exception:
        outcome = RecallOutcome("", min(CANDIDATE_CAP, candidate_count), 0, False, "error")

    elapsed_ms = 0.0
    if analytics_mode == "local":
        try:
            finished = timer()
            if (
                isinstance(finished, bool)
                or not isinstance(finished, (int, float))
                or not math.isfinite(float(finished))
            ):
                return legacy_context
            elapsed_ms = (float(finished) - float(started)) * 1000.0
            if not math.isfinite(elapsed_ms) or elapsed_ms < 0.0:
                return legacy_context
        except Exception:
            return legacy_context
    _record_recall_metric(
        memory,
        recall_mode=recall_mode,
        semantic_mode=semantic_mode,
        outcome=outcome,
        elapsed_ms=elapsed_ms,
        analytics_mode=analytics_mode,
    )

    if recall_mode != "hybrid" or not outcome.section:
        return legacy_context
    if not legacy_context:
        return outcome.section
    separator = "" if legacy_context.endswith("\n\n") else "\n\n"
    return legacy_context + separator + outcome.section