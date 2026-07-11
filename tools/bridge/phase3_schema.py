"""Phase 3 safe-learning sidecar schema and exact migration verification.

The schema is always additive and dormant by default. It stores immutable
execution outcomes, restricted learning candidates, evidence links, deterministic
evaluation plans, and deterministic evaluation results. It contains no active
version pointer and cannot modify legacy ``Skills``.
"""

import datetime
import hashlib
import json
import re
import sqlite3


class Phase3MigrationError(Exception):
    def __init__(self, version, description, cause):
        self.version = version
        self.description = description
        self.cause = cause
        super().__init__(f"Phase3 migration v{version} ({description}) failed: {cause}")


class Phase3SchemaVerificationError(Phase3MigrationError):
    def __init__(self, version, detail):
        super().__init__(version, "safe learning sidecar", f"schema verification: {detail}")


def _normalize_sql(sql):
    if not isinstance(sql, str):
        return ""
    output = []
    quote = None
    index = 0
    while index < len(sql):
        char = sql[index]
        if quote is not None:
            output.append(char)
            if quote == "[":
                if char == "]":
                    quote = None
            elif char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 1
                    output.append(sql[index])
                else:
                    quote = None
        elif char in ("'", '"', "`"):
            quote = char
            output.append(char)
        elif char == "[":
            quote = char
            output.append(char)
        elif not char.isspace():
            output.append(char.lower())
        index += 1
    return "".join(output)


_META_DDL = """CREATE TABLE IF NOT EXISTS _phase3_schema_migrations (
    Version INTEGER NOT NULL PRIMARY KEY
        CHECK(typeof(Version)='integer' AND Version>0),
    Description TEXT NOT NULL
        CHECK(typeof(Description)='text' AND instr(Description,char(0))=0
            AND length(Description)>0 AND length(Description)<=256),
    AppliedAt TEXT NOT NULL
        CHECK(typeof(AppliedAt)='text' AND instr(AppliedAt,char(0))=0
            AND length(AppliedAt)>0 AND length(AppliedAt)<=64),
    Checksum TEXT NOT NULL
        CHECK(typeof(Checksum)='text' AND instr(Checksum,char(0))=0
            AND length(Checksum)=64 AND Checksum NOT GLOB '*[^0-9a-f]*')
)"""
_META_NORMALIZED = _normalize_sql(_META_DDL.replace("IF NOT EXISTS ", ""))
_SQL_NOW_DEFAULT = "strftime('%Y-%m-%dT%H:%M:%f','now') || 'Z'"


V1_DDL = [
    """CREATE TABLE LearningExecutionReports (
    ReportId TEXT NOT NULL PRIMARY KEY
        CHECK(typeof(ReportId)='text' AND instr(ReportId,char(0))=0
            AND length(ReportId)=64 AND ReportId NOT GLOB '*[^0-9a-f]*'),
    OperationId TEXT NOT NULL
        CHECK(typeof(OperationId)='text' AND instr(OperationId,char(0))=0
            AND length(OperationId)=36 AND OperationId NOT GLOB '*[^0-9a-f-]*'
            AND substr(OperationId,9,1)='-' AND substr(OperationId,14,1)='-'
            AND substr(OperationId,19,1)='-' AND substr(OperationId,24,1)='-'
            AND length(replace(OperationId,'-',''))=32
            AND replace(OperationId,'-','') NOT GLOB '*[^0-9a-f]*'),
    ActionRunId TEXT NOT NULL
        CHECK(typeof(ActionRunId)='text' AND instr(ActionRunId,char(0))=0
            AND length(ActionRunId)>0 AND length(ActionRunId)<=256),
    TurnId TEXT NOT NULL DEFAULT ''
        CHECK(typeof(TurnId)='text' AND instr(TurnId,char(0))=0 AND length(TurnId)<=256),
    SkillId TEXT NOT NULL
        CHECK(typeof(SkillId)='text' AND instr(SkillId,char(0))=0
            AND length(SkillId)>0 AND length(SkillId)<=128),
    SkillVersionHash TEXT NOT NULL
        CHECK(typeof(SkillVersionHash)='text' AND instr(SkillVersionHash,char(0))=0
            AND length(SkillVersionHash)=64 AND SkillVersionHash NOT GLOB '*[^0-9a-f]*'),
    Outcome TEXT NOT NULL
        CHECK(typeof(Outcome)='text' AND instr(Outcome,char(0))=0
            AND Outcome IN ('succeeded','failed','aborted')),
    Postcondition TEXT NOT NULL
        CHECK(typeof(Postcondition)='text' AND instr(Postcondition,char(0))=0
            AND Postcondition IN ('observed','not_observed','not_applicable')),
    VerificationSource TEXT NOT NULL
        CHECK(typeof(VerificationSource)='text' AND instr(VerificationSource,char(0))=0
            AND VerificationSource IN ('user','tool','system','test')),
    DurationMs INTEGER NOT NULL
        CHECK(typeof(DurationMs)='integer' AND DurationMs>=0 AND DurationMs<=3600000),
    EvidenceHash TEXT NOT NULL
        CHECK(typeof(EvidenceHash)='text' AND instr(EvidenceHash,char(0))=0
            AND length(EvidenceHash)=64 AND EvidenceHash NOT GLOB '*[^0-9a-f]*'),
    CommandHash TEXT NOT NULL
        CHECK(typeof(CommandHash)='text' AND instr(CommandHash,char(0))=0
            AND length(CommandHash)=64 AND CommandHash NOT GLOB '*[^0-9a-f]*'),
    EventId TEXT NOT NULL UNIQUE
        CHECK(typeof(EventId)='text' AND instr(EventId,char(0))=0
            AND length(EventId)>0 AND length(EventId)<=256),
    ReportedAt TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now') || 'Z')
        CHECK(typeof(ReportedAt)='text' AND instr(ReportedAt,char(0))=0
            AND length(ReportedAt)>0 AND length(ReportedAt)<=64),
    UNIQUE(OperationId),
    UNIQUE(ActionRunId, SkillVersionHash),
    FOREIGN KEY(EventId) REFERENCES MemoryEvents(EventId)
) WITHOUT ROWID""",
    """CREATE TABLE LearningCandidates (
    CandidateId TEXT NOT NULL PRIMARY KEY
        CHECK(typeof(CandidateId)='text' AND instr(CandidateId,char(0))=0
            AND length(CandidateId)=64 AND CandidateId NOT GLOB '*[^0-9a-f]*'),
    OperationId TEXT NOT NULL
        CHECK(typeof(OperationId)='text' AND instr(OperationId,char(0))=0
            AND length(OperationId)=36 AND OperationId NOT GLOB '*[^0-9a-f-]*'
            AND substr(OperationId,9,1)='-' AND substr(OperationId,14,1)='-'
            AND substr(OperationId,19,1)='-' AND substr(OperationId,24,1)='-'
            AND length(replace(OperationId,'-',''))=32
            AND replace(OperationId,'-','') NOT GLOB '*[^0-9a-f]*'),
    Kind TEXT NOT NULL
        CHECK(typeof(Kind)='text' AND instr(Kind,char(0))=0
            AND Kind IN ('skill_instructions','skill_prompt_template','skill_routing_rule')),
    TargetSkillId TEXT NOT NULL
        CHECK(typeof(TargetSkillId)='text' AND instr(TargetSkillId,char(0))=0
            AND length(TargetSkillId)>0 AND length(TargetSkillId)<=128),
    BaseVersionHash TEXT NOT NULL
        CHECK(typeof(BaseVersionHash)='text' AND instr(BaseVersionHash,char(0))=0
            AND length(BaseVersionHash)=64 AND BaseVersionHash NOT GLOB '*[^0-9a-f]*'),
    CandidateVersionHash TEXT NOT NULL
        CHECK(typeof(CandidateVersionHash)='text' AND instr(CandidateVersionHash,char(0))=0
            AND length(CandidateVersionHash)=64
            AND CandidateVersionHash NOT GLOB '*[^0-9a-f]*'),
    CandidatePayload TEXT NOT NULL
        CHECK(typeof(CandidatePayload)='text' AND instr(CandidatePayload,char(0))=0
            AND length(CandidatePayload)>1 AND length(CandidatePayload)<=32768),
    PayloadHash TEXT NOT NULL
        CHECK(typeof(PayloadHash)='text' AND instr(PayloadHash,char(0))=0
            AND length(PayloadHash)=64 AND PayloadHash NOT GLOB '*[^0-9a-f]*'),
    CandidateHash TEXT NOT NULL
        CHECK(typeof(CandidateHash)='text' AND instr(CandidateHash,char(0))=0
            AND length(CandidateHash)=64 AND CandidateHash NOT GLOB '*[^0-9a-f]*'),
    ProposedBy TEXT NOT NULL
        CHECK(typeof(ProposedBy)='text' AND instr(ProposedBy,char(0))=0
            AND ProposedBy IN ('user','system','admin','test')),
    ActorId TEXT NOT NULL DEFAULT ''
        CHECK(typeof(ActorId)='text' AND instr(ActorId,char(0))=0 AND length(ActorId)<=512),
    CommandHash TEXT NOT NULL
        CHECK(typeof(CommandHash)='text' AND instr(CommandHash,char(0))=0
            AND length(CommandHash)=64 AND CommandHash NOT GLOB '*[^0-9a-f]*'),
    EventId TEXT NOT NULL UNIQUE
        CHECK(typeof(EventId)='text' AND instr(EventId,char(0))=0
            AND length(EventId)>0 AND length(EventId)<=256),
    CreatedAt TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now') || 'Z')
        CHECK(typeof(CreatedAt)='text' AND instr(CreatedAt,char(0))=0
            AND length(CreatedAt)>0 AND length(CreatedAt)<=64),
    UNIQUE(OperationId),
    UNIQUE(TargetSkillId, CandidateVersionHash),
    FOREIGN KEY(EventId) REFERENCES MemoryEvents(EventId)
) WITHOUT ROWID""",
    """CREATE TABLE LearningCandidateEvidence (
    CandidateId TEXT NOT NULL
        CHECK(typeof(CandidateId)='text' AND instr(CandidateId,char(0))=0
            AND length(CandidateId)=64 AND CandidateId NOT GLOB '*[^0-9a-f]*'),
    ReportId TEXT NOT NULL
        CHECK(typeof(ReportId)='text' AND instr(ReportId,char(0))=0
            AND length(ReportId)=64 AND ReportId NOT GLOB '*[^0-9a-f]*'),
    EvidenceRole TEXT NOT NULL
        CHECK(typeof(EvidenceRole)='text' AND instr(EvidenceRole,char(0))=0
            AND EvidenceRole IN ('support','failure')),
    LinkedAt TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now') || 'Z')
        CHECK(typeof(LinkedAt)='text' AND instr(LinkedAt,char(0))=0
            AND length(LinkedAt)>0 AND length(LinkedAt)<=64),
    PRIMARY KEY(CandidateId, ReportId),
    FOREIGN KEY(CandidateId) REFERENCES LearningCandidates(CandidateId),
    FOREIGN KEY(ReportId) REFERENCES LearningExecutionReports(ReportId)
) WITHOUT ROWID""",
    """CREATE TABLE LearningEvaluationPlans (
    PlanId TEXT NOT NULL PRIMARY KEY
        CHECK(typeof(PlanId)='text' AND instr(PlanId,char(0))=0
            AND length(PlanId)=64 AND PlanId NOT GLOB '*[^0-9a-f]*'),
    OperationId TEXT NOT NULL
        CHECK(typeof(OperationId)='text' AND instr(OperationId,char(0))=0
            AND length(OperationId)=36 AND OperationId NOT GLOB '*[^0-9a-f-]*'
            AND substr(OperationId,9,1)='-' AND substr(OperationId,14,1)='-'
            AND substr(OperationId,19,1)='-' AND substr(OperationId,24,1)='-'
            AND length(replace(OperationId,'-',''))=32
            AND replace(OperationId,'-','') NOT GLOB '*[^0-9a-f]*'),
    CandidateId TEXT NOT NULL
        CHECK(typeof(CandidateId)='text' AND instr(CandidateId,char(0))=0
            AND length(CandidateId)=64 AND CandidateId NOT GLOB '*[^0-9a-f]*'),
    EvaluatorId TEXT NOT NULL
        CHECK(typeof(EvaluatorId)='text' AND instr(EvaluatorId,char(0))=0
            AND length(EvaluatorId)>0 AND length(EvaluatorId)<=128),
    EvaluatorVersion TEXT NOT NULL
        CHECK(typeof(EvaluatorVersion)='text' AND instr(EvaluatorVersion,char(0))=0
            AND length(EvaluatorVersion)>0 AND length(EvaluatorVersion)<=128),
    FixtureSetHash TEXT NOT NULL
        CHECK(typeof(FixtureSetHash)='text' AND instr(FixtureSetHash,char(0))=0
            AND length(FixtureSetHash)=64 AND FixtureSetHash NOT GLOB '*[^0-9a-f]*'),
    BaselineVersionHash TEXT NOT NULL
        CHECK(typeof(BaselineVersionHash)='text' AND instr(BaselineVersionHash,char(0))=0
            AND length(BaselineVersionHash)=64
            AND BaselineVersionHash NOT GLOB '*[^0-9a-f]*'),
    CandidateVersionHash TEXT NOT NULL
        CHECK(typeof(CandidateVersionHash)='text' AND instr(CandidateVersionHash,char(0))=0
            AND length(CandidateVersionHash)=64
            AND CandidateVersionHash NOT GLOB '*[^0-9a-f]*'),
    PlanHash TEXT NOT NULL
        CHECK(typeof(PlanHash)='text' AND instr(PlanHash,char(0))=0
            AND length(PlanHash)=64 AND PlanHash NOT GLOB '*[^0-9a-f]*'),
    CommandHash TEXT NOT NULL
        CHECK(typeof(CommandHash)='text' AND instr(CommandHash,char(0))=0
            AND length(CommandHash)=64 AND CommandHash NOT GLOB '*[^0-9a-f]*'),
    EventId TEXT NOT NULL UNIQUE
        CHECK(typeof(EventId)='text' AND instr(EventId,char(0))=0
            AND length(EventId)>0 AND length(EventId)<=256),
    CreatedAt TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now') || 'Z')
        CHECK(typeof(CreatedAt)='text' AND instr(CreatedAt,char(0))=0
            AND length(CreatedAt)>0 AND length(CreatedAt)<=64),
    UNIQUE(OperationId),
    UNIQUE(CandidateId, EvaluatorId, EvaluatorVersion, FixtureSetHash),
    FOREIGN KEY(CandidateId) REFERENCES LearningCandidates(CandidateId),
    FOREIGN KEY(EventId) REFERENCES MemoryEvents(EventId)
) WITHOUT ROWID""",
    """CREATE TABLE LearningEvaluationResults (
    ResultId TEXT NOT NULL PRIMARY KEY
        CHECK(typeof(ResultId)='text' AND instr(ResultId,char(0))=0
            AND length(ResultId)=64 AND ResultId NOT GLOB '*[^0-9a-f]*'),
    PlanId TEXT NOT NULL UNIQUE
        CHECK(typeof(PlanId)='text' AND instr(PlanId,char(0))=0
            AND length(PlanId)=64 AND PlanId NOT GLOB '*[^0-9a-f]*'),
    BaselinePassed INTEGER NOT NULL
        CHECK(typeof(BaselinePassed)='integer' AND BaselinePassed>=0 AND BaselinePassed<=1000),
    BaselineTotal INTEGER NOT NULL
        CHECK(typeof(BaselineTotal)='integer' AND BaselineTotal>=0 AND BaselineTotal<=1000),
    CandidatePassed INTEGER NOT NULL
        CHECK(typeof(CandidatePassed)='integer' AND CandidatePassed>=0 AND CandidatePassed<=1000),
    CandidateTotal INTEGER NOT NULL
        CHECK(typeof(CandidateTotal)='integer' AND CandidateTotal>=0 AND CandidateTotal<=1000),
    SafetyPassed INTEGER NOT NULL
        CHECK(typeof(SafetyPassed)='integer' AND SafetyPassed IN (0,1)),
    RegressionCount INTEGER NOT NULL
        CHECK(typeof(RegressionCount)='integer' AND RegressionCount>=0
            AND RegressionCount<=1000),
    Passed INTEGER NOT NULL
        CHECK(typeof(Passed)='integer' AND Passed IN (0,1)),
    ResultHash TEXT NOT NULL
        CHECK(typeof(ResultHash)='text' AND instr(ResultHash,char(0))=0
            AND length(ResultHash)=64 AND ResultHash NOT GLOB '*[^0-9a-f]*'),
    EventId TEXT NOT NULL UNIQUE
        CHECK(typeof(EventId)='text' AND instr(EventId,char(0))=0
            AND length(EventId)>0 AND length(EventId)<=256),
    EvaluatedAt TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now') || 'Z')
        CHECK(typeof(EvaluatedAt)='text' AND instr(EvaluatedAt,char(0))=0
            AND length(EvaluatedAt)>0 AND length(EvaluatedAt)<=64),
    CHECK(BaselinePassed<=BaselineTotal),
    CHECK(CandidatePassed<=CandidateTotal),
    CHECK(Passed=0 OR (SafetyPassed=1 AND RegressionCount=0
        AND CandidatePassed>=BaselinePassed)),
    FOREIGN KEY(PlanId) REFERENCES LearningEvaluationPlans(PlanId),
    FOREIGN KEY(EventId) REFERENCES MemoryEvents(EventId)
) WITHOUT ROWID""",
]


_TABLES = (
    "LearningExecutionReports", "LearningCandidates", "LearningCandidateEvidence",
    "LearningEvaluationPlans", "LearningEvaluationResults",
)
_UNIQUE_GROUPS = {
    "LearningExecutionReports": (
        ("ReportId",), ("OperationId",), ("EventId",),
        ("ActionRunId", "SkillVersionHash"),
    ),
    "LearningCandidates": (
        ("CandidateId",), ("OperationId",), ("EventId",),
        ("TargetSkillId", "CandidateVersionHash"),
    ),
    "LearningCandidateEvidence": (("CandidateId", "ReportId"),),
    "LearningEvaluationPlans": (
        ("PlanId",), ("OperationId",), ("EventId",),
        ("CandidateId", "EvaluatorId", "EvaluatorVersion", "FixtureSetHash"),
    ),
    "LearningEvaluationResults": (("ResultId",), ("PlanId",), ("EventId",)),
}


def _replacement_guard(table):
    return " OR ".join(
        "(" + " AND ".join(f"{column}=NEW.{column}" for column in group) + ")"
        for group in _UNIQUE_GROUPS[table]
    )


V1_TRIGGERS = []
for table in _TABLES:
    prefix = re.sub(r"(?<!^)(?=[A-Z])", "_", table).lower()
    V1_TRIGGERS.extend((
        (
            f"trg_{prefix}_no_replace", table,
            f"CREATE TRIGGER trg_{prefix}_no_replace BEFORE INSERT ON {table}\n"
            f"WHEN EXISTS(SELECT 1 FROM {table} WHERE {_replacement_guard(table)})\n"
            f"BEGIN SELECT RAISE(ABORT,'{table} is immutable: replacement not allowed'); END",
        ),
        (
            f"trg_{prefix}_no_update", table,
            f"CREATE TRIGGER trg_{prefix}_no_update BEFORE UPDATE ON {table}\n"
            f"BEGIN SELECT RAISE(ABORT,'{table} is immutable: UPDATE not allowed'); END",
        ),
        (
            f"trg_{prefix}_no_delete", table,
            f"CREATE TRIGGER trg_{prefix}_no_delete BEFORE DELETE ON {table}\n"
            f"BEGIN SELECT RAISE(ABORT,'{table} is immutable: DELETE not allowed'); END",
        ),
    ))


V1_INDEXES = [
    ("idx_learning_reports_skill", "LearningExecutionReports", ["SkillId", "ReportedAt"], False),
    ("idx_learning_reports_outcome", "LearningExecutionReports", ["Outcome"], False),
    ("idx_learning_candidates_target", "LearningCandidates", ["TargetSkillId", "CreatedAt"], False),
    ("idx_learning_candidates_kind", "LearningCandidates", ["Kind"], False),
    ("idx_learning_evidence_report", "LearningCandidateEvidence", ["ReportId"], False),
    ("idx_learning_plans_candidate", "LearningEvaluationPlans", ["CandidateId"], False),
    ("idx_learning_results_passed", "LearningEvaluationResults", ["Passed", "EvaluatedAt"], False),
]


COLUMN_MANIFESTS = {
    "LearningExecutionReports": {
        "ReportId": ("TEXT", True, None, 1), "OperationId": ("TEXT", True, None, 0),
        "ActionRunId": ("TEXT", True, None, 0), "TurnId": ("TEXT", True, "''", 0),
        "SkillId": ("TEXT", True, None, 0), "SkillVersionHash": ("TEXT", True, None, 0),
        "Outcome": ("TEXT", True, None, 0), "Postcondition": ("TEXT", True, None, 0),
        "VerificationSource": ("TEXT", True, None, 0), "DurationMs": ("INTEGER", True, None, 0),
        "EvidenceHash": ("TEXT", True, None, 0), "CommandHash": ("TEXT", True, None, 0),
        "EventId": ("TEXT", True, None, 0), "ReportedAt": ("TEXT", True, _SQL_NOW_DEFAULT, 0),
    },
    "LearningCandidates": {
        "CandidateId": ("TEXT", True, None, 1), "OperationId": ("TEXT", True, None, 0),
        "Kind": ("TEXT", True, None, 0), "TargetSkillId": ("TEXT", True, None, 0),
        "BaseVersionHash": ("TEXT", True, None, 0),
        "CandidateVersionHash": ("TEXT", True, None, 0),
        "CandidatePayload": ("TEXT", True, None, 0), "PayloadHash": ("TEXT", True, None, 0),
        "CandidateHash": ("TEXT", True, None, 0), "ProposedBy": ("TEXT", True, None, 0),
        "ActorId": ("TEXT", True, "''", 0), "CommandHash": ("TEXT", True, None, 0),
        "EventId": ("TEXT", True, None, 0), "CreatedAt": ("TEXT", True, _SQL_NOW_DEFAULT, 0),
    },
    "LearningCandidateEvidence": {
        "CandidateId": ("TEXT", True, None, 1), "ReportId": ("TEXT", True, None, 2),
        "EvidenceRole": ("TEXT", True, None, 0), "LinkedAt": ("TEXT", True, _SQL_NOW_DEFAULT, 0),
    },
    "LearningEvaluationPlans": {
        "PlanId": ("TEXT", True, None, 1), "OperationId": ("TEXT", True, None, 0),
        "CandidateId": ("TEXT", True, None, 0), "EvaluatorId": ("TEXT", True, None, 0),
        "EvaluatorVersion": ("TEXT", True, None, 0), "FixtureSetHash": ("TEXT", True, None, 0),
        "BaselineVersionHash": ("TEXT", True, None, 0),
        "CandidateVersionHash": ("TEXT", True, None, 0),
        "PlanHash": ("TEXT", True, None, 0), "CommandHash": ("TEXT", True, None, 0),
        "EventId": ("TEXT", True, None, 0), "CreatedAt": ("TEXT", True, _SQL_NOW_DEFAULT, 0),
    },
    "LearningEvaluationResults": {
        "ResultId": ("TEXT", True, None, 1), "PlanId": ("TEXT", True, None, 0),
        "BaselinePassed": ("INTEGER", True, None, 0), "BaselineTotal": ("INTEGER", True, None, 0),
        "CandidatePassed": ("INTEGER", True, None, 0), "CandidateTotal": ("INTEGER", True, None, 0),
        "SafetyPassed": ("INTEGER", True, None, 0), "RegressionCount": ("INTEGER", True, None, 0),
        "Passed": ("INTEGER", True, None, 0), "ResultHash": ("TEXT", True, None, 0),
        "EventId": ("TEXT", True, None, 0), "EvaluatedAt": ("TEXT", True, _SQL_NOW_DEFAULT, 0),
    },
}


FK_MANIFESTS = {
    "LearningExecutionReports": [
        ("MemoryEvents", "EventId", "EventId", "NO ACTION", "NO ACTION", "NONE"),
    ],
    "LearningCandidates": [
        ("MemoryEvents", "EventId", "EventId", "NO ACTION", "NO ACTION", "NONE"),
    ],
    "LearningCandidateEvidence": [
        ("LearningExecutionReports", "ReportId", "ReportId", "NO ACTION", "NO ACTION", "NONE"),
        ("LearningCandidates", "CandidateId", "CandidateId", "NO ACTION", "NO ACTION", "NONE"),
    ],
    "LearningEvaluationPlans": [
        ("MemoryEvents", "EventId", "EventId", "NO ACTION", "NO ACTION", "NONE"),
        ("LearningCandidates", "CandidateId", "CandidateId", "NO ACTION", "NO ACTION", "NONE"),
    ],
    "LearningEvaluationResults": [
        ("MemoryEvents", "EventId", "EventId", "NO ACTION", "NO ACTION", "NONE"),
        ("LearningEvaluationPlans", "PlanId", "PlanId", "NO ACTION", "NO ACTION", "NONE"),
    ],
}


TABLE_DDL = {
    re.match(r"CREATE TABLE\s+(\w+)", ddl, re.IGNORECASE).group(1): _normalize_sql(ddl)
    for ddl in V1_DDL
}
TRIGGER_MANIFEST = {name: (table, _normalize_sql(sql)) for name, table, sql in V1_TRIGGERS}
INDEX_MANIFEST = {name: (table, columns, unique) for name, table, columns, unique in V1_INDEXES}


def _schema_payload():
    return {
        "ddl": [re.sub(r"\s+", " ", ddl).strip() for ddl in V1_DDL],
        "triggers": [re.sub(r"\s+", " ", item[2]).strip() for item in V1_TRIGGERS],
        "indexes": [[name, table, columns, unique] for name, table, columns, unique in V1_INDEXES],
        "columns": {table: {name: list(value) for name, value in cols.items()}
                    for table, cols in COLUMN_MANIFESTS.items()},
        "fks": FK_MANIFESTS,
        "unique_groups": _UNIQUE_GROUPS,
        "contract": "immutable shadow learning reports, candidates, evidence, plans, results",
    }


SCHEMA_CHECKSUM = hashlib.sha256(
    json.dumps(_schema_payload(), sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()

# Iteration-1 v1 was created briefly before WITHOUT ROWID hardening. Keep its
# exact checksum/DDL so local development databases upgrade rather than fail.
_LEGACY_V1_SCHEMA_PAYLOAD = _schema_payload()
_LEGACY_V1_SCHEMA_PAYLOAD["ddl"] = [
    re.sub(r"\s+WITHOUT ROWID$", "", ddl, flags=re.IGNORECASE)
    for ddl in _LEGACY_V1_SCHEMA_PAYLOAD["ddl"]
]
LEGACY_V1_CHECKSUM = hashlib.sha256(
    json.dumps(
        _LEGACY_V1_SCHEMA_PAYLOAD, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
).hexdigest()
LEGACY_V1_TABLE_DDL = {
    table: re.sub(r"withoutrowid$", "", ddl)
    for table, ddl in TABLE_DDL.items()
}
V2_CHECKSUM = SCHEMA_CHECKSUM
MIGRATION_HISTORY = {
    1: ("safe learning v1", (LEGACY_V1_CHECKSUM, SCHEMA_CHECKSUM)),
    2: ("safe learning v2 without rowid", (V2_CHECKSUM,)),
}


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def current_phase3_version(conn):
    if not _table_exists(conn, "_phase3_schema_migrations"):
        return -1
    row = conn.execute("SELECT MAX(Version) FROM _phase3_schema_migrations").fetchone()
    return row[0] if row and row[0] is not None else -1


def _affinity(value):
    declared = str(value or "").upper()
    if "INT" in declared:
        return "INTEGER"
    if any(token in declared for token in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if not declared or "BLOB" in declared:
        return "BLOB"
    if any(token in declared for token in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def _default(value):
    return None if value is None else re.sub(r"\s+", "", str(value)).lower()


def _verify_columns(conn, table, version):
    rows = conn.execute(f"PRAGMA table_xinfo({table})").fetchall()
    actual = {row[1]: row for row in rows}
    expected = COLUMN_MANIFESTS[table]
    if set(actual) != set(expected):
        raise Phase3SchemaVerificationError(version, f"{table} column set drift")
    for name, wanted in expected.items():
        row = actual[name]
        observed = (_affinity(row[2]), bool(row[3]), _default(row[4]), int(row[5]))
        target = (wanted[0], wanted[1], _default(wanted[2]), wanted[3])
        if observed != target or int(row[6]) != 0:
            raise Phase3SchemaVerificationError(version, f"{table}.{name} column drift")


def _constraint_signatures(conn, table):
    signatures = []
    for row in conn.execute(f"PRAGMA index_list({table})").fetchall():
        if row[3] == "c":
            continue
        keys = [entry for entry in conn.execute(
            f"PRAGMA index_xinfo({row[1]})"
        ).fetchall() if entry[5]]
        signatures.append((
            row[3], bool(row[2]), bool(row[4]),
            tuple((entry[2], bool(entry[3]), entry[4]) for entry in keys),
        ))
    return sorted(signatures, key=repr)


def _expected_constraint_signatures(table):
    groups = []
    for index, columns in enumerate(_UNIQUE_GROUPS[table]):
        origin = "pk" if index == 0 else "u"
        groups.append((
            origin, True, False,
            tuple((column, False, "BINARY") for column in columns),
        ))
    return sorted(groups, key=repr)


def _verify_indexes(conn, table, version):
    rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
    expected = {
        name: (columns, unique)
        for name, (indexed_table, columns, unique) in INDEX_MANIFEST.items()
        if indexed_table == table
    }
    actual = {row[1]: row for row in rows if row[3] == "c"}
    if set(actual) != set(expected):
        raise Phase3SchemaVerificationError(version, f"{table} user-index drift")
    for name, (columns, unique) in expected.items():
        row = actual[name]
        keys = [entry for entry in conn.execute(
            f"PRAGMA index_xinfo({name})"
        ).fetchall() if entry[5]]
        if (
            bool(row[2]) != unique or bool(row[4])
            or [entry[2] for entry in keys] != columns
            or any(bool(entry[3]) or entry[4] != "BINARY" for entry in keys)
        ):
            raise Phase3SchemaVerificationError(version, f"{name} index semantics drift")
    if _constraint_signatures(conn, table) != _expected_constraint_signatures(table):
        raise Phase3SchemaVerificationError(version, f"{table} constraint-index drift")


def _verify_fks(conn, table, version):
    actual = sorted(
        (row[2], row[3], row[4], row[5], row[6], row[7])
        for row in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    )
    if actual != sorted(FK_MANIFESTS[table]):
        raise Phase3SchemaVerificationError(version, f"{table} foreign-key drift")


def _verify_triggers(conn, version):
    rows = conn.execute(
        "SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger'"
    ).fetchall()
    actual = {row[0]: (row[1], _normalize_sql(row[2])) for row in rows if row[1] in _TABLES}
    if actual != TRIGGER_MANIFEST:
        raise Phase3SchemaVerificationError(version, "trigger manifest drift")


def _verify_relational_integrity(conn, version):
    missing_or_excess = conn.execute(
        "SELECT c.CandidateId FROM LearningCandidates c "
        "LEFT JOIN LearningCandidateEvidence e ON e.CandidateId=c.CandidateId "
        "GROUP BY c.CandidateId HAVING COUNT(e.ReportId)<1 OR COUNT(e.ReportId)>100 "
        "LIMIT 1"
    ).fetchone()
    if missing_or_excess is not None:
        raise Phase3SchemaVerificationError(
            version, "candidate evidence cardinality drift"
        )
    semantic_drift = conn.execute(
        "SELECT 1 FROM LearningCandidateEvidence e "
        "JOIN LearningCandidates c ON c.CandidateId=e.CandidateId "
        "JOIN LearningExecutionReports r ON r.ReportId=e.ReportId "
        "WHERE r.SkillId<>c.TargetSkillId "
        "OR r.SkillVersionHash<>c.BaseVersionHash "
        "OR (e.EvidenceRole='support')<>(r.Outcome='succeeded' "
        "AND r.Postcondition='observed' AND r.VerificationSource IN ('user','test')) "
        "LIMIT 1"
    ).fetchone()
    if semantic_drift is not None:
        raise Phase3SchemaVerificationError(
            version, "candidate evidence semantic drift"
        )


def _create_v1(conn):
    for ddl in V1_DDL:
        conn.execute(ddl)
    for _name, _table, sql in V1_TRIGGERS:
        conn.execute(sql)
    for name, table, columns, unique in V1_INDEXES:
        qualifier = "UNIQUE " if unique else ""
        conn.execute(f"CREATE {qualifier}INDEX {name} ON {table}({', '.join(columns)})")


def _verify_runtime_immutability(conn, version):
    marker = hashlib.sha256(
        f"phase3-verify:{id(conn)}:{datetime.datetime.now(datetime.timezone.utc).isoformat()}".encode()
    ).hexdigest()[:16]
    digest = hashlib.sha256(marker.encode()).hexdigest()
    operation = f"00000000-0000-4000-8000-{marker[:12]}"
    report_id = hashlib.sha256(f"report:{marker}".encode()).hexdigest()
    candidate_id = hashlib.sha256(f"candidate:{marker}".encode()).hexdigest()
    plan_id = hashlib.sha256(f"plan:{marker}".encode()).hexdigest()
    result_id = hashlib.sha256(f"result:{marker}".encode()).hexdigest()

    event_sql = (
        "INSERT INTO MemoryEvents (EventId,StreamId,StreamVersion,EventType,"
        "SchemaVersion,ActorType,ActorId,Origin,OccurredAt,CorrelationId,"
        "CausationId,SessionId,TurnId,SourceMessageId,Trust,Sensitivity,"
        "ConsentScope,Payload,PayloadHash,EventHash,IdempotencyKey) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    events = []
    for index, label in enumerate(("report", "candidate", "plan", "result")):
        event_id = f"phase3-verify-{label}-{marker}"
        events.append(event_id)
        events_row = (
            event_id, f"phase3:verify:{label}:{marker}", 0,
            f"learning.{label}_verified", 1, "system", "", "test",
            "2000-01-01T00:00:00Z", "", "", "", "", "", 1.0,
            "normal", "local_only", "{}", digest, digest,
            f"phase3-verify:{label}:{marker}:{index}",
        )
        events[-1] = (event_id, events_row)

    report_sql = (
        "INSERT INTO LearningExecutionReports "
        "(ReportId,OperationId,ActionRunId,SkillId,SkillVersionHash,Outcome,"
        "Postcondition,VerificationSource,DurationMs,EvidenceHash,CommandHash,EventId) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    report_values = (
        report_id, operation, f"run-{marker}", f"skill-{marker}", digest,
        "succeeded", "observed", "test", 10, digest, digest, events[0][0],
    )
    candidate_sql = (
        "INSERT INTO LearningCandidates "
        "(CandidateId,OperationId,Kind,TargetSkillId,BaseVersionHash,"
        "CandidateVersionHash,CandidatePayload,PayloadHash,CandidateHash,"
        "ProposedBy,CommandHash,EventId) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    candidate_values = (
        candidate_id, f"11111111-1111-4111-8111-{marker[:12]}",
        "skill_instructions", f"skill-{marker}", digest, digest,
        '{"instructions":"verify"}', digest, digest, "test", digest,
        events[1][0],
    )
    evidence_sql = (
        "INSERT INTO LearningCandidateEvidence "
        "(CandidateId,ReportId,EvidenceRole) VALUES (?,?,?)"
    )
    evidence_values = (candidate_id, report_id, "support")
    plan_sql = (
        "INSERT INTO LearningEvaluationPlans "
        "(PlanId,OperationId,CandidateId,EvaluatorId,EvaluatorVersion,FixtureSetHash,"
        "BaselineVersionHash,CandidateVersionHash,PlanHash,CommandHash,EventId) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)"
    )
    plan_values = (
        plan_id, f"22222222-2222-4222-8222-{marker[:12]}", candidate_id,
        "deterministic-local", "v1", digest, digest, digest, digest, digest,
        events[2][0],
    )
    result_sql = (
        "INSERT INTO LearningEvaluationResults "
        "(ResultId,PlanId,BaselinePassed,BaselineTotal,CandidatePassed,"
        "CandidateTotal,SafetyPassed,RegressionCount,Passed,ResultHash,EventId) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)"
    )
    result_values = (
        result_id, plan_id, 1, 1, 1, 1, 1, 0, 1, digest, events[3][0],
    )
    setup = tuple((event_sql, row) for _event_id, row in events) + (
        (report_sql, report_values), (candidate_sql, candidate_values),
        (evidence_sql, evidence_values), (plan_sql, plan_values),
        (result_sql, result_values),
    )
    rows = {
        "LearningExecutionReports": (report_sql, report_values, "ReportId=?", (report_id,), "Outcome"),
        "LearningCandidates": (candidate_sql, candidate_values, "CandidateId=?", (candidate_id,), "Kind"),
        "LearningCandidateEvidence": (
            evidence_sql, evidence_values,
            "CandidateId=? AND ReportId=?", (candidate_id, report_id), "EvidenceRole",
        ),
        "LearningEvaluationPlans": (plan_sql, plan_values, "PlanId=?", (plan_id,), "EvaluatorId"),
        "LearningEvaluationResults": (result_sql, result_values, "ResultId=?", (result_id,), "Passed"),
    }
    outer = "phase3_verify_immutability"
    conn.execute(f"SAVEPOINT {outer}")
    try:
        for table, (insert_sql, insert_values, where, where_values, update_column) in rows.items():
            for operation_name in ("replace", "upsert", "update", "delete"):
                probe = f"phase3_verify_{table.lower()}_{operation_name}"
                conn.execute(f"SAVEPOINT {probe}")
                try:
                    for sql, values in setup:
                        conn.execute(sql, values)
                    before = conn.execute(
                        f"SELECT * FROM {table} WHERE {where}", where_values
                    ).fetchone()
                    if operation_name == "replace":
                        statement = insert_sql.replace("INSERT INTO", "INSERT OR REPLACE INTO", 1)
                        values = insert_values
                        expected = f"{table} is immutable: replacement not allowed"
                    elif operation_name == "upsert":
                        target = ",".join(_UNIQUE_GROUPS[table][0])
                        statement = (
                            insert_sql + f" ON CONFLICT({target}) DO UPDATE "
                            f"SET {update_column}=excluded.{update_column}"
                        )
                        values = insert_values
                        expected = f"{table} is immutable: replacement not allowed"
                    elif operation_name == "update":
                        statement = (
                            f"UPDATE {table} SET {update_column}={update_column} "
                            f"WHERE {where}"
                        )
                        values = where_values
                        expected = f"{table} is immutable: UPDATE not allowed"
                    else:
                        statement = f"DELETE FROM {table} WHERE {where}"
                        values = where_values
                        expected = f"{table} is immutable: DELETE not allowed"
                    try:
                        conn.execute(statement, values)
                    except sqlite3.IntegrityError as exc:
                        if str(exc) != expected:
                            raise Phase3SchemaVerificationError(
                                version,
                                f"{table} {operation_name} blocked unexpectedly: {exc}",
                            ) from exc
                    else:
                        raise Phase3SchemaVerificationError(
                            version, f"{table} {operation_name} immutability drift"
                        )
                    after = conn.execute(
                        f"SELECT * FROM {table} WHERE {where}", where_values
                    ).fetchone()
                    if tuple(after) != tuple(before):
                        raise Phase3SchemaVerificationError(
                            version, f"{table} {operation_name} changed protected state"
                        )
                finally:
                    conn.execute(f"ROLLBACK TO {probe}")
                    conn.execute(f"RELEASE {probe}")
    finally:
        conn.execute(f"ROLLBACK TO {outer}")
        conn.execute(f"RELEASE {outer}")


def verify_phase3_schema(conn, *, allow_legacy_v1=False):
    if not _table_exists(conn, "_phase3_schema_migrations"):
        return -1
    meta = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='_phase3_schema_migrations'"
    ).fetchone()
    if not meta or _normalize_sql(meta[0]) != _META_NORMALIZED:
        raise Phase3SchemaVerificationError(current_phase3_version(conn), "metadata table drift")
    version = current_phase3_version(conn)
    if version < 1:
        return version
    rows = conn.execute(
        "SELECT Version,Description,Checksum FROM _phase3_schema_migrations "
        "ORDER BY Version"
    ).fetchall()
    if [row[0] for row in rows] != list(range(1, version + 1)):
        raise Phase3SchemaVerificationError(version, "migration history is not contiguous")
    for stored_version, description, checksum in rows:
        expected = MIGRATION_HISTORY.get(stored_version)
        if (
            expected is None
            or description != expected[0]
            or checksum not in expected[1]
        ):
            raise Phase3SchemaVerificationError(
                version, f"migration v{stored_version} metadata drift"
            )
    v1_checksum = rows[0][2]
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise Phase3SchemaVerificationError(version, "foreign_keys is not enabled")
    for table in _TABLES:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        observed_ddl = _normalize_sql(row[0]) if row is not None else ""
        accepted_ddl = {TABLE_DDL[table]}
        if allow_legacy_v1 and version == 1:
            accepted_ddl = {
                LEGACY_V1_TABLE_DDL[table]
                if v1_checksum == LEGACY_V1_CHECKSUM else TABLE_DDL[table]
            }
        if observed_ddl not in accepted_ddl:
            raise Phase3SchemaVerificationError(version, f"{table} DDL drift")
        _verify_columns(conn, table, version)
        _verify_indexes(conn, table, version)
        _verify_fks(conn, table, version)
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise Phase3SchemaVerificationError(
            version, f"foreign-key violations detected ({len(violations)})"
        )
    _verify_relational_integrity(conn, version)
    _verify_triggers(conn, version)
    _verify_runtime_immutability(conn, version)
    return version


def run_phase3_migrations(conn):
    if conn.in_transaction:
        raise Phase3MigrationError(
            current_phase3_version(conn), "transaction ownership",
            "migration requires an idle connection",
        )
    conn.execute("PRAGMA foreign_keys=ON")
    applied = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        metadata_exists = _table_exists(conn, "_phase3_schema_migrations")
        if not metadata_exists:
            conn.execute(_META_DDL)
        rows = conn.execute(
            "SELECT Version,Description,Checksum FROM _phase3_schema_migrations"
        ).fetchall()
        for version, description, checksum in rows:
            expected = MIGRATION_HISTORY.get(version)
            if (
                expected is None
                or description != expected[0]
                or checksum not in expected[1]
            ):
                raise Phase3MigrationError(
                    version, description, "migration metadata drift"
                )
        current = current_phase3_version(conn)
        if current < 1:
            for table in _TABLES:
                if _table_exists(conn, table):
                    raise Phase3MigrationError(
                        1, "safe learning v1", f"pre-existing table {table}"
                    )
            _create_v1(conn)
            conn.execute(
                "INSERT INTO _phase3_schema_migrations "
                "(Version,Description,AppliedAt,Checksum) VALUES (?,?,?,?)",
                (
                    1, "safe learning v1",
                    datetime.datetime.now(datetime.timezone.utc).isoformat(
                        timespec="seconds"
                    ).replace("+00:00", "Z"),
                    SCHEMA_CHECKSUM,
                ),
            )
            current = 1
            applied += 1
        if current == 1:
            verify_phase3_schema(conn, allow_legacy_v1=True)
            rowid_backed = []
            for table in _TABLES:
                ddl = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if ddl and _normalize_sql(ddl[0]) == LEGACY_V1_TABLE_DDL[table]:
                    rowid_backed.append(table)
            if rowid_backed and len(rowid_backed) != len(_TABLES):
                raise Phase3MigrationError(2, "safe learning v2", "mixed v1/v2 table layout")
            if rowid_backed:
                for _name, table, _sql in V1_TRIGGERS:
                    conn.execute(f"DROP TRIGGER IF EXISTS trg_{re.sub(r'(?<!^)(?=[A-Z])', '_', table).lower()}_no_replace")
                    conn.execute(f"DROP TRIGGER IF EXISTS trg_{re.sub(r'(?<!^)(?=[A-Z])', '_', table).lower()}_no_update")
                    conn.execute(f"DROP TRIGGER IF EXISTS trg_{re.sub(r'(?<!^)(?=[A-Z])', '_', table).lower()}_no_delete")
                for name, _table, _columns, _unique in V1_INDEXES:
                    conn.execute(f"DROP INDEX IF EXISTS {name}")
                conn.execute("PRAGMA defer_foreign_keys=ON")
                for table, ddl in zip(_TABLES, V1_DDL):
                    old = f"{table}_phase3_v1"
                    conn.execute(f"ALTER TABLE {table} RENAME TO {old}")
                    conn.execute(ddl)
                    columns = ",".join(COLUMN_MANIFESTS[table])
                    conn.execute(
                        f"INSERT INTO {table} ({columns}) SELECT {columns} FROM {old}"
                    )
                    conn.execute(f"DROP TABLE {old}")
                for _name, _table, sql in V1_TRIGGERS:
                    conn.execute(sql)
                for name, table, columns, unique in V1_INDEXES:
                    qualifier = "UNIQUE " if unique else ""
                    conn.execute(
                        f"CREATE {qualifier}INDEX {name} ON {table}({', '.join(columns)})"
                    )
            conn.execute(
                "INSERT INTO _phase3_schema_migrations "
                "(Version,Description,AppliedAt,Checksum) VALUES (?,?,?,?)",
                (
                    2, "safe learning v2 without rowid",
                    datetime.datetime.now(datetime.timezone.utc).isoformat(
                        timespec="seconds"
                    ).replace("+00:00", "Z"), V2_CHECKSUM,
                ),
            )
            applied += 1
        verify_phase3_schema(conn)
        conn.commit()
    except Exception as exc:
        if conn.in_transaction:
            conn.rollback()
        if isinstance(exc, Phase3MigrationError):
            raise
        raise Phase3MigrationError(2, "safe learning v2", str(exc)) from exc
    verify_phase3_schema(conn)
    if applied:
        print(f"[Phase3 Migrations] {applied} migration(s) applied (now at v2)")
    return applied
