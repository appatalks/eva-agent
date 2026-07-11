"""Phase 2 privacy-safe retrieval metrics recording and aggregation.

All metric helpers enforce fixed allowlists for categorical values and never
accept, store, or expose: query text, fact content, entity values, event IDs,
message IDs, or URLs.

Type policy:
- Counts and latency: strict integers only (bool rejected, float rejected).
- Binary fields (CacheHit, SemanticEgress): strict integer 0 or 1 (bool rejected).
- On invalid input: generic field-name-only error (never echoes the rejected value).
- NaN, Inf, float, string: all rejected for numeric fields.

Cross-field constraints (enforced in SQLite CHECK and at application layer):
- ResultCount <= CandidateCount
- CacheHit cannot be 1 if SemanticMode == "off"
- SemanticEgress can only be 1 if SemanticMode == "openai"
"""

from typing import Optional


# ── Allowlists ──────────────────────────────────────────────────────

ALLOWED_RECALL_MODES = frozenset({"legacy", "shadow", "hybrid"})
ALLOWED_SEMANTIC_MODES = frozenset({"off", "cache", "openai"})
ALLOWED_FALLBACK_VALUES = frozenset({"", "lexical_only", "cache_only", "timeout", "error"})

# Bounds for numeric fields
MAX_CANDIDATE_COUNT = 200
MAX_RESULT_COUNT = 6
MAX_LATENCY_MS = 300000  # 5 minutes


def _strict_int(value, field_name: str) -> int:
    """Validate strict integer (rejects bool, float, str, NaN, Inf)."""
    if isinstance(value, bool):
        raise ValueError(f"invalid value for {field_name}")
    if not isinstance(value, int):
        raise ValueError(f"invalid value for {field_name}")
    return value


def _strict_binary_int(value, field_name: str) -> int:
    """Validate strict 0 or 1 integer (rejects bool, float, str)."""
    if isinstance(value, bool):
        raise ValueError(f"invalid value for {field_name}")
    if not isinstance(value, int):
        raise ValueError(f"invalid value for {field_name}")
    if value not in (0, 1):
        raise ValueError(f"invalid value for {field_name}")
    return value


# ── Metric builder ──────────────────────────────────────────────────

class MetricRecord:
    """An immutable, validated, privacy-safe metric record ready for storage."""

    __slots__ = (
        "recall_mode", "semantic_mode", "candidate_count", "result_count",
        "semantic_egress", "cache_hit", "fallback_used", "latency_ms", "_sealed",
    )

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise AttributeError("MetricRecord is immutable")
        object.__setattr__(self, name, value)

    def __init__(
        self,
        *,
        recall_mode: str,
        semantic_mode: str,
        candidate_count: int = 0,
        result_count: int = 0,
        semantic_egress: int = 0,
        cache_hit: int = 0,
        fallback_used: str = "",
        latency_ms: int = 0,
    ):
        # Categorical validation
        if not isinstance(recall_mode, str) or recall_mode not in ALLOWED_RECALL_MODES:
            raise ValueError("invalid value for recall_mode")
        if not isinstance(semantic_mode, str) or semantic_mode not in ALLOWED_SEMANTIC_MODES:
            raise ValueError("invalid value for semantic_mode")
        if not isinstance(fallback_used, str) or fallback_used not in ALLOWED_FALLBACK_VALUES:
            raise ValueError("invalid value for fallback_used")

        # Strict integer validation (no bool, no float, no string)
        candidate_count = _strict_int(candidate_count, "candidate_count")
        result_count = _strict_int(result_count, "result_count")
        latency_ms = _strict_int(latency_ms, "latency_ms")
        semantic_egress = _strict_binary_int(semantic_egress, "semantic_egress")
        cache_hit = _strict_binary_int(cache_hit, "cache_hit")

        # Bounds
        if candidate_count < 0 or candidate_count > MAX_CANDIDATE_COUNT:
            raise ValueError("invalid value for candidate_count")
        if result_count < 0 or result_count > MAX_RESULT_COUNT:
            raise ValueError("invalid value for result_count")
        if latency_ms < 0 or latency_ms > MAX_LATENCY_MS:
            raise ValueError("invalid value for latency_ms")

        # Cross-field constraints
        if result_count > candidate_count:
            raise ValueError("invalid value for result_count")
        if cache_hit == 1 and semantic_mode == "off":
            raise ValueError("invalid value for cache_hit")
        if semantic_egress == 1 and semantic_mode != "openai":
            raise ValueError("invalid value for semantic_egress")

        self.recall_mode = recall_mode
        self.semantic_mode = semantic_mode
        self.candidate_count = candidate_count
        self.result_count = result_count
        self.semantic_egress = semantic_egress
        self.cache_hit = cache_hit
        self.fallback_used = fallback_used
        self.latency_ms = latency_ms
        self._sealed = True

    def to_dict(self) -> dict:
        return {
            "RecallMode": self.recall_mode,
            "SemanticMode": self.semantic_mode,
            "CandidateCount": self.candidate_count,
            "ResultCount": self.result_count,
            "SemanticEgress": self.semantic_egress,
            "CacheHit": self.cache_hit,
            "FallbackUsed": self.fallback_used,
            "LatencyMs": self.latency_ms,
        }


def record_metric(conn, metric: MetricRecord) -> None:
    """Insert a validated metric record into MemoryRetrievalMetrics."""
    if type(metric) is not MetricRecord:
        raise ValueError("invalid value for metric")
    # Revalidate current slot values at the write boundary as defense against
    # object.__setattr__ or deserialization bypassing normal immutability.
    validated = MetricRecord(
        recall_mode=metric.recall_mode,
        semantic_mode=metric.semantic_mode,
        candidate_count=metric.candidate_count,
        result_count=metric.result_count,
        semantic_egress=metric.semantic_egress,
        cache_hit=metric.cache_hit,
        fallback_used=metric.fallback_used,
        latency_ms=metric.latency_ms,
    )
    d = validated.to_dict()
    conn.execute(
        "INSERT INTO MemoryRetrievalMetrics "
        "(RecallMode,SemanticMode,CandidateCount,ResultCount,SemanticEgress,CacheHit,FallbackUsed,LatencyMs) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            d["RecallMode"], d["SemanticMode"], d["CandidateCount"],
            d["ResultCount"], d["SemanticEgress"], d["CacheHit"],
            d["FallbackUsed"], d["LatencyMs"],
        ),
    )


def aggregate_metrics(conn, *, since_iso: Optional[str] = None) -> dict:
    """Return privacy-safe aggregate statistics.

    Returns dict with:
    - total_retrievals: int
    - avg_latency_ms: float
    - avg_candidates: float
    - avg_results: float
    - semantic_egress_count: int
    - cache_hit_count: int
    - by_mode: dict[str, int]
    """
    from bridge.retrieval import parse_timestamp

    parsed_since = None
    if since_iso is not None:
        parsed_since = parse_timestamp(since_iso) if isinstance(since_iso, str) else None
        if parsed_since is None:
            raise ValueError("invalid value for since_iso")

    # ISO-8601 strings can denote the same instant with different offsets and
    # fractional precision, so lexical SQL comparison is not a time ordering.
    # Parse only low-cardinality metric fields locally for exact UTC filtering.
    count = 0
    latency_total = 0
    candidate_total = 0
    result_total = 0
    egress_total = 0
    cache_hit_total = 0
    by_mode = {}
    for row in conn.execute(
        "SELECT RecordedAt,RecallMode,SemanticMode,LatencyMs,CandidateCount,"
        "ResultCount,SemanticEgress,CacheHit,FallbackUsed "
        "FROM MemoryRetrievalMetrics"
    ):
        recorded_at = parse_timestamp(row[0])
        if recorded_at is None:
            continue
        if parsed_since is not None and recorded_at < parsed_since:
            continue
        recall_mode, semantic_mode = row[1], row[2]
        numeric = row[3:8]
        fallback_used = row[8]
        if (
            recall_mode not in ALLOWED_RECALL_MODES
            or semantic_mode not in ALLOWED_SEMANTIC_MODES
            or not isinstance(fallback_used, str)
            or fallback_used not in ALLOWED_FALLBACK_VALUES
            or any(isinstance(value, bool) or not isinstance(value, int)
                   for value in numeric)
        ):
            continue
        latency_ms, candidate_count, result_count, semantic_egress, cache_hit = numeric
        if (
            not 0 <= latency_ms <= MAX_LATENCY_MS
            or not 0 <= candidate_count <= MAX_CANDIDATE_COUNT
            or not 0 <= result_count <= MAX_RESULT_COUNT
            or result_count > candidate_count
            or semantic_egress not in (0, 1)
            or cache_hit not in (0, 1)
            or (cache_hit == 1 and semantic_mode == "off")
            or (semantic_egress == 1 and semantic_mode != "openai")
        ):
            continue
        count += 1
        latency_total += latency_ms
        candidate_total += candidate_count
        result_total += result_count
        egress_total += semantic_egress
        cache_hit_total += cache_hit
        by_mode[recall_mode] = by_mode.get(recall_mode, 0) + 1

    return {
        "total_retrievals": count,
        "avg_latency_ms": round(latency_total / count, 1) if count else 0.0,
        "avg_candidates": round(candidate_total / count, 1) if count else 0.0,
        "avg_results": round(result_total / count, 1) if count else 0.0,
        "semantic_egress_count": egress_total,
        "cache_hit_count": cache_hit_total,
        "by_mode": by_mode,
    }
