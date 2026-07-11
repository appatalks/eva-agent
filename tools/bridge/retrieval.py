"""Phase 2 retrieval: deterministic scoring and ranking for memory recall.

All functions are pure (no side effects), deterministic given same inputs,
support injected clocks for testability. No network calls.

Timestamp contract:
- Input must be string ISO8601. Naive timestamps are treated as UTC.
- Aware timestamps are normalized to UTC for comparison.
- Numeric epoch values are NOT accepted (must be ISO string).
- Malformed claim timestamps are rejected; malformed ``now`` fails closed.
- Future timestamps clamp age to 0.

Candidate contract:
- Input list >200 raises ValueError (fail closed, not silently truncated).
- Input list is never mutated.
- Max results <= 6.
- ClaimId must be non-empty string; mixed IDs cannot crash.

Semantic contract:
- Absent/invalid SemanticScore => None (renormalized out even if globally available).
- Explicit measured 0.0 stays 0.0 (not treated as absent).
- NaN/Inf rejected from all numeric inputs.

Cache identity contract:
- Uses length-delimited canonical fields: ObjectType|ObjectId|provider|model|
  model_version|dimensions|encoding|content_hash|consent_fingerprint.
- Strict validated write/read APIs.
- lookup_embedding_cache requires full identity (not key alone).

Prompt renderer contract:
- Canonical JSON-lines format with escaped fields.
- No literal newlines/tabs/CR/quote breakout.
- Strips bidi controls, LSEP, PSEP, C0/C1 control chars.
- Neutralizes [[EVA_ case-insensitively and role headers structurally.
- Explicit untrusted data header/footer.
- No IDs in output.
- Caps applied after escaping.
"""

import hashlib
import json
import math
import re
import struct
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ── Constants ───────────────────────────────────────────────────────

CANDIDATE_CAP = 200
RESULT_CAP = 6
VALUE_CAP = 500
CONTEXT_CAP = 4096

WEIGHT_LEXICAL = 0.35
WEIGHT_SEMANTIC = 0.30
WEIGHT_TEMPORAL = 0.15
WEIGHT_CONFIDENCE = 0.15
WEIGHT_PROVENANCE = 0.05

_ALL_WEIGHTS = {
    "lexical": WEIGHT_LEXICAL,
    "semantic": WEIGHT_SEMANTIC,
    "temporal": WEIGHT_TEMPORAL,
    "confidence": WEIGHT_CONFIDENCE,
    "provenance": WEIGHT_PROVENANCE,
}

DEFAULT_HALF_LIFE_DAYS = 30.0
MAX_EVIDENCE_COUNT = 1_000_000

# Allowed sensitivity values (case-sensitive)
ALLOWED_SENSITIVITIES = frozenset({"public", "normal", "private", "secret"})
# Allowed consent scopes (case-sensitive)
ALLOWED_CONSENT_SCOPES = frozenset({"local_only", "session", "cloud_allowed", "deleted"})
# Allowed encoding
ALLOWED_ENCODINGS = frozenset({"f32le"})

# ── Normalization ───────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_MAX_TOKENS = 512
_MAX_TOKEN_CHARS = 64
_MAX_TEXT_CHARS = 32768
_CANONICAL_EXPIRY_RE = re.compile(
    r"(?:[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
    r"|[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"\.[0-9]{6}Z)\Z"
)


def normalize_text(text: str) -> str:
    """Normalize to NFC Unicode form, lowercase, strip."""
    if not isinstance(text, str):
        return ""
    return unicodedata.normalize("NFC", text).strip().casefold()


def tokenize(text: str) -> List[str]:
    """Tokenize bounded text into bounded Unicode NFC word tokens."""
    if not isinstance(text, str):
        return []
    normalized = normalize_text(text[:_MAX_TEXT_CHARS])
    tokens = []
    for match in _TOKEN_RE.finditer(normalized):
        tokens.append(match.group(0)[:_MAX_TOKEN_CHARS])
        if len(tokens) >= _MAX_TOKENS:
            break
    return tokens


# ── Timestamp parsing ───────────────────────────────────────────────

def parse_timestamp(ts: Any) -> Optional[datetime]:
    """Parse ISO8601 string timestamp. Returns UTC-aware datetime or None.

    Contract:
    - Must be a non-empty string (rejects int/float/None).
    - Naive timestamps are treated as UTC.
    - Aware timestamps are normalized to UTC.
    - Numeric epoch values are NOT accepted.
    - Malformed returns None.
    """
    if not isinstance(ts, str) or not ts.strip():
        return None
    try:
        cleaned = ts.strip()
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def parse_cache_expiry(ts: Any) -> Optional[datetime]:
    """Parse only canonical UTC cache expiry text.

    Accepted forms are exactly ``YYYY-MM-DDTHH:MM:SSZ`` and
    ``YYYY-MM-DDTHH:MM:SS.ffffffZ``. Offsets, alternate separators,
    whitespace, and non-six-digit fractions are rejected rather than
    normalized.
    """
    if not isinstance(ts, str) or _CANONICAL_EXPIRY_RE.fullmatch(ts) is None:
        return None
    try:
        return datetime.fromisoformat(ts[:-1] + "+00:00").astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


# ── Component Scorers ───────────────────────────────────────────────

def _is_valid_float(value) -> bool:
    """Reject NaN, Infinity, non-numeric, and bool."""
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    try:
        converted = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    return math.isfinite(converted)


def lexical_score(query_tokens: List[str], candidate_tokens: List[str]) -> float:
    """Deterministic token overlap score in [0, 1]."""
    if not query_tokens or not candidate_tokens:
        return 0.0
    query_set = set(query_tokens)
    candidate_set = set(candidate_tokens)
    if not query_set or not candidate_set:
        return 0.0
    intersection = query_set & candidate_set
    score = len(intersection) / len(query_set)
    return max(0.0, min(1.0, score))


def temporal_score(age_days: float, half_life_days: float = DEFAULT_HALF_LIFE_DAYS) -> float:
    """Exponential temporal decay. Future clamps age to 0. Invalid returns 0."""
    if not _is_valid_float(age_days) or not _is_valid_float(half_life_days):
        return 0.0
    if half_life_days <= 0.0:
        return 0.0
    effective_age = max(0.0, float(age_days))
    result = math.exp(-math.log(2) * effective_age / half_life_days)
    return max(0.0, min(1.0, result))


def effective_confidence(
    base_confidence: float,
    trust: float,
    decay_rate: float,
    age_days: float,
) -> float:
    """C = clamp(base * trust * exp(-decay * age), 0, 1). Rejects NaN/Inf/bool."""
    for val in (base_confidence, trust, decay_rate, age_days):
        if not _is_valid_float(val):
            return 0.0
    if base_confidence < 0.0 or base_confidence > 1.0:
        return 0.0
    if trust < 0.0 or trust > 1.0:
        return 0.0
    if decay_rate < 0.0:
        return 0.0
    effective_age = max(0.0, float(age_days))
    raw = base_confidence * trust * math.exp(-decay_rate * effective_age)
    return max(0.0, min(1.0, raw))


def provenance_score(evidence_count: int, max_evidence: int = 10) -> float:
    """Bounded provenance score. Rejects non-int, bool, and oversized input."""
    if isinstance(evidence_count, bool) or not isinstance(evidence_count, int):
        return 0.0
    if evidence_count < 0 or evidence_count > MAX_EVIDENCE_COUNT:
        return 0.0
    if (
        isinstance(max_evidence, bool)
        or not isinstance(max_evidence, int)
        or max_evidence <= 0
        or max_evidence > MAX_EVIDENCE_COUNT
    ):
        return 0.0
    if evidence_count >= max_evidence:
        return 1.0
    return evidence_count / max_evidence


def compute_final_score(
    lexical: Optional[float],
    semantic: Optional[float],
    temporal: Optional[float],
    confidence: Optional[float],
    provenance: Optional[float],
) -> float:
    """Weighted final score with renormalization when components are None.

    None components are omitted and weights renormalized.
    Explicit 0.0 stays 0.0 (counted with its weight).
    NaN/Inf in any component rejects entire score.
    """
    components = {
        "lexical": lexical,
        "semantic": semantic,
        "temporal": temporal,
        "confidence": confidence,
        "provenance": provenance,
    }

    available_sum = 0.0
    weighted_sum = 0.0
    for name, value in components.items():
        if value is None:
            continue
        if not _is_valid_float(value):
            return 0.0
        weight = _ALL_WEIGHTS[name]
        available_sum += weight
        weighted_sum += weight * max(0.0, min(1.0, float(value)))

    if available_sum <= 0.0:
        return 0.0
    return weighted_sum / available_sum


# ── Candidate ranking ───────────────────────────────────────────────

def rank_candidates(
    candidates: List[Dict[str, Any]],
    query_tokens: List[str],
    now_iso: str,
    *,
    semantic_available: bool = False,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> List[Dict[str, Any]]:
    """Rank candidates by composite score with stable ordering.

    Raises ValueError if len(candidates) > CANDIDATE_CAP (fail closed).
    Does NOT mutate the input list.
    Sort: score DESC, effective confidence DESC, normalized UTC observation
    instant DESC, ClaimId ASC (deterministic).
    Results capped at RESULT_CAP.

    Malformed claim timestamps reject the candidate. Malformed ``now_iso``
    rejects the entire operation.
    """
    if not isinstance(candidates, list):
        raise ValueError("candidates must be a list")
    if len(candidates) > CANDIDATE_CAP:
        raise ValueError(
            f"candidate count {len(candidates)} exceeds cap {CANDIDATE_CAP}"
        )

    if not isinstance(query_tokens, list) or len(query_tokens) > _MAX_TOKENS:
        raise ValueError("query_tokens must be a bounded list")
    normalized_query_tokens = []
    for token in query_tokens:
        if not isinstance(token, str) or len(token) > _MAX_TOKEN_CHARS:
            raise ValueError("query token must be a bounded string")
        normalized_query_tokens.extend(tokenize(token))
        if len(normalized_query_tokens) > _MAX_TOKENS:
            raise ValueError("query token count exceeds cap")
    if not isinstance(semantic_available, bool):
        raise ValueError("semantic_available must be boolean")
    if not _is_valid_float(half_life_days) or half_life_days <= 0:
        raise ValueError("half_life_days must be a finite positive number")
    now_dt = parse_timestamp(now_iso)
    if now_dt is None:
        raise ValueError("now_iso must be a valid ISO8601 timestamp")

    scored = []
    for candidate in candidates:
        # ClaimId: strict normalized string, non-empty
        if not isinstance(candidate, dict):
            continue
        claim_id = candidate.get("ClaimId")
        if not isinstance(claim_id, str) or not claim_id.strip():
            continue
        claim_id = unicodedata.normalize("NFC", claim_id.strip())
        if len(claim_id) > 256:
            continue

        subject = candidate.get("Subject")
        predicate = candidate.get("Predicate")
        obj = candidate.get("Object")
        if not all(isinstance(value, str) and value for value in (subject, predicate, obj)):
            continue
        subject = unicodedata.normalize("NFC", subject)
        predicate = unicodedata.normalize("NFC", predicate)
        obj = unicodedata.normalize("NFC", obj)
        if len(subject) > 512 or len(predicate) > 256 or len(obj) > 2048:
            continue
        base_conf = candidate.get("Confidence", 0.0)
        trust = candidate.get("Trust", 0.0)
        decay_rate = candidate.get("DecayRate", 0.01)
        observed_at = candidate.get("ObservedAt", "")
        evidence_count = candidate.get("EvidenceCount", 0)

        # Reject NaN/Inf/bool in numeric fields
        if (
            not _is_valid_float(base_conf)
            or not _is_valid_float(trust)
            or not _is_valid_float(decay_rate)
            or not 0.0 <= float(base_conf) <= 1.0
            or not 0.0 <= float(trust) <= 1.0
            or not 0.0 <= float(decay_rate) <= 1.0
        ):
            continue

        # Lexical score
        candidate_text = f"{subject} {predicate} {obj}"
        candidate_tokens = tokenize(candidate_text)
        l_score = lexical_score(normalized_query_tokens, candidate_tokens)

        # Parse observed timestamp
        observed_dt = parse_timestamp(observed_at)

        # Approved claims require valid temporal provenance. A malformed
        # timestamp is rejected rather than receiving a renormalization boost.
        if observed_dt is None:
            continue
        age_days = (now_dt - observed_dt).total_seconds() / 86400.0
        t_score = temporal_score(age_days, half_life_days)
        c_score = effective_confidence(
            float(base_conf), float(trust), float(decay_rate), max(0.0, age_days)
        )

        # Provenance score: evidence counts are strict non-negative integers.
        if (
            isinstance(evidence_count, bool)
            or not isinstance(evidence_count, int)
            or evidence_count < 0
            or evidence_count > MAX_EVIDENCE_COUNT
        ):
            continue
        p_score = provenance_score(evidence_count)

        # Semantic score: None if absent/invalid even if globally available
        s_score = None
        if semantic_available:
            raw_semantic = candidate.get("SemanticScore")
            if (
                raw_semantic is not None
                and _is_valid_float(raw_semantic)
                and 0.0 <= float(raw_semantic) <= 1.0
            ):
                # Explicit 0.0 stays 0.0
                s_score = float(raw_semantic)
            else:
                # Absent/invalid => None (renormalized out)
                s_score = None

        final = compute_final_score(l_score, s_score, t_score, c_score, p_score)
        observed_delta = observed_dt - datetime(1970, 1, 1, tzinfo=timezone.utc)
        observed_epoch_us = (
            observed_delta.days * 86_400_000_000
            + observed_delta.seconds * 1_000_000
            + observed_delta.microseconds
        )

        scored.append({
            **candidate,
            "ClaimId": claim_id,
            "Subject": subject,
            "Predicate": predicate,
            "Object": obj,
            "Confidence": base_conf,
            "Trust": trust,
            "DecayRate": decay_rate,
            "ObservedAt": observed_at,
            "EvidenceCount": evidence_count,
            "_score": final,
            "_effective_confidence": c_score,
            "_observed_at": observed_dt.isoformat().replace("+00:00", "Z"),
            "_observed_epoch_us": observed_epoch_us,
        })

    # Stable sort: score DESC, then malformed timestamps last, then ClaimId ASC
    def _sort_key(x):
        return (
            -x["_score"], -x["_effective_confidence"],
            -x["_observed_epoch_us"], x["ClaimId"],
        )

    scored.sort(key=_sort_key)
    return scored[:RESULT_CAP]


# ── Untrusted context rendering (JSON-lines) ───────────────────────

# Bidi controls, line/paragraph separators, C0/C1 control chars
_UNSAFE_CHARS_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f"
    r"\u00ad\u061c\u180e\u200b-\u200f"  # invisible/bidi controls
    r"\u202a-\u202e\u2060-\u206f"  # bidi formatting/deprecated controls
    r"\u2028\u2029"  # LSEP/PSEP
    r"\ufeff"  # BOM
    r"]"
)
_ACTION_MARKER_RE = re.compile(r"\[\[EVA_", re.IGNORECASE)
_ROLE_HEADER_RE = re.compile(
    r"^([ \t]*)(?:(system|user|assistant|developer)[ \t]*:"
    r"|\[(system|user|assistant|developer)\][ \t]*:?)",
    re.IGNORECASE | re.MULTILINE,
)


def _sanitize_field(text: str) -> str:
    """Sanitize a single field value for safe prompt injection."""
    text = unicodedata.normalize("NFC", text)
    # Canonicalize carriage returns before removing controls or scanning
    # structure. Removing CR later could reconstruct [[EVA_ or role headers.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Strip unsafe characters
    text = _UNSAFE_CHARS_RE.sub("", text)
    # Neutralize [[EVA_ markers (case-insensitive) by inserting ZWS
    text = _ACTION_MARKER_RE.sub("[\u200b[EVA\u200b_", text)
    # Neutralize role headers with a full-width colon before flattening lines.
    text = _ROLE_HEADER_RE.sub(
        lambda match: match.group(1) + (match.group(2) or match.group(3)) + "：",
        text,
    )
    # No literal newlines/tabs/CR in field values.
    text = text.replace("\n", " ")
    text = text.replace("\t", " ")
    text = _ACTION_MARKER_RE.sub("[\u200b[EVA\u200b_", text)
    text = _ROLE_HEADER_RE.sub(
        lambda match: match.group(1) + (match.group(2) or match.group(3)) + "：",
        text,
    )
    return text


def _bounded_json_field(value: Any, cap: int = VALUE_CAP) -> str:
    """Return a sanitized value whose JSON string encoding is at most *cap*."""
    sanitized = _sanitize_field(str(value))
    low, high = 0, len(sanitized)
    best = ""
    while low <= high:
        middle = (low + high) // 2
        candidate = sanitized[:middle]
        encoded = json.dumps(candidate, ensure_ascii=True, separators=(",", ":"))
        if len(encoded) <= cap:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


def render_untrusted_context(claims: List[Dict[str, Any]], context_cap: int = CONTEXT_CAP) -> str:
    """Render claims as bounded JSON-lines with untrusted data markers.

    Format:
      --- BEGIN UNTRUSTED RECALLED DATA ---
      {"subject":"...","predicate":"...","object":"..."}
      ...
      --- END UNTRUSTED RECALLED DATA ---

    No IDs. Caps applied after escaping. No partial malformed lines.
    """
    header = "--- BEGIN UNTRUSTED RECALLED DATA ---"
    footer = "--- END UNTRUSTED RECALLED DATA ---"

    if not isinstance(claims, list):
        raise ValueError("claims must be a list")
    if isinstance(context_cap, bool) or not isinstance(context_cap, int):
        raise ValueError("context_cap must be an integer")
    effective_cap = min(max(0, context_cap), CONTEXT_CAP)
    minimum = len(header) + 1 + len(footer)
    if effective_cap < minimum:
        return ""

    lines = [header]
    current_length = len(header) + 1  # +1 for newline

    for claim in claims:
        if not isinstance(claim, dict):
            continue
        record = {
            "object": _bounded_json_field(claim.get("Object", "")),
            "predicate": _bounded_json_field(claim.get("Predicate", "")),
            "subject": _bounded_json_field(claim.get("Subject", "")),
        }
        line = json.dumps(
            record, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )

        # Check cap (include footer space)
        needed = len(line) + 1 + len(footer) + 1
        if current_length + needed > effective_cap:
            break
        lines.append(line)
        current_length += len(line) + 1

    lines.append(footer)
    return "\n".join(lines)


# ── Semantic consent/eligibility ────────────────────────────────────

def is_semantic_eligible(
    *,
    phase2_enabled: bool,
    semantic_mode: str,
    egress_mode: str,
    query_consent: bool,
    sensitivity: str,
    consent_scope: str,
) -> bool:
    """Determine if eligible for semantic (cloud) processing. Fail closed.

    Requires ALL:
    - phase2_enabled is actual True (not truthy int)
    - semantic_mode == "openai" (case-sensitive, exact allowlist)
    - egress_mode == "cloud"
    - query_consent is actual True
    - sensitivity in ALLOWED_SENSITIVITIES and != "secret"
    - consent_scope == "cloud_allowed" (case-sensitive, exact allowlist)

    Unknown/unlisted values fail closed.
    """
    if not isinstance(phase2_enabled, bool) or not phase2_enabled:
        return False
    if not isinstance(semantic_mode, str) or semantic_mode != "openai":
        return False
    if not isinstance(egress_mode, str) or egress_mode != "cloud":
        return False
    if not isinstance(query_consent, bool) or not query_consent:
        return False
    if not isinstance(sensitivity, str) or sensitivity not in ALLOWED_SENSITIVITIES:
        return False
    if sensitivity == "secret":
        return False
    if not isinstance(consent_scope, str) or consent_scope not in ALLOWED_CONSENT_SCOPES:
        return False
    if consent_scope != "cloud_allowed":
        return False
    return True


# ── Embedding cache identity ────────────────────────────────────────

def embedding_cache_key(
    *,
    object_type: str,
    object_id: str,
    provider: str,
    model: str,
    model_version: str,
    dimensions: int,
    encoding: str,
    content_hash: str,
    consent_fingerprint: str,
) -> str:
    """Deterministic cache key using length-delimited canonical fields.

    All fields are validated. Returns SHA-256 hex digest (64 chars).
    """
    identity = _validate_cache_identity_fields(
        object_type=object_type, object_id=object_id,
        provider=provider, model=model, model_version=model_version,
        dimensions=dimensions, encoding=encoding,
        content_hash=content_hash, consent_fingerprint=consent_fingerprint,
    )
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validate_cache_identity_fields(
    *, object_type, object_id, provider, model, model_version,
    dimensions, encoding, content_hash, consent_fingerprint,
):
    """Validate all cache identity fields strictly."""
    identity = {}
    for name, val, max_len in [
        ("object_type", object_type, 64),
        ("object_id", object_id, 256),
        ("provider", provider, 128),
        ("model", model, 256),
        ("model_version", model_version, 64),
        ("consent_fingerprint", consent_fingerprint, 64),
    ]:
        if not isinstance(val, str):
            raise ValueError(f"{name} must be non-empty string <= {max_len} chars")
        normalized = unicodedata.normalize("NFC", val).strip()
        if (
            not normalized or len(normalized) > max_len
            or _UNSAFE_CHARS_RE.search(normalized)
        ):
            raise ValueError(f"{name} must be non-empty string <= {max_len} chars")
        identity[name] = normalized

    if (
        not isinstance(content_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", content_hash)
    ):
        raise ValueError("content_hash must be exactly 64 hex chars (SHA-256)")
    identity["content_hash"] = content_hash

    if not re.fullmatch(r"[0-9a-f]{64}", identity["consent_fingerprint"]):
        raise ValueError("consent_fingerprint must be a SHA-256 hex string")

    if isinstance(dimensions, bool) or not isinstance(dimensions, int):
        raise ValueError("dimensions must be integer")
    if dimensions < 1 or dimensions > 8192:
        raise ValueError("dimensions must be 1..8192")
    identity["dimensions"] = dimensions

    if encoding not in ALLOWED_ENCODINGS:
        raise ValueError(f"encoding must be one of {sorted(ALLOWED_ENCODINGS)}")
    identity["encoding"] = encoding
    return identity


def content_hash(text: str) -> str:
    """SHA-256 hash of NFC-normalized text (64 hex chars)."""
    if not isinstance(text, str):
        raise ValueError("content_hash input must be string")
    normalized = unicodedata.normalize("NFC", text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def build_consent_fingerprint(
    *, sensitivity: str, consent_scope: str, query_consent: bool,
    policy_version: str = "phase2-v1",
) -> str:
    """Hash the exact low-cardinality consent decision used for a cache row."""
    if not isinstance(sensitivity, str) or sensitivity not in ALLOWED_SENSITIVITIES:
        raise ValueError("invalid sensitivity")
    if not isinstance(consent_scope, str) or consent_scope not in ALLOWED_CONSENT_SCOPES:
        raise ValueError("invalid consent_scope")
    if not isinstance(query_consent, bool):
        raise ValueError("invalid query_consent")
    if (
        not isinstance(policy_version, str)
        or not policy_version.strip()
        or len(policy_version.strip()) > 128
        or _UNSAFE_CHARS_RE.search(policy_version)
        or any(char in policy_version for char in "\r\n\t")
    ):
        raise ValueError("invalid policy_version")
    canonical = json.dumps(
        {
            "consent_scope": consent_scope,
            "policy_version": unicodedata.normalize("NFC", policy_version.strip()),
            "query_consent": query_consent,
            "sensitivity": sensitivity,
        },
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _clock_now(clock=None) -> datetime:
    now = clock() if clock else datetime.now(timezone.utc)
    if not isinstance(now, datetime):
        raise ValueError("clock must return datetime")
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def write_embedding_cache(
    conn,
    *,
    object_type: str,
    object_id: str,
    provider: str,
    model: str,
    model_version: str,
    dimensions: int,
    encoding: str = "f32le",
    content_hash_hex: str,
    consent_fingerprint: str,
    embedding_blob: bytes,
    expires_at: Optional[str] = None,
    clock=None,
):
    """Write a validated embedding to cache. Strict validation of all fields.

    - embedding_blob length must equal dimensions * 4 (f32le)
    - All floats in blob must be finite
    - expires_at if provided must be parseable ISO timestamp
    """
    identity = _validate_cache_identity_fields(
        object_type=object_type, object_id=object_id,
        provider=provider, model=model, model_version=model_version,
        dimensions=dimensions, encoding=encoding,
        content_hash=content_hash_hex, consent_fingerprint=consent_fingerprint,
    )

    if not isinstance(embedding_blob, bytes):
        raise ValueError("embedding_blob must be bytes")
    expected_len = dimensions * 4
    if len(embedding_blob) != expected_len:
        raise ValueError(
            f"embedding_blob length {len(embedding_blob)} != dimensions*4 ({expected_len})"
        )

    # Verify all floats are finite
    float_count = dimensions
    floats = struct.unpack(f"<{float_count}f", embedding_blob)
    for f in floats:
        if math.isnan(f) or math.isinf(f):
            raise ValueError("embedding contains NaN or Inf values")

    canonical_expiry = None
    if expires_at is not None:
        parsed_expiry = parse_cache_expiry(expires_at)
        if parsed_expiry is None:
            raise ValueError("expires_at must be canonical UTC timestamp string or None")
        canonical_expiry = expires_at

    created_at = _clock_now(clock).isoformat().replace("+00:00", "Z")

    cache_key = embedding_cache_key(
        object_type=object_type, object_id=object_id,
        provider=provider, model=model, model_version=model_version,
        dimensions=dimensions, encoding=encoding,
        content_hash=content_hash_hex, consent_fingerprint=consent_fingerprint,
    )

    conn.execute(
        "INSERT OR REPLACE INTO MemoryEmbeddingCache "
        "(CacheKey,ObjectType,ObjectId,Provider,Model,ModelVersion,Dimensions,"
        "Encoding,ContentHash,ConsentFingerprint,Embedding,CreatedAt,ExpiresAt) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            cache_key, identity["object_type"], identity["object_id"],
            identity["provider"], identity["model"], identity["model_version"],
            identity["dimensions"], identity["encoding"],
            identity["content_hash"], identity["consent_fingerprint"],
            embedding_blob, created_at, canonical_expiry,
        ),
    )
    return cache_key


def lookup_embedding_cache(
    conn,
    *,
    object_type: str,
    object_id: str,
    provider: str,
    model: str,
    model_version: str,
    dimensions: int,
    encoding: str = "f32le",
    content_hash_hex: str,
    consent_fingerprint: str,
    clock=None,
) -> Optional[bytes]:
    """Look up cached embedding by full identity. Returns None on miss or stale.

    Cannot take key alone. Validates all identity fields.
    Checks expiry against injected clock (or utcnow).
    Verifies metadata matches and blob is valid.
    """
    identity = _validate_cache_identity_fields(
        object_type=object_type, object_id=object_id,
        provider=provider, model=model, model_version=model_version,
        dimensions=dimensions, encoding=encoding,
        content_hash=content_hash_hex, consent_fingerprint=consent_fingerprint,
    )

    cache_key = embedding_cache_key(
        object_type=object_type, object_id=object_id,
        provider=provider, model=model, model_version=model_version,
        dimensions=dimensions, encoding=encoding,
        content_hash=content_hash_hex, consent_fingerprint=consent_fingerprint,
    )

    row = conn.execute(
        "SELECT Embedding, ExpiresAt, Provider, Model, ModelVersion, "
        "Dimensions, Encoding, ContentHash, ConsentFingerprint, ObjectType, ObjectId "
        "FROM MemoryEmbeddingCache WHERE CacheKey = ?",
        (cache_key,),
    ).fetchone()

    if row is None:
        return None

    blob, expires, r_prov, r_model, r_ver, r_dims, r_enc, r_hash, r_fp, r_otype, r_oid = row

    # Metadata match verification
    if (r_prov != identity["provider"] or r_model != identity["model"]
            or r_ver != identity["model_version"]
            or r_dims != identity["dimensions"] or r_enc != identity["encoding"]
            or r_hash != identity["content_hash"]
            or r_fp != identity["consent_fingerprint"]
            or r_otype != identity["object_type"] or r_oid != identity["object_id"]):
        return None

    # Expiry check
    if expires is not None:
        now = _clock_now(clock)
        exp_dt = parse_cache_expiry(expires)
        if exp_dt is None or now >= exp_dt:
            return None

    # Blob validation
    if not isinstance(blob, bytes) or len(blob) != dimensions * 4:
        return None

    # Verify finite floats
    try:
        floats = struct.unpack(f"<{dimensions}f", blob)
        if any(math.isnan(f) or math.isinf(f) for f in floats):
            return None
    except struct.error:
        return None

    return blob


def delete_embedding_by_object(conn, *, object_type: str, object_id: str) -> int:
    """Delete all cached embeddings for a specific object. Returns count deleted."""
    if (
        not isinstance(object_type, str)
        or not object_type.strip()
        or len(object_type.strip()) > 64
        or _UNSAFE_CHARS_RE.search(object_type)
    ):
        raise ValueError("object_type must be non-empty string")
    if (
        not isinstance(object_id, str)
        or not object_id.strip()
        or len(object_id.strip()) > 256
        or _UNSAFE_CHARS_RE.search(object_id)
    ):
        raise ValueError("object_id must be non-empty string")
    object_type = unicodedata.normalize("NFC", object_type.strip())
    object_id = unicodedata.normalize("NFC", object_id.strip())
    cursor = conn.execute(
        "DELETE FROM MemoryEmbeddingCache WHERE ObjectType=? AND ObjectId=?",
        (object_type, object_id),
    )
    return cursor.rowcount


def invalidate_by_consent_fingerprint(conn, *, consent_fingerprint: str) -> int:
    """Invalidate (delete) all cached embeddings for a consent fingerprint."""
    if not isinstance(consent_fingerprint, str) or not re.fullmatch(
        r"[0-9a-f]{64}", consent_fingerprint
    ):
        raise ValueError("consent_fingerprint must be SHA-256 hex")
    cursor = conn.execute(
        "DELETE FROM MemoryEmbeddingCache WHERE ConsentFingerprint=?",
        (consent_fingerprint,),
    )
    return cursor.rowcount
