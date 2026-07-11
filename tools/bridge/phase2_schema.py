"""Phase 2 sidecar schema: additive tables with separate migration metadata.

This module creates Phase 2 tables alongside (not modifying) Phase 1 kernel
tables. Uses a dedicated ``_phase2_schema_migrations`` metadata table so old
binaries that only know Phase 1 see no drift and continue working.

Design decisions:
- Claims are pure immutable records. There are no Active/SupersededBy lifecycle
  fields because they cannot be updated (triggers forbid UPDATE). Status is
  derived from MemoryClaimResolutions: a claim without a retract/supersede
  resolution is considered active.
- Embedding cache includes full identity: ObjectType, ObjectId, Provider, Model,
  ModelVersion, Dimensions, Encoding, ContentHash, ConsentFingerprint. No raw
  text is stored.
"""

import datetime
import hashlib
import json
import re
import sqlite3


MIN_SQLITE_VERSION = (3, 26, 0)


def _normalize_sql(sql):
    """Normalize SQL syntax while preserving quoted literal contents exactly."""
    if not isinstance(sql, str):
        return ""
    normalized = []
    quote = None
    index = 0
    while index < len(sql):
        char = sql[index]
        if quote is not None:
            normalized.append(char)
            if quote == "[":
                if char == "]":
                    quote = None
            elif char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 1
                    normalized.append(sql[index])
                else:
                    quote = None
        elif char in ("'", '"', "`"):
            quote = char
            normalized.append(char)
        elif char == "[":
            quote = char
            normalized.append(char)
        elif not char.isspace():
            normalized.append(char.lower())
        index += 1
    return "".join(normalized)


class Phase2MigrationError(Exception):
    def __init__(self, version, description, cause):
        self.version = version
        self.description = description
        self.cause = cause
        super().__init__(f"Phase2 migration v{version} ({description}) FAILED fatally: {cause}")


class Phase2SchemaVerificationError(Phase2MigrationError):
    def __init__(self, version, description, detail):
        super().__init__(version, description, f"Schema verification: {detail}")


def _require_supported_sqlite():
    """Require the SQLite baseline needed for exact table_xinfo attestation."""
    try:
        current = tuple(int(part) for part in sqlite3.sqlite_version_info[:3])
    except (AttributeError, TypeError, ValueError):
        current = (0, 0, 0)
    if current < MIN_SQLITE_VERSION:
        found = ".".join(str(part) for part in current)
        required = ".".join(str(part) for part in MIN_SQLITE_VERSION)
        raise Phase2MigrationError(
            -1,
            "sqlite prerequisite",
            f"SQLite {required} or newer is required (found {found})",
        )
    return current


_META_DDL = (
    "CREATE TABLE IF NOT EXISTS _phase2_schema_migrations ("
    "version INTEGER PRIMARY KEY,"
    "description TEXT NOT NULL CHECK(typeof(description)='text' "
    "AND instr(description,char(0))=0 AND length(description)>0 "
    "AND length(description)<=256),"
    "applied_at TEXT NOT NULL CHECK(typeof(applied_at)='text' "
    "AND instr(applied_at,char(0))=0 AND length(applied_at)>0 "
    "AND length(applied_at)<=64),"
    "checksum TEXT NOT NULL CHECK(typeof(checksum)='text' "
    "AND instr(checksum,char(0))=0 AND length(checksum)=64 "
    "AND checksum NOT GLOB '*[^0-9a-f]*'))"
)
_META_DDL_NORMALIZED = _normalize_sql(_META_DDL.replace("IF NOT EXISTS ", ""))

_SQL_NOW_DEFAULT = "strftime('%Y-%m-%dT%H:%M:%f','now') || 'Z'"

# ── V1 DDL statements (canonical source of truth) ──────────────────

_V1_DDL = [
    """CREATE TABLE MemorySemanticClaims (
    ClaimId TEXT NOT NULL PRIMARY KEY
        CHECK(typeof(ClaimId)='text' AND instr(ClaimId,char(0))=0
            AND length(ClaimId)>0 AND length(ClaimId)<=256),
    Subject TEXT NOT NULL
        CHECK(typeof(Subject)='text' AND instr(Subject,char(0))=0
            AND length(Subject)>0 AND length(Subject)<=512),
    Predicate TEXT NOT NULL
        CHECK(typeof(Predicate)='text' AND instr(Predicate,char(0))=0
            AND length(Predicate)>0 AND length(Predicate)<=256),
    Object TEXT NOT NULL
        CHECK(typeof(Object)='text' AND instr(Object,char(0))=0
            AND length(Object)>0 AND length(Object)<=2048),
    Confidence REAL NOT NULL
        CHECK(typeof(Confidence) IN ('real','integer') AND Confidence>=0.0 AND Confidence<=1.0),
    Trust REAL NOT NULL
        CHECK(typeof(Trust) IN ('real','integer') AND Trust>=0.0 AND Trust<=1.0),
    DecayRate REAL NOT NULL DEFAULT 0.01
        CHECK(typeof(DecayRate) IN ('real','integer') AND DecayRate>=0.0 AND DecayRate<=1.0),
    Sensitivity TEXT NOT NULL DEFAULT 'normal'
        CHECK(typeof(Sensitivity)='text' AND instr(Sensitivity,char(0))=0
            AND Sensitivity IN ('public','normal','private','secret')),
    ConsentScope TEXT NOT NULL DEFAULT 'local_only'
        CHECK(typeof(ConsentScope)='text' AND instr(ConsentScope,char(0))=0
            AND ConsentScope IN ('local_only','session','cloud_allowed','deleted')),
    Source TEXT NOT NULL DEFAULT ''
        CHECK(typeof(Source)='text' AND instr(Source,char(0))=0 AND length(Source)<=1024),
    ObservedAt TEXT NOT NULL
        CHECK(typeof(ObservedAt)='text' AND instr(ObservedAt,char(0))=0
            AND length(ObservedAt)>0 AND length(ObservedAt)<=64),
    CreatedAt TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now') || 'Z')
        CHECK(typeof(CreatedAt)='text' AND instr(CreatedAt,char(0))=0
            AND length(CreatedAt)>0 AND length(CreatedAt)<=64)
)""",
    """CREATE TABLE MemoryClaimEvidence (
    EvidenceId TEXT NOT NULL PRIMARY KEY
        CHECK(typeof(EvidenceId)='text' AND instr(EvidenceId,char(0))=0
            AND length(EvidenceId)>0 AND length(EvidenceId)<=256),
    ClaimId TEXT NOT NULL
        CHECK(typeof(ClaimId)='text' AND instr(ClaimId,char(0))=0
            AND length(ClaimId)>0 AND length(ClaimId)<=256),
    EventId TEXT NOT NULL
        CHECK(typeof(EventId)='text' AND instr(EventId,char(0))=0
            AND length(EventId)>0 AND length(EventId)<=256),
    EvidenceType TEXT NOT NULL
        CHECK(typeof(EvidenceType)='text' AND instr(EvidenceType,char(0))=0
            AND EvidenceType IN ('direct','inferred','corroborated','contradicted')),
    Strength REAL NOT NULL
        CHECK(typeof(Strength) IN ('real','integer') AND Strength>=0.0 AND Strength<=1.0),
    RecordedAt TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now') || 'Z')
        CHECK(typeof(RecordedAt)='text' AND instr(RecordedAt,char(0))=0
            AND length(RecordedAt)>0 AND length(RecordedAt)<=64),
    FOREIGN KEY(ClaimId) REFERENCES MemorySemanticClaims(ClaimId),
    FOREIGN KEY(EventId) REFERENCES MemoryEvents(EventId)
)""",
    """CREATE TABLE MemoryClaimResolutions (
    ResolutionId TEXT NOT NULL PRIMARY KEY
        CHECK(typeof(ResolutionId)='text' AND instr(ResolutionId,char(0))=0
            AND length(ResolutionId)>0 AND length(ResolutionId)<=256),
    ClaimId TEXT NOT NULL
        CHECK(typeof(ClaimId)='text' AND instr(ClaimId,char(0))=0
            AND length(ClaimId)>0 AND length(ClaimId)<=256),
    Action TEXT NOT NULL
        CHECK(typeof(Action)='text' AND instr(Action,char(0))=0
            AND Action IN ('confirm','deny','supersede','retract','merge')),
    Reason TEXT NOT NULL DEFAULT ''
        CHECK(typeof(Reason)='text' AND instr(Reason,char(0))=0 AND length(Reason)<=2048),
    ResolvedBy TEXT NOT NULL
        CHECK(typeof(ResolvedBy)='text' AND instr(ResolvedBy,char(0))=0
            AND ResolvedBy IN ('user','system','admin')),
    ResolvedAt TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now') || 'Z')
        CHECK(typeof(ResolvedAt)='text' AND instr(ResolvedAt,char(0))=0
            AND length(ResolvedAt)>0 AND length(ResolvedAt)<=64),
    FOREIGN KEY(ClaimId) REFERENCES MemorySemanticClaims(ClaimId)
)""",
    """CREATE TABLE MemoryEmbeddingCache (
    CacheKey TEXT NOT NULL PRIMARY KEY
        CHECK(typeof(CacheKey)='text' AND instr(CacheKey,char(0))=0
            AND length(CacheKey)==64 AND CacheKey NOT GLOB '*[^0-9a-f]*'),
    ObjectType TEXT NOT NULL
        CHECK(typeof(ObjectType)='text' AND instr(ObjectType,char(0))=0
            AND length(ObjectType)>0 AND length(ObjectType)<=64),
    ObjectId TEXT NOT NULL
        CHECK(typeof(ObjectId)='text' AND instr(ObjectId,char(0))=0
            AND length(ObjectId)>0 AND length(ObjectId)<=256),
    Provider TEXT NOT NULL
        CHECK(typeof(Provider)='text' AND instr(Provider,char(0))=0
            AND length(Provider)>0 AND length(Provider)<=128),
    Model TEXT NOT NULL
        CHECK(typeof(Model)='text' AND instr(Model,char(0))=0
            AND length(Model)>0 AND length(Model)<=256),
    ModelVersion TEXT NOT NULL
        CHECK(typeof(ModelVersion)='text' AND instr(ModelVersion,char(0))=0
            AND length(ModelVersion)>0 AND length(ModelVersion)<=64),
    Dimensions INTEGER NOT NULL
        CHECK(typeof(Dimensions)='integer' AND Dimensions>0 AND Dimensions<=8192),
    Encoding TEXT NOT NULL DEFAULT 'f32le'
        CHECK(typeof(Encoding)='text' AND instr(Encoding,char(0))=0 AND Encoding='f32le'),
    ContentHash TEXT NOT NULL
        CHECK(typeof(ContentHash)='text' AND instr(ContentHash,char(0))=0
            AND length(ContentHash)==64 AND ContentHash NOT GLOB '*[^0-9a-f]*'),
    ConsentFingerprint TEXT NOT NULL
        CHECK(typeof(ConsentFingerprint)='text' AND instr(ConsentFingerprint,char(0))=0
            AND length(ConsentFingerprint)==64
            AND ConsentFingerprint NOT GLOB '*[^0-9a-f]*'),
    Embedding BLOB NOT NULL
        CHECK(typeof(Embedding)='blob' AND length(Embedding)=Dimensions*4),
    CreatedAt TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now') || 'Z')
        CHECK(typeof(CreatedAt)='text' AND instr(CreatedAt,char(0))=0
            AND length(CreatedAt)>0 AND length(CreatedAt)<=64),
    ExpiresAt TEXT CHECK(
        ExpiresAt IS NULL OR (typeof(ExpiresAt)='text'
            AND instr(ExpiresAt,char(0))=0
            AND ExpiresAt NOT GLOB '*[^0-9T:Z.-]*'
            AND ((length(ExpiresAt)=20
                    AND ExpiresAt GLOB '????-??-??T??:??:??Z')
                OR (length(ExpiresAt)=27
                    AND ExpiresAt GLOB '????-??-??T??:??:??.??????Z'))
            AND CAST(substr(ExpiresAt,1,4) AS INTEGER) BETWEEN 1 AND 9999
            AND date(substr(ExpiresAt,1,10))=substr(ExpiresAt,1,10)
            AND CAST(substr(ExpiresAt,12,2) AS INTEGER) BETWEEN 0 AND 23
            AND CAST(substr(ExpiresAt,15,2) AS INTEGER) BETWEEN 0 AND 59
            AND CAST(substr(ExpiresAt,18,2) AS INTEGER) BETWEEN 0 AND 59
            AND julianday(ExpiresAt) IS NOT NULL)),
        UNIQUE(ObjectType, ObjectId, Provider, Model, ModelVersion, Dimensions,
            Encoding, ContentHash, ConsentFingerprint)
)""",
    """CREATE TABLE MemoryRetrievalMetrics (
    MetricId INTEGER PRIMARY KEY AUTOINCREMENT,
    RecordedAt TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now') || 'Z')
        CHECK(typeof(RecordedAt)='text' AND instr(RecordedAt,char(0))=0
            AND length(RecordedAt)>0 AND length(RecordedAt)<=64),
    RecallMode TEXT NOT NULL
        CHECK(typeof(RecallMode)='text' AND instr(RecallMode,char(0))=0
            AND RecallMode IN ('legacy','shadow','hybrid')),
    SemanticMode TEXT NOT NULL
        CHECK(typeof(SemanticMode)='text' AND instr(SemanticMode,char(0))=0
            AND SemanticMode IN ('off','cache','openai')),
    CandidateCount INTEGER NOT NULL DEFAULT 0
        CHECK(typeof(CandidateCount)='integer' AND CandidateCount>=0 AND CandidateCount<=200),
    ResultCount INTEGER NOT NULL DEFAULT 0
        CHECK(typeof(ResultCount)='integer' AND ResultCount>=0 AND ResultCount<=6),
    SemanticEgress INTEGER NOT NULL DEFAULT 0
        CHECK(typeof(SemanticEgress)='integer' AND SemanticEgress IN (0,1)),
    CacheHit INTEGER NOT NULL DEFAULT 0
        CHECK(typeof(CacheHit)='integer' AND CacheHit IN (0,1)),
    FallbackUsed TEXT NOT NULL DEFAULT ''
        CHECK(typeof(FallbackUsed)='text' AND instr(FallbackUsed,char(0))=0
            AND FallbackUsed IN ('','lexical_only','cache_only','timeout','error')),
    LatencyMs INTEGER NOT NULL DEFAULT 0
        CHECK(typeof(LatencyMs)='integer' AND LatencyMs>=0 AND LatencyMs<=300000),
    CHECK(ResultCount<=CandidateCount),
    CHECK(NOT(CacheHit=1 AND SemanticMode='off')),
    CHECK(NOT(SemanticEgress=1 AND SemanticMode!='openai'))
)""",
    """CREATE TABLE MemoryConsolidationCheckpoints (
    CheckpointId TEXT NOT NULL PRIMARY KEY
        CHECK(typeof(CheckpointId)='text' AND instr(CheckpointId,char(0))=0
            AND length(CheckpointId)>0 AND length(CheckpointId)<=256),
    JobType TEXT NOT NULL
        CHECK(typeof(JobType)='text' AND instr(JobType,char(0))=0
            AND length(JobType)>0 AND length(JobType)<=128),
    CursorValue TEXT NOT NULL DEFAULT ''
        CHECK(typeof(CursorValue)='text' AND instr(CursorValue,char(0))=0
            AND length(CursorValue)<=4096),
    UpdatedAt TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now') || 'Z')
        CHECK(typeof(UpdatedAt)='text' AND instr(UpdatedAt,char(0))=0
            AND length(UpdatedAt)>0 AND length(UpdatedAt)<=64),
    Metadata TEXT NOT NULL DEFAULT '{}'
        CHECK(typeof(Metadata)='text' AND instr(Metadata,char(0))=0
            AND length(Metadata)<=65536)
)""",
]

_V1_TRIGGERS = [
    (
        "trg_claims_no_replace",
        "MemorySemanticClaims",
        "CREATE TRIGGER trg_claims_no_replace BEFORE INSERT ON MemorySemanticClaims\n"
        "WHEN EXISTS(SELECT 1 FROM MemorySemanticClaims WHERE ClaimId=NEW.ClaimId)\n"
        "BEGIN SELECT RAISE(ABORT,'MemorySemanticClaims is immutable: replacement not allowed'); END",
    ),
    (
        "trg_claims_no_update",
        "MemorySemanticClaims",
        "CREATE TRIGGER trg_claims_no_update BEFORE UPDATE ON MemorySemanticClaims\n"
        "BEGIN SELECT RAISE(ABORT,'MemorySemanticClaims is immutable: UPDATE not allowed'); END",
    ),
    (
        "trg_claims_no_delete",
        "MemorySemanticClaims",
        "CREATE TRIGGER trg_claims_no_delete BEFORE DELETE ON MemorySemanticClaims\n"
        "BEGIN SELECT RAISE(ABORT,'MemorySemanticClaims is immutable: DELETE not allowed'); END",
    ),
    (
        "trg_evidence_no_replace",
        "MemoryClaimEvidence",
        "CREATE TRIGGER trg_evidence_no_replace BEFORE INSERT ON MemoryClaimEvidence\n"
        "WHEN EXISTS(SELECT 1 FROM MemoryClaimEvidence WHERE EvidenceId=NEW.EvidenceId)\n"
        "BEGIN SELECT RAISE(ABORT,'MemoryClaimEvidence is immutable: replacement not allowed'); END",
    ),
    (
        "trg_evidence_no_update",
        "MemoryClaimEvidence",
        "CREATE TRIGGER trg_evidence_no_update BEFORE UPDATE ON MemoryClaimEvidence\n"
        "BEGIN SELECT RAISE(ABORT,'MemoryClaimEvidence is immutable: UPDATE not allowed'); END",
    ),
    (
        "trg_evidence_no_delete",
        "MemoryClaimEvidence",
        "CREATE TRIGGER trg_evidence_no_delete BEFORE DELETE ON MemoryClaimEvidence\n"
        "BEGIN SELECT RAISE(ABORT,'MemoryClaimEvidence is immutable: DELETE not allowed'); END",
    ),
    (
        "trg_resolutions_no_replace",
        "MemoryClaimResolutions",
        "CREATE TRIGGER trg_resolutions_no_replace BEFORE INSERT ON MemoryClaimResolutions\n"
        "WHEN EXISTS(SELECT 1 FROM MemoryClaimResolutions WHERE ResolutionId=NEW.ResolutionId)\n"
        "BEGIN SELECT RAISE(ABORT,'MemoryClaimResolutions is append-only: replacement not allowed'); END",
    ),
    (
        "trg_resolutions_no_update",
        "MemoryClaimResolutions",
        "CREATE TRIGGER trg_resolutions_no_update BEFORE UPDATE ON MemoryClaimResolutions\n"
        "BEGIN SELECT RAISE(ABORT,'MemoryClaimResolutions is append-only: UPDATE not allowed'); END",
    ),
    (
        "trg_resolutions_no_delete",
        "MemoryClaimResolutions",
        "CREATE TRIGGER trg_resolutions_no_delete BEFORE DELETE ON MemoryClaimResolutions\n"
        "BEGIN SELECT RAISE(ABORT,'MemoryClaimResolutions is append-only: DELETE not allowed'); END",
    ),
]

_V1_INDEXES = [
    ("idx_claims_subject", "MemorySemanticClaims", ["Subject"], False),
    ("idx_claims_predicate", "MemorySemanticClaims", ["Predicate"], False),
    ("idx_claims_observed", "MemorySemanticClaims", ["ObservedAt"], False),
    ("idx_claims_consent", "MemorySemanticClaims", ["ConsentScope"], False),
    ("idx_claims_sensitivity", "MemorySemanticClaims", ["Sensitivity"], False),
    ("idx_evidence_claim", "MemoryClaimEvidence", ["ClaimId"], False),
    ("idx_evidence_event", "MemoryClaimEvidence", ["EventId"], False),
    ("idx_resolutions_claim", "MemoryClaimResolutions", ["ClaimId"], False),
    ("idx_cache_content", "MemoryEmbeddingCache", ["ContentHash"], False),
    ("idx_cache_provider", "MemoryEmbeddingCache", ["Provider", "Model"], False),
    ("idx_cache_expires", "MemoryEmbeddingCache", ["ExpiresAt"], False),
    ("idx_cache_object", "MemoryEmbeddingCache", ["ObjectType", "ObjectId"], False),
    ("idx_cache_consent", "MemoryEmbeddingCache", ["ConsentFingerprint"], False),
    ("idx_metrics_recorded", "MemoryRetrievalMetrics", ["RecordedAt"], False),
    ("idx_metrics_mode", "MemoryRetrievalMetrics", ["RecallMode"], False),
    ("idx_checkpoints_job", "MemoryConsolidationCheckpoints", ["JobType"], False),
]

# ── Manifests ───────────────────────────────────────────────────────

_PHASE2_COLUMN_MANIFESTS = {
    "MemorySemanticClaims": {
        "ClaimId": ("TEXT", True, None, 1),
        "Subject": ("TEXT", True, None, 0),
        "Predicate": ("TEXT", True, None, 0),
        "Object": ("TEXT", True, None, 0),
        "Confidence": ("REAL", True, None, 0),
        "Trust": ("REAL", True, None, 0),
        "DecayRate": ("REAL", True, "0.01", 0),
        "Sensitivity": ("TEXT", True, "'normal'", 0),
        "ConsentScope": ("TEXT", True, "'local_only'", 0),
        "Source": ("TEXT", True, "''", 0),
        "ObservedAt": ("TEXT", True, None, 0),
        "CreatedAt": ("TEXT", True, _SQL_NOW_DEFAULT, 0),
    },
    "MemoryClaimEvidence": {
        "EvidenceId": ("TEXT", True, None, 1),
        "ClaimId": ("TEXT", True, None, 0),
        "EventId": ("TEXT", True, None, 0),
        "EvidenceType": ("TEXT", True, None, 0),
        "Strength": ("REAL", True, None, 0),
        "RecordedAt": ("TEXT", True, _SQL_NOW_DEFAULT, 0),
    },
    "MemoryClaimResolutions": {
        "ResolutionId": ("TEXT", True, None, 1),
        "ClaimId": ("TEXT", True, None, 0),
        "Action": ("TEXT", True, None, 0),
        "Reason": ("TEXT", True, "''", 0),
        "ResolvedBy": ("TEXT", True, None, 0),
        "ResolvedAt": ("TEXT", True, _SQL_NOW_DEFAULT, 0),
    },
    "MemoryEmbeddingCache": {
        "CacheKey": ("TEXT", True, None, 1),
        "ObjectType": ("TEXT", True, None, 0),
        "ObjectId": ("TEXT", True, None, 0),
        "Provider": ("TEXT", True, None, 0),
        "Model": ("TEXT", True, None, 0),
        "ModelVersion": ("TEXT", True, None, 0),
        "Dimensions": ("INTEGER", True, None, 0),
        "Encoding": ("TEXT", True, "'f32le'", 0),
        "ContentHash": ("TEXT", True, None, 0),
        "ConsentFingerprint": ("TEXT", True, None, 0),
        "Embedding": ("BLOB", True, None, 0),
        "CreatedAt": ("TEXT", True, _SQL_NOW_DEFAULT, 0),
        "ExpiresAt": ("TEXT", False, None, 0),
    },
    "MemoryRetrievalMetrics": {
        "MetricId": ("INTEGER", False, None, 1),
        "RecordedAt": ("TEXT", True, _SQL_NOW_DEFAULT, 0),
        "RecallMode": ("TEXT", True, None, 0),
        "SemanticMode": ("TEXT", True, None, 0),
        "CandidateCount": ("INTEGER", True, "0", 0),
        "ResultCount": ("INTEGER", True, "0", 0),
        "SemanticEgress": ("INTEGER", True, "0", 0),
        "CacheHit": ("INTEGER", True, "0", 0),
        "FallbackUsed": ("TEXT", True, "''", 0),
        "LatencyMs": ("INTEGER", True, "0", 0),
    },
    "MemoryConsolidationCheckpoints": {
        "CheckpointId": ("TEXT", True, None, 1),
        "JobType": ("TEXT", True, None, 0),
        "CursorValue": ("TEXT", True, "''", 0),
        "UpdatedAt": ("TEXT", True, _SQL_NOW_DEFAULT, 0),
        "Metadata": ("TEXT", True, "'{}'", 0),
    },
}

_PHASE2_INDEX_MANIFEST = {
    "MemorySemanticClaims": [
        "idx_claims_subject", "idx_claims_predicate",
        "idx_claims_observed", "idx_claims_consent", "idx_claims_sensitivity",
    ],
    "MemoryClaimEvidence": ["idx_evidence_claim", "idx_evidence_event"],
    "MemoryClaimResolutions": ["idx_resolutions_claim"],
    "MemoryEmbeddingCache": [
        "idx_cache_content", "idx_cache_provider", "idx_cache_expires",
        "idx_cache_object", "idx_cache_consent",
    ],
    "MemoryRetrievalMetrics": ["idx_metrics_recorded", "idx_metrics_mode"],
    "MemoryConsolidationCheckpoints": ["idx_checkpoints_job"],
}

_PHASE2_INDEX_SEMANTICS = {i[0]: (i[1], i[2], i[3]) for i in _V1_INDEXES}

_PHASE2_FK_MANIFEST = {
    "MemoryClaimEvidence": [
        ("MemorySemanticClaims", "ClaimId", "ClaimId", "NO ACTION", "NO ACTION", "NONE"),
        ("MemoryEvents", "EventId", "EventId", "NO ACTION", "NO ACTION", "NONE"),
    ],
    "MemoryClaimResolutions": [
        ("MemorySemanticClaims", "ClaimId", "ClaimId", "NO ACTION", "NO ACTION", "NONE"),
    ],
    "MemorySemanticClaims": [],
    "MemoryEmbeddingCache": [],
    "MemoryRetrievalMetrics": [],
    "MemoryConsolidationCheckpoints": [],
}

_PHASE2_TRIGGER_MANIFEST = {
    t[0]: _normalize_sql(t[2]) for t in _V1_TRIGGERS
}
_PHASE2_TRIGGER_TABLES = {t[0]: t[1] for t in _V1_TRIGGERS}

_PHASE2_TABLE_DDL_MANIFEST = {
    re.match(r"CREATE TABLE\s+(\w+)", ddl, re.IGNORECASE).group(1):
        _normalize_sql(ddl)
    for ddl in _V1_DDL
}


def _binary_keys(*columns):
    return tuple((column, False, "BINARY") for column in columns)


_PHASE2_CONSTRAINT_INDEX_MANIFEST = {
    "MemorySemanticClaims": [
        ("pk", True, False, _binary_keys("ClaimId")),
    ],
    "MemoryClaimEvidence": [
        ("pk", True, False, _binary_keys("EvidenceId")),
    ],
    "MemoryClaimResolutions": [
        ("pk", True, False, _binary_keys("ResolutionId")),
    ],
    "MemoryEmbeddingCache": [
        ("pk", True, False, _binary_keys("CacheKey")),
        (
            "u", True, False,
            _binary_keys(
                "ObjectType", "ObjectId", "Provider", "Model", "ModelVersion",
                "Dimensions", "Encoding", "ContentHash", "ConsentFingerprint",
            ),
        ),
    ],
    "MemoryRetrievalMetrics": [],
    "MemoryConsolidationCheckpoints": [
        ("pk", True, False, _binary_keys("CheckpointId")),
    ],
}


def _full_manifest_hash(version):
    """Compute manifest hash including full canonical schema."""
    full = {
        "ddl": [re.sub(r"\s+", " ", d).strip() for d in _V1_DDL],
        "triggers": [re.sub(r"\s+", " ", t[2]).strip() for t in _V1_TRIGGERS],
        "indexes": [[i[0], i[1], i[2], i[3]] for i in _V1_INDEXES],
        "columns": {
            t: {c: list(v) for c, v in cols.items()}
            for t, cols in _PHASE2_COLUMN_MANIFESTS.items()
        },
        "fks": _PHASE2_FK_MANIFEST,
    }
    canonical = json.dumps(full, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_PHASE2_MIGRATION_MANIFESTS = {
    1: {
        "tables": [
            "MemorySemanticClaims", "MemoryClaimEvidence", "MemoryClaimResolutions",
            "MemoryEmbeddingCache", "MemoryRetrievalMetrics", "MemoryConsolidationCheckpoints",
        ],
        "contract": "phase2 sidecar v1: immutable claims, evidence, resolutions, "
                    "embedding cache (full identity), metrics (cross-field), checkpoints",
    },
}


def _manifest_hash(version):
    return _full_manifest_hash(version)


# ── Helpers ─────────────────────────────────────────────────────────

def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _object_exists(conn, kind, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type=? AND name=?", (kind, name)
    ).fetchone() is not None


def _normalized_sql(conn, kind, name):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type=? AND name=?", (kind, name)
    ).fetchone()
    return _normalize_sql(row[0] if row and row[0] else "")


def _sqlite_affinity(declared_type):
    declared = str(declared_type or "").upper()
    if "INT" in declared:
        return "INTEGER"
    if any(marker in declared for marker in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if not declared or "BLOB" in declared:
        return "BLOB"
    if any(marker in declared for marker in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def _normalized_default(value):
    if value is None:
        return None
    return re.sub(r"\s+", "", str(value)).lower()


def _primary_key_columns(conn, table):
    rows = conn.execute(f"PRAGMA table_xinfo({table})").fetchall()
    return [row[1] for row in sorted((row for row in rows if row[5]), key=lambda row: row[5])]


def _current_version(conn):
    if not _table_exists(conn, "_phase2_schema_migrations"):
        return -1
    row = conn.execute("SELECT MAX(version) FROM _phase2_schema_migrations").fetchone()
    return row[0] if row and row[0] is not None else -1


# ── v1 DDL execution ───────────────────────────────────────────────

def _create_phase2_tables(conn):
    """Create all Phase 2 sidecar tables, triggers, indexes."""
    conn.execute("PRAGMA foreign_keys=ON")
    for ddl in _V1_DDL:
        conn.execute(ddl)
    for _name, _table, sql in _V1_TRIGGERS:
        conn.execute(sql)
    for name, table, columns, unique in _V1_INDEXES:
        cols = ", ".join(columns)
        uq = "UNIQUE " if unique else ""
        conn.execute(f"CREATE {uq}INDEX {name} ON {table}({cols})")


def _m1_sidecar_tables(conn):
    """Migration v1: Create all Phase 2 sidecar tables."""
    _create_phase2_tables(conn)


_PHASE2_MIGRATIONS = [
    (1, "phase2 sidecar v1", _manifest_hash(1), _m1_sidecar_tables),
]


# ── Verification (Phase1-equivalent rigor) ──────────────────────────

def _verify_column_manifest(conn, table, version):
    """Verify exact columns + hidden=0."""
    rows = conn.execute(f"PRAGMA table_xinfo({table})").fetchall()
    actual = {row[1]: row for row in rows}
    expected = _PHASE2_COLUMN_MANIFESTS[table]

    if set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        raise Phase2SchemaVerificationError(
            version, "phase2 sidecar",
            f"{table} column manifest drift (missing={missing}, extra={extra})",
        )

    for name, (affinity, not_null, default, pk_position) in expected.items():
        row = actual[name]
        observed = (
            _sqlite_affinity(row[2]), bool(row[3]),
            _normalized_default(row[4]), int(row[5]),
        )
        wanted = (affinity, not_null, _normalized_default(default), pk_position)
        if observed != wanted:
            raise Phase2SchemaVerificationError(
                version, "phase2 sidecar",
                f"{table}.{name} manifest drift: {observed} != {wanted}",
            )
        if len(row) >= 7 and int(row[6]) != 0:
            raise Phase2SchemaVerificationError(
                version, "phase2 sidecar",
                f"{table}.{name} hidden/generated-column drift",
            )


def _verify_index(conn, table, name, columns, unique, version):
    """Verify index semantics: unique, origin=c, non-partial, columns, collation, sort."""
    matches = [
        row for row in conn.execute(f"PRAGMA index_list({table})").fetchall()
        if row[1] == name
    ]
    if len(matches) != 1:
        raise Phase2SchemaVerificationError(
            version, "phase2 sidecar", f"index {name} missing on {table}"
        )
    row = matches[0]
    if bool(row[2]) != unique or row[3] != "c" or bool(row[4]):
        raise Phase2SchemaVerificationError(
            version, "phase2 sidecar",
            f"index semantics drift: {name} unique={row[2]} origin={row[3]} partial={row[4]}",
        )
    key_rows = [
        entry for entry in conn.execute(f"PRAGMA index_xinfo({name})").fetchall()
        if entry[5]
    ]
    actual_cols = [entry[2] for entry in key_rows]
    if actual_cols != columns:
        raise Phase2SchemaVerificationError(
            version, "phase2 sidecar",
            f"index key drift: {name} {actual_cols} != {columns}",
        )
    for entry in key_rows:
        if entry[4] != "BINARY" or bool(entry[3]):
            raise Phase2SchemaVerificationError(
                version, "phase2 sidecar",
                f"index collation/sort drift: {name}.{entry[2]}",
            )


def _verify_user_index_set(conn, table, expected_names, version):
    """Verify exact user-index set (no extra)."""
    actual = {
        row[1] for row in conn.execute(f"PRAGMA index_list({table})").fetchall()
        if row[3] == "c"
    }
    expected = set(expected_names)
    if actual != expected:
        raise Phase2SchemaVerificationError(
            version, "phase2 sidecar",
            f"{table} index set drift (extra={sorted(actual - expected)}, "
            f"missing={sorted(expected - actual)})",
        )


def _verify_constraint_indexes(conn, table, version):
    """Verify exact primary/unique constraint index signatures."""
    actual = []
    for row in conn.execute(f"PRAGMA index_list({table})").fetchall():
        if row[3] == "c":
            continue
        key_rows = [
            e for e in conn.execute(f"PRAGMA index_xinfo({row[1]})").fetchall()
            if e[5]
        ]
        actual.append((
            row[3], bool(row[2]), bool(row[4]),
            tuple((entry[2], bool(entry[3]), entry[4]) for entry in key_rows),
        ))
    expected = _PHASE2_CONSTRAINT_INDEX_MANIFEST[table]
    if sorted(actual, key=repr) != sorted(expected, key=repr):
        raise Phase2SchemaVerificationError(
            version, "phase2 sidecar", f"{table} constraint-index manifest drift"
        )


def _verify_fks(conn, table, expected_fks, version):
    """Verify FK tuples, actions, and count."""
    rows = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    if len(rows) != len(expected_fks):
        raise Phase2SchemaVerificationError(
            version, "phase2 sidecar",
            f"{table} FK count drift: {len(rows)} != {len(expected_fks)}",
        )
    actual_tuples = sorted(
        (row[2], row[3], row[4], row[5], row[6], row[7]) for row in rows
    )
    expected_sorted = sorted(expected_fks)
    if actual_tuples != expected_sorted:
        raise Phase2SchemaVerificationError(
            version, "phase2 sidecar",
            f"{table} FK tuples drift",
        )


def _verify_triggers(conn, version):
    """Verify exact sidecar trigger set, target tables, and SQL bodies."""
    sidecar_tables = set(_PHASE2_COLUMN_MANIFESTS)
    actual_rows = conn.execute(
        "SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger'"
    ).fetchall()
    actual = {
        row[0]: (row[1], row[2])
        for row in actual_rows
        if row[1] in sidecar_tables
    }
    expected_names = set(_PHASE2_TRIGGER_MANIFEST)
    if set(actual) != expected_names:
        raise Phase2SchemaVerificationError(
            version, "phase2 sidecar",
            "trigger set drift "
            f"(extra={sorted(set(actual) - expected_names)}, "
            f"missing={sorted(expected_names - set(actual))})",
        )
    for name, expected_sql in _PHASE2_TRIGGER_MANIFEST.items():
        table, sql = actual[name]
        if table != _PHASE2_TRIGGER_TABLES[name]:
            raise Phase2SchemaVerificationError(
                version, "phase2 sidecar", f"trigger {name} target-table drift"
            )
        actual_sql = _normalize_sql(sql)
        if actual_sql != expected_sql:
            raise Phase2SchemaVerificationError(
                version, "phase2 sidecar", f"trigger {name} SQL body drift"
            )


def _verify_immutability_runtime(conn, version):
    """Runtime-test isolated replace/UPSERT/UPDATE/DELETE guards and state."""
    marker = hashlib.sha256(
        f"p2verify:{id(conn)}:{datetime.datetime.now(datetime.timezone.utc).isoformat()}".encode()
    ).hexdigest()[:16]
    claim_id = f"_verify_{marker}"
    evidence_id = f"_verify_evidence_{marker}"
    resolution_id = f"_verify_resolution_{marker}"
    event_id = f"_verify_event_{marker}"
    savepoint = "phase2_verify_immut"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        claim_insert = (
            "INSERT INTO MemorySemanticClaims "
            "(ClaimId,Subject,Predicate,Object,Confidence,Trust,ObservedAt) "
            "VALUES (?,?,?,?,?,?,?)"
        )
        claim_params = (
            claim_id, "verify", "verify", "verify", 0.5, 0.5,
            "2000-01-01T00:00:00Z",
        )
        event_insert = (
            "INSERT INTO MemoryEvents (EventId,StreamId,StreamVersion,EventType,"
            "SchemaVersion,ActorType,ActorId,Origin,OccurredAt,CorrelationId,"
            "CausationId,SessionId,TurnId,SourceMessageId,Trust,Sensitivity,"
            "ConsentScope,Payload,PayloadHash,EventHash,IdempotencyKey) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )
        event_params = (
            event_id, f"phase2:verify:{marker}", 0, "phase2.verify", 1,
            "system", "", "test", "2000-01-01T00:00:00Z", "", "", "",
            "", "", 1.0, "normal", "local_only", "{}", marker, marker,
            f"phase2-verify:{marker}",
        )
        evidence_insert = (
            "INSERT INTO MemoryClaimEvidence "
            "(EvidenceId,ClaimId,EventId,EvidenceType,Strength) VALUES (?,?,?,?,?)"
        )
        evidence_params = (evidence_id, claim_id, event_id, "direct", 1.0)
        resolution_insert = (
            "INSERT INTO MemoryClaimResolutions "
            "(ResolutionId,ClaimId,Action,ResolvedBy) VALUES (?,?,?,?)"
        )
        resolution_params = (resolution_id, claim_id, "confirm", "user")

        claim_setup = ((claim_insert, claim_params),)
        evidence_setup = (
            (claim_insert, claim_params),
            (event_insert, event_params),
            (evidence_insert, evidence_params),
        )
        resolution_setup = (
            (claim_insert, claim_params),
            (resolution_insert, resolution_params),
        )

        probes = (
            (
                "claims_replace", claim_setup,
                "INSERT OR REPLACE INTO MemorySemanticClaims "
                "(ClaimId,Subject,Predicate,Object,Confidence,Trust,ObservedAt) "
                "VALUES (?,?,?,?,?,?,?)",
                (claim_id, "changed", "verify", "verify", 0.5, 0.5,
                 "2000-01-01T00:00:00Z"),
                "MemorySemanticClaims is immutable: replacement not allowed",
                "SELECT Subject FROM MemorySemanticClaims WHERE ClaimId=?", (claim_id,),
            ),
            (
                "claims_upsert", claim_setup,
                claim_insert + " ON CONFLICT(ClaimId) DO UPDATE SET Subject=excluded.Subject",
                (claim_id, "changed", "verify", "verify", 0.5, 0.5,
                 "2000-01-01T00:00:00Z"),
                "MemorySemanticClaims is immutable: replacement not allowed",
                "SELECT Subject FROM MemorySemanticClaims WHERE ClaimId=?", (claim_id,),
            ),
            (
                "claims_update", claim_setup,
                "UPDATE MemorySemanticClaims SET Subject='changed' WHERE ClaimId=?",
                (claim_id,), "MemorySemanticClaims is immutable: UPDATE not allowed",
                "SELECT Subject FROM MemorySemanticClaims WHERE ClaimId=?", (claim_id,),
            ),
            (
                "claims_delete", claim_setup,
                "DELETE FROM MemorySemanticClaims WHERE ClaimId=?", (claim_id,),
                "MemorySemanticClaims is immutable: DELETE not allowed",
                "SELECT Subject FROM MemorySemanticClaims WHERE ClaimId=?", (claim_id,),
            ),
            (
                "evidence_replace", evidence_setup,
                "INSERT OR REPLACE INTO MemoryClaimEvidence "
                "(EvidenceId,ClaimId,EventId,EvidenceType,Strength) VALUES (?,?,?,?,?)",
                (evidence_id, claim_id, event_id, "direct", 0.5),
                "MemoryClaimEvidence is immutable: replacement not allowed",
                "SELECT Strength FROM MemoryClaimEvidence WHERE EvidenceId=?", (evidence_id,),
            ),
            (
                "evidence_upsert", evidence_setup,
                evidence_insert
                + " ON CONFLICT(EvidenceId) DO UPDATE SET Strength=excluded.Strength",
                (evidence_id, claim_id, event_id, "direct", 0.5),
                "MemoryClaimEvidence is immutable: replacement not allowed",
                "SELECT Strength FROM MemoryClaimEvidence WHERE EvidenceId=?", (evidence_id,),
            ),
            (
                "evidence_update", evidence_setup,
                "UPDATE MemoryClaimEvidence SET Strength=0.5 WHERE EvidenceId=?",
                (evidence_id,), "MemoryClaimEvidence is immutable: UPDATE not allowed",
                "SELECT Strength FROM MemoryClaimEvidence WHERE EvidenceId=?", (evidence_id,),
            ),
            (
                "evidence_delete", evidence_setup,
                "DELETE FROM MemoryClaimEvidence WHERE EvidenceId=?", (evidence_id,),
                "MemoryClaimEvidence is immutable: DELETE not allowed",
                "SELECT Strength FROM MemoryClaimEvidence WHERE EvidenceId=?", (evidence_id,),
            ),
            (
                "resolutions_replace", resolution_setup,
                "INSERT OR REPLACE INTO MemoryClaimResolutions "
                "(ResolutionId,ClaimId,Action,Reason,ResolvedBy) VALUES (?,?,?,?,?)",
                (resolution_id, claim_id, "confirm", "changed", "user"),
                "MemoryClaimResolutions is append-only: replacement not allowed",
                "SELECT Reason FROM MemoryClaimResolutions WHERE ResolutionId=?",
                (resolution_id,),
            ),
            (
                "resolutions_upsert", resolution_setup,
                "INSERT INTO MemoryClaimResolutions "
                "(ResolutionId,ClaimId,Action,Reason,ResolvedBy) VALUES (?,?,?,?,?) "
                "ON CONFLICT(ResolutionId) DO UPDATE SET Reason=excluded.Reason",
                (resolution_id, claim_id, "confirm", "changed", "user"),
                "MemoryClaimResolutions is append-only: replacement not allowed",
                "SELECT Reason FROM MemoryClaimResolutions WHERE ResolutionId=?",
                (resolution_id,),
            ),
            (
                "resolutions_update", resolution_setup,
                "UPDATE MemoryClaimResolutions SET Reason='changed' WHERE ResolutionId=?",
                (resolution_id,),
                "MemoryClaimResolutions is append-only: UPDATE not allowed",
                "SELECT Reason FROM MemoryClaimResolutions WHERE ResolutionId=?",
                (resolution_id,),
            ),
            (
                "resolutions_delete", resolution_setup,
                "DELETE FROM MemoryClaimResolutions WHERE ResolutionId=?", (resolution_id,),
                "MemoryClaimResolutions is append-only: DELETE not allowed",
                "SELECT Reason FROM MemoryClaimResolutions WHERE ResolutionId=?",
                (resolution_id,),
            ),
        )

        for index, (
            label, setup, stmt, params, expected_error, state_sql, state_params,
        ) in enumerate(probes):
            probe_savepoint = f"phase2_verify_probe_{index}"
            conn.execute(f"SAVEPOINT {probe_savepoint}")
            try:
                for setup_sql, setup_params in setup:
                    conn.execute(setup_sql, setup_params)
                before_row = conn.execute(state_sql, state_params).fetchone()
                before = tuple(before_row) if before_row is not None else None
                try:
                    conn.execute(stmt, params)
                except sqlite3.IntegrityError as exc:
                    if str(exc) != expected_error:
                        raise Phase2SchemaVerificationError(
                            version, "phase2 sidecar",
                            f"{label} blocked by unexpected constraint: {exc}",
                        ) from exc
                else:
                    raise Phase2SchemaVerificationError(
                        version, "phase2 sidecar",
                        f"{label} immutability trigger behavior drift",
                    )
                after_row = conn.execute(state_sql, state_params).fetchone()
                after = tuple(after_row) if after_row is not None else None
                if after != before:
                    raise Phase2SchemaVerificationError(
                        version, "phase2 sidecar", f"{label} changed protected state"
                    )
            finally:
                conn.execute(f"ROLLBACK TO {probe_savepoint}")
                conn.execute(f"RELEASE {probe_savepoint}")
    finally:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")


def _verify_foreign_keys_pragma(conn, version):
    """Verify PRAGMA foreign_keys=1."""
    fk = conn.execute("PRAGMA foreign_keys").fetchone()
    if not fk or fk[0] != 1:
        raise Phase2SchemaVerificationError(
            version, "phase2 sidecar", "PRAGMA foreign_keys is not ON"
        )


def verify_phase2_schema(conn):
    """Verify Phase 2 sidecar tables with Phase1-equivalent rigor."""
    _require_supported_sqlite()
    if not _table_exists(conn, "_phase2_schema_migrations"):
        return -1
    if _normalized_sql(conn, "table", "_phase2_schema_migrations") != _META_DDL_NORMALIZED:
        raise Phase2SchemaVerificationError(
            _current_version(conn), "phase2 sidecar", "migration metadata table drift"
        )
    version = _current_version(conn)
    if version < 1:
        return version

    _verify_foreign_keys_pragma(conn, version)

    expected_tables = _PHASE2_MIGRATION_MANIFESTS[1]["tables"]
    for table in expected_tables:
        if not _table_exists(conn, table):
            raise Phase2SchemaVerificationError(
                version, "phase2 sidecar", f"{table} missing"
            )

    for table in expected_tables:
        _verify_column_manifest(conn, table, version)
        if _normalized_sql(conn, "table", table) != _PHASE2_TABLE_DDL_MANIFEST[table]:
            raise Phase2SchemaVerificationError(
                version, "phase2 sidecar", f"{table} table DDL drift"
            )

    for table, idx_names in _PHASE2_INDEX_MANIFEST.items():
        _verify_user_index_set(conn, table, idx_names, version)
        for idx_name in idx_names:
            tbl, cols, unique = _PHASE2_INDEX_SEMANTICS[idx_name]
            _verify_index(conn, tbl, idx_name, cols, unique, version)

    for table in expected_tables:
        _verify_constraint_indexes(conn, table, version)

    for table, fk_list in _PHASE2_FK_MANIFEST.items():
        _verify_fks(conn, table, fk_list, version)

    _verify_triggers(conn, version)
    _verify_immutability_runtime(conn, version)

    return version


# ── Public entry point ──────────────────────────────────────────────

def run_phase2_migrations(conn):
    """Run Phase 2 sidecar migrations. Atomic, idempotent, fatal on drift.

    Migration verification occurs INSIDE each migration savepoint before
    metadata release/commit. On failure or pre-existing incompatible objects,
    rollback leaves no v1 metadata and no newly-created sidecar artifacts.
    """
    _require_supported_sqlite()
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(_META_DDL)
    conn.commit()

    known = {v: (d, c) for v, d, c, _ in _PHASE2_MIGRATIONS}
    for version, description, checksum in conn.execute(
        "SELECT version,description,checksum FROM _phase2_schema_migrations"
    ).fetchall():
        if known.get(version) != (description, checksum):
            raise Phase2MigrationError(version, description, "migration metadata checksum drift")

    current = _current_version(conn)
    applied = 0
    for version, description, checksum, up in _PHASE2_MIGRATIONS:
        if version <= current:
            continue

        # Pre-existing incompatible objects check
        for table in _PHASE2_MIGRATION_MANIFESTS[version]["tables"]:
            if _table_exists(conn, table):
                raise Phase2MigrationError(
                    version, description,
                    f"pre-existing incompatible object: table {table} already exists",
                )

        savepoint = f"phase2_migration_{version}"
        try:
            conn.execute(f"SAVEPOINT {savepoint}")
            up(conn)
            conn.execute(
                "INSERT INTO _phase2_schema_migrations(version,description,applied_at,checksum) "
                "VALUES (?,?,?,?)",
                (
                    version, description,
                    datetime.datetime.now(datetime.timezone.utc).isoformat(
                        timespec="seconds"
                    ).replace("+00:00", "Z"),
                    checksum,
                ),
            )
            # Metadata is visible inside the savepoint, so the full verifier
            # executes before either schema or version can become durable.
            verify_phase2_schema(conn)
            conn.execute(f"RELEASE {savepoint}")
            applied += 1
            print(f"[Phase2 Migrations] Applied v{version}: {description}")
        except Exception as exc:
            try:
                conn.execute(f"ROLLBACK TO {savepoint}")
                conn.execute(f"RELEASE {savepoint}")
            except Exception:
                pass
            if isinstance(exc, Phase2MigrationError):
                raise
            raise Phase2MigrationError(version, description, str(exc)) from exc

    conn.commit()
    if current >= 0 or applied > 0:
        verify_phase2_schema(conn)
    if applied:
        print(f"[Phase2 Migrations] {applied} migration(s) applied (now at v{_current_version(conn)})")
    return applied
