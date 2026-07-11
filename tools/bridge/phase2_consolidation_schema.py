"""Exact additive schema for Phase 2 evidence-linked claim proposals.

This is migration v2 of the Phase 2 sidecar. Proposal, conflict, scan-receipt,
and decision records are immutable. Operational scan position remains in the
v1 ``MemoryConsolidationCheckpoints`` table and may be advanced atomically with
receipt/proposal inserts.
"""

import datetime
import hashlib
import json
import re
import sqlite3


_SQL_NOW_DEFAULT = "strftime('%Y-%m-%dT%H:%M:%f','now') || 'Z'"


def _normalize_sql(sql):
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


V2_DDL = [
    """CREATE TABLE MemoryClaimProposals (
    ProposalId TEXT NOT NULL PRIMARY KEY
        CHECK(typeof(ProposalId)='text' AND instr(ProposalId,char(0))=0
            AND length(ProposalId)=64 AND ProposalId NOT GLOB '*[^0-9a-f]*'),
    SourceEventId TEXT NOT NULL
        CHECK(typeof(SourceEventId)='text' AND instr(SourceEventId,char(0))=0
            AND length(SourceEventId)>0 AND length(SourceEventId)<=256),
    SourceJournalSequence INTEGER NOT NULL
        CHECK(typeof(SourceJournalSequence)='integer' AND SourceJournalSequence>0),
    SourcePayloadHash TEXT NOT NULL
        CHECK(typeof(SourcePayloadHash)='text' AND instr(SourcePayloadHash,char(0))=0
            AND length(SourcePayloadHash)=64 AND SourcePayloadHash NOT GLOB '*[^0-9a-f]*'),
    ProposalDigest TEXT NOT NULL
        CHECK(typeof(ProposalDigest)='text' AND instr(ProposalDigest,char(0))=0
            AND length(ProposalDigest)=64 AND ProposalDigest NOT GLOB '*[^0-9a-f]*'),
    ExtractorVersion TEXT NOT NULL
        CHECK(typeof(ExtractorVersion)='text' AND instr(ExtractorVersion,char(0))=0
            AND length(ExtractorVersion)>0 AND length(ExtractorVersion)<=128),
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
    Sensitivity TEXT NOT NULL
        CHECK(typeof(Sensitivity)='text' AND instr(Sensitivity,char(0))=0
            AND Sensitivity IN ('public','normal','private','secret')),
    ConsentScope TEXT NOT NULL
        CHECK(typeof(ConsentScope)='text' AND instr(ConsentScope,char(0))=0
            AND ConsentScope IN ('local_only','session','cloud_allowed','deleted')),
    ObservedAt TEXT NOT NULL
        CHECK(typeof(ObservedAt)='text' AND instr(ObservedAt,char(0))=0
            AND length(ObservedAt)>0 AND length(ObservedAt)<=64),
    Classification TEXT NOT NULL
        CHECK(typeof(Classification)='text' AND instr(Classification,char(0))=0
            AND Classification IN ('new','confirmation','contradiction','temporal_change')),
    EvidenceType TEXT NOT NULL
        CHECK(typeof(EvidenceType)='text' AND instr(EvidenceType,char(0))=0
            AND EvidenceType IN ('direct','inferred')),
    CreatedAt TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now') || 'Z')
        CHECK(typeof(CreatedAt)='text' AND instr(CreatedAt,char(0))=0
            AND length(CreatedAt)>0 AND length(CreatedAt)<=64),
    UNIQUE(SourceEventId, ExtractorVersion),
    UNIQUE(ProposalId, ProposalDigest),
    UNIQUE(ProposalId, SourceEventId, ExtractorVersion,
        SourceJournalSequence, SourcePayloadHash),
    FOREIGN KEY(SourceEventId) REFERENCES MemoryEvents(EventId)
)""",
    """CREATE TABLE MemoryClaimProposalConflicts (
    ProposalId TEXT NOT NULL
        CHECK(typeof(ProposalId)='text' AND instr(ProposalId,char(0))=0
            AND length(ProposalId)=64 AND ProposalId NOT GLOB '*[^0-9a-f]*'),
    ClaimId TEXT NOT NULL
        CHECK(typeof(ClaimId)='text' AND instr(ClaimId,char(0))=0
            AND length(ClaimId)>0 AND length(ClaimId)<=256),
    ConflictType TEXT NOT NULL
        CHECK(typeof(ConflictType)='text' AND instr(ConflictType,char(0))=0
            AND ConflictType IN ('confirmation','contradiction','temporal_change')),
    ExistingObjectHash TEXT NOT NULL
        CHECK(typeof(ExistingObjectHash)='text' AND instr(ExistingObjectHash,char(0))=0
            AND length(ExistingObjectHash)=64
            AND ExistingObjectHash NOT GLOB '*[^0-9a-f]*'),
    CreatedAt TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now') || 'Z')
        CHECK(typeof(CreatedAt)='text' AND instr(CreatedAt,char(0))=0
            AND length(CreatedAt)>0 AND length(CreatedAt)<=64),
    PRIMARY KEY(ProposalId, ClaimId),
    FOREIGN KEY(ProposalId) REFERENCES MemoryClaimProposals(ProposalId),
    FOREIGN KEY(ClaimId) REFERENCES MemorySemanticClaims(ClaimId)
)""",
    """CREATE TABLE MemoryConsolidationReceipts (
    ExtractorVersion TEXT NOT NULL
        CHECK(typeof(ExtractorVersion)='text' AND instr(ExtractorVersion,char(0))=0
            AND length(ExtractorVersion)>0 AND length(ExtractorVersion)<=128),
    SourceEventId TEXT NOT NULL
        CHECK(typeof(SourceEventId)='text' AND instr(SourceEventId,char(0))=0
            AND length(SourceEventId)>0 AND length(SourceEventId)<=256),
    SourceJournalSequence INTEGER NOT NULL
        CHECK(typeof(SourceJournalSequence)='integer' AND SourceJournalSequence>0),
    SourcePayloadHash TEXT NOT NULL
        CHECK(typeof(SourcePayloadHash)='text' AND instr(SourcePayloadHash,char(0))=0
            AND length(SourcePayloadHash)=64 AND SourcePayloadHash NOT GLOB '*[^0-9a-f]*'),
    Disposition TEXT NOT NULL
        CHECK(typeof(Disposition)='text' AND instr(Disposition,char(0))=0
            AND Disposition IN ('ignored','proposed','invalid')),
    ProposalId TEXT
        CHECK(ProposalId IS NULL OR (typeof(ProposalId)='text'
            AND instr(ProposalId,char(0))=0 AND length(ProposalId)=64
            AND ProposalId NOT GLOB '*[^0-9a-f]*')),
    ReasonCode TEXT NOT NULL
        CHECK(typeof(ReasonCode)='text' AND instr(ReasonCode,char(0))=0
            AND ReasonCode IN ('unsupported_event','proposed','invalid_payload')),
    ReceiptHash TEXT NOT NULL
        CHECK(typeof(ReceiptHash)='text' AND instr(ReceiptHash,char(0))=0
            AND length(ReceiptHash)=64 AND ReceiptHash NOT GLOB '*[^0-9a-f]*'),
    CreatedAt TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now') || 'Z')
        CHECK(typeof(CreatedAt)='text' AND instr(CreatedAt,char(0))=0
            AND length(CreatedAt)>0 AND length(CreatedAt)<=64),
    PRIMARY KEY(ExtractorVersion, SourceEventId),
    UNIQUE(ExtractorVersion, SourceJournalSequence),
    CHECK((Disposition='proposed' AND ProposalId IS NOT NULL AND ReasonCode='proposed')
        OR (Disposition='ignored' AND ProposalId IS NULL AND ReasonCode='unsupported_event')
        OR (Disposition='invalid' AND ProposalId IS NULL AND ReasonCode='invalid_payload')),
    FOREIGN KEY(SourceEventId) REFERENCES MemoryEvents(EventId),
    FOREIGN KEY(ProposalId, SourceEventId, ExtractorVersion,
        SourceJournalSequence, SourcePayloadHash)
        REFERENCES MemoryClaimProposals(ProposalId, SourceEventId, ExtractorVersion,
            SourceJournalSequence, SourcePayloadHash)
)""",
    """CREATE TABLE MemoryClaimProposalDecisions (
    DecisionId TEXT NOT NULL PRIMARY KEY
        CHECK(typeof(DecisionId)='text' AND instr(DecisionId,char(0))=0
            AND length(DecisionId)=64 AND DecisionId NOT GLOB '*[^0-9a-f]*'),
    ProposalId TEXT NOT NULL
        CHECK(typeof(ProposalId)='text' AND instr(ProposalId,char(0))=0
            AND length(ProposalId)=64 AND ProposalId NOT GLOB '*[^0-9a-f]*'),
    ProposalDigest TEXT NOT NULL
        CHECK(typeof(ProposalDigest)='text' AND instr(ProposalDigest,char(0))=0
            AND length(ProposalDigest)=64 AND ProposalDigest NOT GLOB '*[^0-9a-f]*'),
    OperationId TEXT NOT NULL
        CHECK(typeof(OperationId)='text' AND instr(OperationId,char(0))=0
            AND length(OperationId)=36 AND OperationId NOT GLOB '*[^0-9a-f-]*'
            AND substr(OperationId,9,1)='-' AND substr(OperationId,14,1)='-'
            AND substr(OperationId,19,1)='-' AND substr(OperationId,24,1)='-'
            AND length(replace(OperationId,'-',''))=32
            AND replace(OperationId,'-','') NOT GLOB '*[^0-9a-f]*'),
    CommandHash TEXT NOT NULL
        CHECK(typeof(CommandHash)='text' AND instr(CommandHash,char(0))=0
            AND length(CommandHash)=64 AND CommandHash NOT GLOB '*[^0-9a-f]*'),
    Action TEXT NOT NULL
        CHECK(typeof(Action)='text' AND instr(Action,char(0))=0
            AND Action IN ('reject','approve_new','confirm_existing',
                'keep_both','supersede_existing')),
    ClaimId TEXT,
    DecisionEventId TEXT NOT NULL
        CHECK(typeof(DecisionEventId)='text' AND instr(DecisionEventId,char(0))=0
            AND length(DecisionEventId)>0 AND length(DecisionEventId)<=256),
    Reason TEXT NOT NULL DEFAULT ''
        CHECK(typeof(Reason)='text' AND instr(Reason,char(0))=0 AND length(Reason)<=2048),
    ActorType TEXT NOT NULL
        CHECK(typeof(ActorType)='text' AND instr(ActorType,char(0))=0
            AND ActorType IN ('user','admin')),
    ActorId TEXT NOT NULL DEFAULT ''
        CHECK(typeof(ActorId)='text' AND instr(ActorId,char(0))=0 AND length(ActorId)<=512),
    DecidedAt TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now') || 'Z')
        CHECK(typeof(DecidedAt)='text' AND instr(DecidedAt,char(0))=0
            AND length(DecidedAt)>0 AND length(DecidedAt)<=64),
    UNIQUE(ProposalId),
    UNIQUE(OperationId),
    UNIQUE(DecisionEventId),
    CHECK((Action='reject' AND ClaimId IS NULL)
        OR (Action!='reject' AND typeof(ClaimId)='text'
            AND instr(ClaimId,char(0))=0 AND length(ClaimId)>0 AND length(ClaimId)<=256)),
    FOREIGN KEY(ProposalId, ProposalDigest)
        REFERENCES MemoryClaimProposals(ProposalId, ProposalDigest),
    FOREIGN KEY(ClaimId) REFERENCES MemorySemanticClaims(ClaimId),
    FOREIGN KEY(DecisionEventId) REFERENCES MemoryEvents(EventId)
)""",
]


_IMMUTABLE_TABLES = (
    ("proposals", "MemoryClaimProposals", "immutable"),
    ("proposal_conflicts", "MemoryClaimProposalConflicts", "immutable"),
    ("consolidation_receipts", "MemoryConsolidationReceipts", "immutable"),
    ("proposal_decisions", "MemoryClaimProposalDecisions", "append-only"),
)

V2_UNIQUE_KEY_GROUPS = {
    "MemoryClaimProposals": (
        ("ProposalId",),
        ("SourceEventId", "ExtractorVersion"),
    ),
    "MemoryClaimProposalConflicts": (("ProposalId", "ClaimId"),),
    "MemoryConsolidationReceipts": (
        ("ExtractorVersion", "SourceEventId"),
        ("ExtractorVersion", "SourceJournalSequence"),
    ),
    "MemoryClaimProposalDecisions": (
        ("DecisionId",), ("ProposalId",), ("OperationId",),
        ("DecisionEventId",),
    ),
}


def _replacement_guard(table):
    groups = V2_UNIQUE_KEY_GROUPS[table]
    return " OR ".join(
        "(" + " AND ".join(f"{column}=NEW.{column}" for column in group) + ")"
        for group in groups
    )


V2_TRIGGERS = []
for prefix, table, label in _IMMUTABLE_TABLES:
    V2_TRIGGERS.extend([
        (
            f"trg_{prefix}_no_replace",
            table,
            f"CREATE TRIGGER trg_{prefix}_no_replace BEFORE INSERT ON {table}\n"
            f"WHEN EXISTS(SELECT 1 FROM {table} WHERE " + _replacement_guard(table)
            + f")\nBEGIN SELECT RAISE(ABORT,'{table} is {label}: replacement not allowed'); END",
        ),
        (
            f"trg_{prefix}_no_update",
            table,
            f"CREATE TRIGGER trg_{prefix}_no_update BEFORE UPDATE ON {table}\n"
            f"BEGIN SELECT RAISE(ABORT,'{table} is {label}: UPDATE not allowed'); END",
        ),
        (
            f"trg_{prefix}_no_delete",
            table,
            f"CREATE TRIGGER trg_{prefix}_no_delete BEFORE DELETE ON {table}\n"
            f"BEGIN SELECT RAISE(ABORT,'{table} is {label}: DELETE not allowed'); END",
        ),
    ])

V2_TRIGGERS.append((
    "trg_proposal_conflicts_sealed",
    "MemoryClaimProposalConflicts",
    "CREATE TRIGGER trg_proposal_conflicts_sealed "
    "BEFORE INSERT ON MemoryClaimProposalConflicts\n"
    "WHEN EXISTS(SELECT 1 FROM MemoryConsolidationReceipts "
    "WHERE ProposalId=NEW.ProposalId AND Disposition='proposed')\n"
    "BEGIN SELECT RAISE(ABORT,'MemoryClaimProposalConflicts is sealed: "
    "membership change not allowed'); END",
))


V2_INDEXES = [
    ("idx_claim_proposals_sequence", "MemoryClaimProposals", ["SourceJournalSequence"], False),
    ("idx_claim_proposals_class", "MemoryClaimProposals", ["Classification"], False),
    ("idx_claim_proposals_observed", "MemoryClaimProposals", ["ObservedAt"], False),
    ("idx_claim_conflicts_claim", "MemoryClaimProposalConflicts", ["ClaimId"], False),
    ("idx_consolidation_receipts_sequence", "MemoryConsolidationReceipts", ["SourceJournalSequence"], False),
    ("idx_consolidation_receipts_disposition", "MemoryConsolidationReceipts", ["Disposition"], False),
    ("idx_claim_decisions_operation", "MemoryClaimProposalDecisions", ["OperationId"], False),
    ("idx_claim_decisions_decided", "MemoryClaimProposalDecisions", ["DecidedAt"], False),
]


V2_COLUMN_MANIFESTS = {
    "MemoryClaimProposals": {
        "ProposalId": ("TEXT", True, None, 1),
        "SourceEventId": ("TEXT", True, None, 0),
        "SourceJournalSequence": ("INTEGER", True, None, 0),
        "SourcePayloadHash": ("TEXT", True, None, 0),
        "ProposalDigest": ("TEXT", True, None, 0),
        "ExtractorVersion": ("TEXT", True, None, 0),
        "Subject": ("TEXT", True, None, 0),
        "Predicate": ("TEXT", True, None, 0),
        "Object": ("TEXT", True, None, 0),
        "Confidence": ("REAL", True, None, 0),
        "Trust": ("REAL", True, None, 0),
        "DecayRate": ("REAL", True, "0.01", 0),
        "Sensitivity": ("TEXT", True, None, 0),
        "ConsentScope": ("TEXT", True, None, 0),
        "ObservedAt": ("TEXT", True, None, 0),
        "Classification": ("TEXT", True, None, 0),
        "EvidenceType": ("TEXT", True, None, 0),
        "CreatedAt": ("TEXT", True, _SQL_NOW_DEFAULT, 0),
    },
    "MemoryClaimProposalConflicts": {
        "ProposalId": ("TEXT", True, None, 1),
        "ClaimId": ("TEXT", True, None, 2),
        "ConflictType": ("TEXT", True, None, 0),
        "ExistingObjectHash": ("TEXT", True, None, 0),
        "CreatedAt": ("TEXT", True, _SQL_NOW_DEFAULT, 0),
    },
    "MemoryConsolidationReceipts": {
        "ExtractorVersion": ("TEXT", True, None, 1),
        "SourceEventId": ("TEXT", True, None, 2),
        "SourceJournalSequence": ("INTEGER", True, None, 0),
        "SourcePayloadHash": ("TEXT", True, None, 0),
        "Disposition": ("TEXT", True, None, 0),
        "ProposalId": ("TEXT", False, None, 0),
        "ReasonCode": ("TEXT", True, None, 0),
        "ReceiptHash": ("TEXT", True, None, 0),
        "CreatedAt": ("TEXT", True, _SQL_NOW_DEFAULT, 0),
    },
    "MemoryClaimProposalDecisions": {
        "DecisionId": ("TEXT", True, None, 1),
        "ProposalId": ("TEXT", True, None, 0),
        "ProposalDigest": ("TEXT", True, None, 0),
        "OperationId": ("TEXT", True, None, 0),
        "CommandHash": ("TEXT", True, None, 0),
        "Action": ("TEXT", True, None, 0),
        "ClaimId": ("TEXT", False, None, 0),
        "DecisionEventId": ("TEXT", True, None, 0),
        "Reason": ("TEXT", True, "''", 0),
        "ActorType": ("TEXT", True, None, 0),
        "ActorId": ("TEXT", True, "''", 0),
        "DecidedAt": ("TEXT", True, _SQL_NOW_DEFAULT, 0),
    },
}


V2_FKS = {
    "MemoryClaimProposals": [
        ("MemoryEvents", "SourceEventId", "EventId", "NO ACTION", "NO ACTION", "NONE"),
    ],
    "MemoryClaimProposalConflicts": [
        ("MemorySemanticClaims", "ClaimId", "ClaimId", "NO ACTION", "NO ACTION", "NONE"),
        ("MemoryClaimProposals", "ProposalId", "ProposalId", "NO ACTION", "NO ACTION", "NONE"),
    ],
    "MemoryConsolidationReceipts": [
        ("MemoryClaimProposals", "ProposalId", "ProposalId", "NO ACTION", "NO ACTION", "NONE"),
        ("MemoryClaimProposals", "SourceEventId", "SourceEventId", "NO ACTION", "NO ACTION", "NONE"),
        ("MemoryClaimProposals", "ExtractorVersion", "ExtractorVersion", "NO ACTION", "NO ACTION", "NONE"),
        ("MemoryClaimProposals", "SourceJournalSequence", "SourceJournalSequence", "NO ACTION", "NO ACTION", "NONE"),
        ("MemoryClaimProposals", "SourcePayloadHash", "SourcePayloadHash", "NO ACTION", "NO ACTION", "NONE"),
        ("MemoryEvents", "SourceEventId", "EventId", "NO ACTION", "NO ACTION", "NONE"),
    ],
    "MemoryClaimProposalDecisions": [
        ("MemoryEvents", "DecisionEventId", "EventId", "NO ACTION", "NO ACTION", "NONE"),
        ("MemorySemanticClaims", "ClaimId", "ClaimId", "NO ACTION", "NO ACTION", "NONE"),
        ("MemoryClaimProposals", "ProposalId", "ProposalId", "NO ACTION", "NO ACTION", "NONE"),
        ("MemoryClaimProposals", "ProposalDigest", "ProposalDigest", "NO ACTION", "NO ACTION", "NONE"),
    ],
}


V2_CONSTRAINT_INDEXES = {
    "MemoryClaimProposals": [
        ("pk", True, False, (("ProposalId", False, "BINARY"),)),
        (
            "u", True, False,
            (("SourceEventId", False, "BINARY"), ("ExtractorVersion", False, "BINARY")),
        ),
        (
            "u", True, False,
            (("ProposalId", False, "BINARY"), ("ProposalDigest", False, "BINARY")),
        ),
        (
            "u", True, False,
            (
                ("ProposalId", False, "BINARY"),
                ("SourceEventId", False, "BINARY"),
                ("ExtractorVersion", False, "BINARY"),
                ("SourceJournalSequence", False, "BINARY"),
                ("SourcePayloadHash", False, "BINARY"),
            ),
        ),
    ],
    "MemoryClaimProposalConflicts": [
        (
            "pk", True, False,
            (("ProposalId", False, "BINARY"), ("ClaimId", False, "BINARY")),
        ),
    ],
    "MemoryConsolidationReceipts": [
        (
            "pk", True, False,
            (("ExtractorVersion", False, "BINARY"), ("SourceEventId", False, "BINARY")),
        ),
        (
            "u", True, False,
            (("ExtractorVersion", False, "BINARY"), ("SourceJournalSequence", False, "BINARY")),
        ),
    ],
    "MemoryClaimProposalDecisions": [
        ("pk", True, False, (("DecisionId", False, "BINARY"),)),
        ("u", True, False, (("ProposalId", False, "BINARY"),)),
        ("u", True, False, (("OperationId", False, "BINARY"),)),
        ("u", True, False, (("DecisionEventId", False, "BINARY"),)),
    ],
}


def _schema_manifest():
    return {
        "predecessor": "56fc41bc87a6fee931125daf0611c0264f7fb69325f6062fcba4bf7e801f4dca",
        "ddl": [re.sub(r"\s+", " ", ddl).strip() for ddl in V2_DDL],
        "triggers": [re.sub(r"\s+", " ", trigger[2]).strip() for trigger in V2_TRIGGERS],
        "indexes": [[name, table, columns, unique] for name, table, columns, unique in V2_INDEXES],
        "columns": {
            table: {column: list(values) for column, values in columns.items()}
            for table, columns in V2_COLUMN_MANIFESTS.items()
        },
        "fks": V2_FKS,
        "constraint_indexes": V2_CONSTRAINT_INDEXES,
        "contract": "immutable evidence-linked claim proposals, conflicts, scan receipts, decisions",
    }


CONSOLIDATION_SCHEMA_CHECKSUM = hashlib.sha256(
    json.dumps(_schema_manifest(), sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()


def create_consolidation_schema(conn):
    for ddl in V2_DDL:
        conn.execute(ddl)
    for _name, _table, sql in V2_TRIGGERS:
        conn.execute(sql)
    for name, table, columns, unique in V2_INDEXES:
        qualifier = "UNIQUE " if unique else ""
        conn.execute(
            f"CREATE {qualifier}INDEX {name} ON {table}({', '.join(columns)})"
        )


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
    return None if value is None else re.sub(r"\s+", "", str(value)).lower()


def _verify_columns(conn, table, version, error_type):
    rows = conn.execute(f"PRAGMA table_xinfo({table})").fetchall()
    actual = {row[1]: row for row in rows}
    expected = V2_COLUMN_MANIFESTS[table]
    if set(actual) != set(expected):
        raise error_type(version, "phase2 consolidation", f"{table} column set drift")
    for name, manifest in expected.items():
        row = actual[name]
        observed = (
            _sqlite_affinity(row[2]), bool(row[3]),
            _normalized_default(row[4]), int(row[5]),
        )
        wanted = (
            manifest[0], manifest[1], _normalized_default(manifest[2]), manifest[3],
        )
        if observed != wanted or int(row[6]) != 0:
            raise error_type(version, "phase2 consolidation", f"{table}.{name} manifest drift")


def _verify_indexes(conn, table, version, error_type):
    expected_user = {
        name: (columns, unique)
        for name, indexed_table, columns, unique in V2_INDEXES
        if indexed_table == table
    }
    rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
    actual_user = {row[1]: row for row in rows if row[3] == "c"}
    if set(actual_user) != set(expected_user):
        raise error_type(version, "phase2 consolidation", f"{table} user-index set drift")
    for name, (columns, unique) in expected_user.items():
        row = actual_user[name]
        if bool(row[2]) != unique or bool(row[4]):
            raise error_type(version, "phase2 consolidation", f"{name} semantics drift")
        key_rows = [
            entry for entry in conn.execute(f"PRAGMA index_xinfo({name})").fetchall()
            if entry[5]
        ]
        if [entry[2] for entry in key_rows] != columns or any(
            bool(entry[3]) or entry[4] != "BINARY" for entry in key_rows
        ):
            raise error_type(version, "phase2 consolidation", f"{name} key drift")

    actual_constraints = []
    for row in rows:
        if row[3] == "c":
            continue
        keys = [
            entry for entry in conn.execute(f"PRAGMA index_xinfo({row[1]})").fetchall()
            if entry[5]
        ]
        actual_constraints.append((
            row[3], bool(row[2]), bool(row[4]),
            tuple((entry[2], bool(entry[3]), entry[4]) for entry in keys),
        ))
    if sorted(actual_constraints, key=repr) != sorted(
        V2_CONSTRAINT_INDEXES[table], key=repr
    ):
        raise error_type(version, "phase2 consolidation", f"{table} constraint-index drift")


def _verify_fks(conn, table, version, error_type):
    actual = sorted(
        (row[2], row[3], row[4], row[5], row[6], row[7])
        for row in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    )
    if actual != sorted(V2_FKS[table]):
        raise error_type(version, "phase2 consolidation", f"{table} FK drift")


def _verify_triggers(conn, version, error_type):
    tables = set(V2_COLUMN_MANIFESTS)
    rows = conn.execute(
        "SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger'"
    ).fetchall()
    actual = {row[0]: (row[1], row[2]) for row in rows if row[1] in tables}
    expected = {name: (table, _normalize_sql(sql)) for name, table, sql in V2_TRIGGERS}
    if set(actual) != set(expected):
        raise error_type(version, "phase2 consolidation", "trigger set drift")
    for name, (table, sql) in actual.items():
        if table != expected[name][0] or _normalize_sql(sql) != expected[name][1]:
            raise error_type(version, "phase2 consolidation", f"trigger {name} drift")


def _verify_runtime_immutability(conn, version, error_type):
    """Prove each v2 table rejects replacement, UPDATE, and DELETE."""
    marker = hashlib.sha256(
        f"verify:{id(conn)}:{datetime.datetime.now(datetime.timezone.utc).isoformat()}".encode()
    ).hexdigest()[:16]
    event_id = f"verify-consolidation-event-{marker}"
    claim_id = f"verify-consolidation-claim-{marker}"
    proposal_id = hashlib.sha256(f"proposal:{marker}".encode()).hexdigest()
    decision_event_id = f"verify-consolidation-decision-{marker}"
    operation_id = f"00000000-0000-4000-8000-{marker[:12]}"
    verifier_extractor = f"_schema_verify_{marker}"
    digest = hashlib.sha256(marker.encode()).hexdigest()
    receipt_hash = hashlib.sha256(f"receipt:{marker}".encode()).hexdigest()

    event_insert = (
        "INSERT INTO MemoryEvents (EventId,StreamId,StreamVersion,EventType,"
        "SchemaVersion,ActorType,ActorId,Origin,OccurredAt,CorrelationId,"
        "CausationId,SessionId,TurnId,SourceMessageId,Trust,Sensitivity,"
        "ConsentScope,Payload,PayloadHash,EventHash,IdempotencyKey) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    event_params = (
        event_id, f"verify:consolidation:{marker}", 0, "phase2.verify", 1,
        "system", "", "test", "2000-01-01T00:00:00Z", "", "", "", "", "",
        1.0, "normal", "local_only", "{}", digest, digest,
        f"verify-consolidation:{marker}",
    )
    decision_event_params = (
        decision_event_id, f"verify:decision:{marker}", 0, "phase2.verify", 1,
        "system", "", "test", "2000-01-01T00:00:00Z", "", "", "", "", "",
        1.0, "normal", "local_only", "{}", digest, digest,
        f"verify-consolidation-decision:{marker}",
    )
    claim_insert = (
        "INSERT INTO MemorySemanticClaims "
        "(ClaimId,Subject,Predicate,Object,Confidence,Trust,ObservedAt) "
        "VALUES (?,?,?,?,?,?,?)"
    )
    claim_params = (
        claim_id, "verify", "verify", "existing", 0.5, 0.5, "2000-01-01T00:00:00Z",
    )
    proposal_insert = (
        "INSERT INTO MemoryClaimProposals "
        "(ProposalId,SourceEventId,SourceJournalSequence,SourcePayloadHash,"
        "ProposalDigest,ExtractorVersion,Subject,Predicate,Object,Confidence,Trust,"
        "Sensitivity,ConsentScope,ObservedAt,Classification,EvidenceType) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    proposal_params = (
        proposal_id, event_id, 1, digest, digest, verifier_extractor, "verify", "verify",
        "proposed", 0.5, 0.5, "normal", "local_only", "2000-01-01T00:00:00Z",
        "new", "direct",
    )
    conflict_insert = (
        "INSERT INTO MemoryClaimProposalConflicts "
        "(ProposalId,ClaimId,ConflictType,ExistingObjectHash) VALUES (?,?,?,?)"
    )
    conflict_params = (proposal_id, claim_id, "contradiction", digest)
    receipt_insert = (
        "INSERT INTO MemoryConsolidationReceipts "
        "(ExtractorVersion,SourceEventId,SourceJournalSequence,SourcePayloadHash,"
        "Disposition,ProposalId,ReasonCode,ReceiptHash) VALUES (?,?,?,?,?,?,?,?)"
    )
    receipt_params = (
        verifier_extractor, event_id, 1, digest, "proposed", proposal_id,
        "proposed", receipt_hash,
    )
    decision_insert = (
        "INSERT INTO MemoryClaimProposalDecisions "
        "(DecisionId,ProposalId,ProposalDigest,OperationId,CommandHash,Action,"
        "ClaimId,DecisionEventId,ActorType) VALUES (?,?,?,?,?,?,?,?,?)"
    )
    decision_params = (
        digest, proposal_id, digest, operation_id, digest, "approve_new", claim_id,
        decision_event_id, "user",
    )
    alternate_digest = hashlib.sha256(f"alternate:{marker}".encode()).hexdigest()
    alternate_proposal_id = hashlib.sha256(
        f"alternate-proposal:{marker}".encode()
    ).hexdigest()
    alternate_decision_id = hashlib.sha256(
        f"alternate-decision:{marker}".encode()
    ).hexdigest()
    alternate_event_id = f"verify-consolidation-event-alt-{marker}"
    alternate_decision_event_id = f"verify-consolidation-decision-alt-{marker}"
    alternate_operation_id = f"11111111-1111-4111-8111-{marker[:12]}"
    alternate_event_params = (
        alternate_event_id, f"verify:consolidation:alt:{marker}", 0,
        "phase2.verify", 1, "system", "", "test", "2000-01-01T00:00:00Z",
        "", "", "", "", "", 1.0, "normal", "local_only", "{}",
        alternate_digest, alternate_digest,
        f"verify-consolidation-alt:{marker}",
    )
    alternate_decision_event_params = (
        alternate_decision_event_id, f"verify:decision:alt:{marker}", 0,
        "phase2.verify", 1, "system", "", "test", "2000-01-01T00:00:00Z",
        "", "", "", "", "", 1.0, "normal", "local_only", "{}",
        alternate_digest, alternate_digest,
        f"verify-consolidation-decision-alt:{marker}",
    )
    alternate_proposal_params = (
        alternate_proposal_id, alternate_event_id, 3, alternate_digest,
        alternate_digest, verifier_extractor, "verify", "verify", "alternate",
        0.5, 0.5, "normal", "local_only", "2000-01-01T00:00:00Z",
        "new", "direct",
    )

    setups = {
        "MemoryClaimProposals": ((event_insert, event_params), (proposal_insert, proposal_params)),
        "MemoryClaimProposalConflicts": (
            (event_insert, event_params), (claim_insert, claim_params),
            (proposal_insert, proposal_params), (conflict_insert, conflict_params),
        ),
        "MemoryConsolidationReceipts": (
            (event_insert, event_params), (proposal_insert, proposal_params),
            (receipt_insert, receipt_params),
        ),
        "MemoryClaimProposalDecisions": (
            (event_insert, event_params), (event_insert, decision_event_params),
            (claim_insert, claim_params), (proposal_insert, proposal_params),
            (decision_insert, decision_params),
        ),
    }
    keys = {
        "MemoryClaimProposals": ("ProposalId=?", (proposal_id,)),
        "MemoryClaimProposalConflicts": (
            "ProposalId=? AND ClaimId=?", (proposal_id, claim_id),
        ),
        "MemoryConsolidationReceipts": (
            "ExtractorVersion=? AND SourceEventId=?", (verifier_extractor, event_id),
        ),
        "MemoryClaimProposalDecisions": ("DecisionId=?", (digest,)),
    }
    labels = {table: label for _prefix, table, label in _IMMUTABLE_TABLES}
    update_columns = {
        "MemoryClaimProposals": "ProposalDigest",
        "MemoryClaimProposalConflicts": "ConflictType",
        "MemoryConsolidationReceipts": "Disposition",
        "MemoryClaimProposalDecisions": "CommandHash",
    }
    conflict_targets = {
        "MemoryClaimProposals": "ProposalId",
        "MemoryClaimProposalConflicts": "ProposalId,ClaimId",
        "MemoryConsolidationReceipts": "ExtractorVersion,SourceEventId",
        "MemoryClaimProposalDecisions": "DecisionId",
    }
    decision_alternate_setup = (
        (event_insert, event_params),
        (event_insert, decision_event_params),
        (event_insert, alternate_event_params),
        (event_insert, alternate_decision_event_params),
        (claim_insert, claim_params),
        (proposal_insert, proposal_params),
        (proposal_insert, alternate_proposal_params),
        (decision_insert, decision_params),
    )
    alternate_unique_probes = (
        (
            "proposals_source_extractor", "MemoryClaimProposals",
            setups["MemoryClaimProposals"], proposal_insert,
            (
                alternate_proposal_id, event_id, 1, digest, alternate_digest,
                verifier_extractor, "verify", "verify", "alternate", 0.5, 0.5,
                "normal", "local_only", "2000-01-01T00:00:00Z", "new", "direct",
            ),
            "SourceEventId,ExtractorVersion",
        ),
        (
            "receipts_sequence", "MemoryConsolidationReceipts",
            setups["MemoryConsolidationReceipts"] + (
                (event_insert, alternate_event_params),
            ),
            receipt_insert,
            (
                verifier_extractor, alternate_event_id, 1, alternate_digest,
                "ignored", None, "unsupported_event", alternate_digest,
            ),
            "ExtractorVersion,SourceJournalSequence",
        ),
        (
            "decisions_proposal", "MemoryClaimProposalDecisions",
            decision_alternate_setup, decision_insert,
            (
                alternate_decision_id, proposal_id, digest,
                alternate_operation_id, alternate_digest, "approve_new", claim_id,
                alternate_decision_event_id, "user",
            ),
            "ProposalId",
        ),
        (
            "decisions_operation", "MemoryClaimProposalDecisions",
            decision_alternate_setup, decision_insert,
            (
                alternate_decision_id, alternate_proposal_id, alternate_digest,
                operation_id, alternate_digest, "approve_new", claim_id,
                alternate_decision_event_id, "user",
            ),
            "OperationId",
        ),
        (
            "decisions_event", "MemoryClaimProposalDecisions",
            decision_alternate_setup, decision_insert,
            (
                alternate_decision_id, alternate_proposal_id, alternate_digest,
                alternate_operation_id, alternate_digest, "approve_new", claim_id,
                decision_event_id, "user",
            ),
            "DecisionEventId",
        ),
    )

    outer = "verify_consolidation_immutability"
    conn.execute(f"SAVEPOINT {outer}")
    try:
        for table, setup in setups.items():
            where, params = keys[table]
            for operation in ("replace", "upsert", "update", "delete"):
                probe = f"verify_{table.lower()}_{operation}"
                conn.execute(f"SAVEPOINT {probe}")
                try:
                    for sql, values in setup:
                        conn.execute(sql, values)
                    before_row = conn.execute(
                        f"SELECT * FROM {table} WHERE {where}", params
                    ).fetchone()
                    before = tuple(before_row) if before_row is not None else None
                    if operation == "replace":
                        insert_sql, insert_params = setup[-1]
                        statement = insert_sql.replace("INSERT INTO", "INSERT OR REPLACE INTO", 1)
                        values = insert_params
                        expected = f"{table} is {labels[table]}: replacement not allowed"
                    elif operation == "upsert":
                        insert_sql, insert_params = setup[-1]
                        column = update_columns[table]
                        statement = (
                            insert_sql + f" ON CONFLICT({conflict_targets[table]}) "
                            f"DO UPDATE SET {column}=excluded.{column}"
                        )
                        values = insert_params
                        expected = f"{table} is {labels[table]}: replacement not allowed"
                    elif operation == "update":
                        column = update_columns[table]
                        statement = f"UPDATE {table} SET {column}={column} WHERE {where}"
                        values = params
                        expected = f"{table} is {labels[table]}: UPDATE not allowed"
                    else:
                        statement = f"DELETE FROM {table} WHERE {where}"
                        values = params
                        expected = f"{table} is {labels[table]}: DELETE not allowed"
                    try:
                        conn.execute(statement, values)
                    except sqlite3.IntegrityError as exc:
                        if str(exc) != expected:
                            raise error_type(
                                version, "phase2 consolidation",
                                f"{table} {operation} blocked unexpectedly: {exc}",
                            ) from exc
                    else:
                        raise error_type(
                            version, "phase2 consolidation",
                            f"{table} {operation} immutability drift",
                        )
                    after_row = conn.execute(
                        f"SELECT * FROM {table} WHERE {where}", params
                    ).fetchone()
                    after = tuple(after_row) if after_row is not None else None
                    if after != before:
                        raise error_type(
                            version, "phase2 consolidation",
                            f"{table} {operation} changed protected state",
                        )
                finally:
                    conn.execute(f"ROLLBACK TO {probe}")
                    conn.execute(f"RELEASE {probe}")
        for label, table, setup, insert_sql, insert_params, target in alternate_unique_probes:
            for operation in ("replace", "upsert"):
                probe = f"verify_alt_{label}_{operation}"
                conn.execute(f"SAVEPOINT {probe}")
                try:
                    for sql, values in setup:
                        conn.execute(sql, values)
                    before = conn.execute(
                        f"SELECT * FROM {table} ORDER BY rowid"
                    ).fetchall()
                    if operation == "replace":
                        statement = insert_sql.replace(
                            "INSERT INTO", "INSERT OR REPLACE INTO", 1
                        )
                    else:
                        column = update_columns[table]
                        statement = (
                            insert_sql + f" ON CONFLICT({target}) DO UPDATE "
                            f"SET {column}=excluded.{column}"
                        )
                    expected = (
                        f"{table} is {labels[table]}: replacement not allowed"
                    )
                    try:
                        conn.execute(statement, insert_params)
                    except sqlite3.IntegrityError as exc:
                        if str(exc) != expected:
                            raise error_type(
                                version, "phase2 consolidation",
                                f"{label} {operation} blocked unexpectedly: {exc}",
                            ) from exc
                    else:
                        raise error_type(
                            version, "phase2 consolidation",
                            f"{label} {operation} uniqueness guard drift",
                        )
                    after = conn.execute(
                        f"SELECT * FROM {table} ORDER BY rowid"
                    ).fetchall()
                    if after != before:
                        raise error_type(
                            version, "phase2 consolidation",
                            f"{label} {operation} changed protected state",
                        )
                finally:
                    conn.execute(f"ROLLBACK TO {probe}")
                    conn.execute(f"RELEASE {probe}")
        seal_probe = "verify_proposal_conflict_membership_seal"
        conn.execute(f"SAVEPOINT {seal_probe}")
        try:
            for sql, values in (
                (event_insert, event_params),
                (claim_insert, claim_params),
                (proposal_insert, proposal_params),
                (receipt_insert, receipt_params),
            ):
                conn.execute(sql, values)
            try:
                conn.execute(conflict_insert, conflict_params)
            except sqlite3.IntegrityError as exc:
                expected = (
                    "MemoryClaimProposalConflicts is sealed: "
                    "membership change not allowed"
                )
                if str(exc) != expected:
                    raise error_type(
                        version, "phase2 consolidation",
                        f"conflict membership seal blocked unexpectedly: {exc}",
                    ) from exc
            else:
                raise error_type(
                    version, "phase2 consolidation",
                    "conflict membership seal behavior drift",
                )
            count = conn.execute(
                "SELECT COUNT(*) FROM MemoryClaimProposalConflicts "
                "WHERE ProposalId=?", (proposal_id,),
            ).fetchone()[0]
            if count != 0:
                raise error_type(
                    version, "phase2 consolidation",
                    "conflict membership changed after sealing",
                )
        finally:
            conn.execute(f"ROLLBACK TO {seal_probe}")
            conn.execute(f"RELEASE {seal_probe}")
    finally:
        conn.execute(f"ROLLBACK TO {outer}")
        conn.execute(f"RELEASE {outer}")


def verify_consolidation_schema(conn, version, error_type):
    for table in V2_COLUMN_MANIFESTS:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if row is None:
            raise error_type(version, "phase2 consolidation", f"{table} missing")
        expected_ddl = next(
            ddl for ddl in V2_DDL if re.match(
                rf"CREATE TABLE\s+{re.escape(table)}\b", ddl, re.IGNORECASE
            )
        )
        if _normalize_sql(row[0]) != _normalize_sql(expected_ddl):
            raise error_type(version, "phase2 consolidation", f"{table} DDL drift")
        _verify_columns(conn, table, version, error_type)
        _verify_indexes(conn, table, version, error_type)
        _verify_fks(conn, table, version, error_type)
    _verify_triggers(conn, version, error_type)
    _verify_runtime_immutability(conn, version, error_type)
    return version
