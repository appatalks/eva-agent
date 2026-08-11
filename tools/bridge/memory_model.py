"""Structured, attributable memory and governed autonomy for SQLite Eva stores."""

import datetime
import hashlib
import json
import re
import threading
import uuid


ATOM_KINDS = {"fact", "preference", "constraint", "decision", "identity_claim", "candidate"}
ATOM_TRUST = {"unconfirmed", "user_confirmed", "operator_approved", "system_observed"}
ATOM_STATUS = {"active", "superseded", "rejected", "expired", "deleted"}
ATOM_SCOPES = {"user", "session", "project", "global", "eva_identity"}
TRAIT_STATUS = {"candidate", "approved", "disabled", "deleted", "expired"}
SKILL_RISKS = {"low", "review", "restricted"}
LOW_RISK_TOOLS = {"", "data-retrieval", "web-search"}
RESTRICTED_TOOL_TERMS = {"browser", "desktop", "signal", "email", "message", "payment", "purchase", "credential", "protected", "delete", "write"}
_KUSTO_TURN_LOCK = threading.RLock()


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _revision_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _clip(value, limit):
    return " ".join(str(value or "").split())[:limit]


def _identifier(prefix):
    return prefix + "-" + uuid.uuid4().hex[:16]


def _scenario_id(scope, scope_id):
    digest = hashlib.sha256((scope + "\0" + scope_id).encode("utf-8")).hexdigest()[:20]
    return "scenario-" + digest


def _legacy_kind(entity, relation):
    relation = (relation or "").lower()
    if (entity or "").lower() == "eva":
        return "identity_claim"
    if "prefer" in relation or "style" in relation or "motto" in relation:
        return "preference"
    if "decision" in relation:
        return "decision"
    if "constraint" in relation or "must" in relation:
        return "constraint"
    return "fact"


def classify_skill_risk(tools):
    values = {item.strip().lower() for item in str(tools or "").split(",") if item.strip()}
    if not values or values <= LOW_RISK_TOOLS:
        return "low"
    if any(term in value for value in values for term in RESTRICTED_TOOL_TERMS):
        return "restricted"
    return "review"


class MemoryModel:
    """Own structured-memory mutations; recall remains safely prompt-framed elsewhere."""

    def __init__(self, memory):
        self.memory = memory

    def migrate_legacy_knowledge(self):
        """Copy legacy Knowledge exactly once into unconfirmed attributed atoms."""
        if not self.memory.table_exists("MemoryMigrations"):
            return 0
        if self.memory.query("SELECT MigrationId FROM MemoryMigrations WHERE MigrationId = ?", ("legacy-knowledge-atoms-v1",)):
            return 0
        rows = self.memory.query(
            "SELECT rowid AS LegacyId, Timestamp, Entity, Relation, Value, Confidence, Source FROM Knowledge"
        ) or []
        now = _now()

        def write(conn):
            inserted = 0
            for row in rows:
                legacy_id = str(row.get("LegacyId", ""))
                source_ref = "legacy-knowledge:" + legacy_id
                if conn.execute("SELECT 1 FROM MemoryAtoms WHERE SourceRef = ? LIMIT 1", (source_ref,)).fetchone():
                    continue
                entity = _clip(row.get("Entity"), 120)
                relation = _clip(row.get("Relation"), 120)
                kind = _legacy_kind(entity, relation)
                if kind == "identity_claim":
                    continue
                scope = "user" if entity.lower() == "user" else "global"
                confidence = max(0.0, min(float(row.get("Confidence", 0.5) or 0.5), 1.0))
                memory_id = "legacy-atom-" + legacy_id
                conn.execute(
                    "INSERT INTO MemoryAtoms (MemoryId, Entity, Relation, Value, Kind, Trust, Status, Scope, ScopeId, Confidence, SourceRef, CreatedAt, UpdatedAt) VALUES (?, ?, ?, ?, ?, 'unconfirmed', 'active', ?, '', ?, ?, ?, ?)",
                    (memory_id, entity, relation, _clip(row.get("Value"), 4000), kind, scope, confidence, source_ref,
                     _clip(row.get("Timestamp"), 40) or now, now),
                )
                conn.execute(
                    "INSERT INTO MemoryEvidence (EvidenceId, MemoryId, SourceType, SourceRef, CreatedAt) VALUES (?, ?, 'legacy_knowledge', ?, ?)",
                    (_identifier("evidence"), memory_id, source_ref, now),
                )
                inserted += 1
            conn.execute(
                "INSERT OR IGNORE INTO MemoryMigrations (MigrationId, AppliedAt, Details) VALUES ('legacy-knowledge-atoms-v1', ?, ?)",
                (now, "Copied legacy Knowledge as unconfirmed attributed atoms"),
            )
            return inserted

        return self.memory.transaction(write)

    def active_charter(self, fallback):
        rows = self.memory.query(
            "SELECT Content FROM CoreIdentity WHERE Status = 'approved' ORDER BY Version DESC LIMIT 1"
        ) or []
        return _clip(rows[0].get("Content"), 6000) if rows else fallback

    def ensure_scenario(self, scope, scope_id, title=""):
        scope = str(scope or "session").lower()
        if scope not in {"session", "project"}:
            raise ValueError("scenario scope must be session or project")
        scope_id = _clip(scope_id, 160)
        if not scope_id:
            raise ValueError("scenario scope_id is required")
        scenario_id = _scenario_id(scope, scope_id)
        now = _revision_now()

        def write(conn):
            row = conn.execute("SELECT * FROM MemoryScenarios WHERE ScenarioId = ?", (scenario_id,)).fetchone()
            if row:
                conn.execute("UPDATE MemoryScenarios SET UpdatedAt = ? WHERE ScenarioId = ?", (now, scenario_id))
                return dict(conn.execute("SELECT * FROM MemoryScenarios WHERE ScenarioId = ?", (scenario_id,)).fetchone())
            conn.execute(
                "INSERT INTO MemoryScenarios (ScenarioId, Scope, ScopeId, Title, Summary, Status, CreatedAt, UpdatedAt) VALUES (?, ?, ?, ?, '', 'active', ?, ?)",
                (scenario_id, scope, scope_id, _clip(title, 160), now, now),
            )
            return dict(conn.execute("SELECT * FROM MemoryScenarios WHERE ScenarioId = ?", (scenario_id,)).fetchone())

        return self.memory.transaction(write)

    def add_atom(self, record, evidence=None):
        """Add an attributed atom. Caller chooses trust only from the fixed vocabulary."""
        if not isinstance(record, dict):
            raise ValueError("memory atom must be an object")
        kind = str(record.get("kind", "fact")).lower()
        trust = str(record.get("trust", "unconfirmed")).lower()
        status = str(record.get("status", "active")).lower()
        scope = str(record.get("scope", "user")).lower()
        if kind not in ATOM_KINDS or trust not in ATOM_TRUST or status not in ATOM_STATUS or scope not in ATOM_SCOPES:
            raise ValueError("memory atom has unsupported lifecycle fields")
        value = _clip(record.get("value"), 4000)
        if not value:
            raise ValueError("memory atom value is required")
        confidence = float(record.get("confidence", 0.5))
        if not 0 <= confidence <= 1:
            raise ValueError("memory atom confidence must be between 0 and 1")
        memory_id = _identifier("memory")
        now = _revision_now()
        source_ref = _clip(record.get("source_ref"), 240)
        row = {
            "MemoryId": memory_id, "Entity": _clip(record.get("entity"), 120), "Relation": _clip(record.get("relation"), 120),
            "Value": value, "Kind": kind, "Trust": trust, "Status": status, "Scope": scope,
            "ScopeId": _clip(record.get("scope_id"), 160), "Confidence": confidence, "SourceRef": source_ref,
            "CreatedAt": now, "UpdatedAt": now, "ExpiresAt": _clip(record.get("expires_at"), 40), "SupersedesId": _clip(record.get("supersedes_id"), 80),
        }

        def write(conn):
            conn.execute(
                "INSERT INTO MemoryAtoms (MemoryId, Entity, Relation, Value, Kind, Trust, Status, Scope, ScopeId, Confidence, SourceRef, CreatedAt, UpdatedAt, ExpiresAt, SupersedesId) VALUES (:MemoryId, :Entity, :Relation, :Value, :Kind, :Trust, :Status, :Scope, :ScopeId, :Confidence, :SourceRef, :CreatedAt, :UpdatedAt, :ExpiresAt, :SupersedesId)",
                row,
            )
            for item in evidence or []:
                if not isinstance(item, dict):
                    continue
                source_type = _clip(item.get("source_type"), 80)
                evidence_ref = _clip(item.get("source_ref"), 240)
                if source_type and evidence_ref:
                    conn.execute(
                        "INSERT INTO MemoryEvidence (EvidenceId, MemoryId, SourceType, SourceRef, CreatedAt) VALUES (?, ?, ?, ?, ?)",
                        (_identifier("evidence"), memory_id, source_type, evidence_ref, now),
                    )
            return row

        return self.memory.transaction(write)

    def supersede_atom(self, memory_id, replacement):
        replacement = dict(replacement or {})

        def write(conn):
            source = conn.execute("SELECT * FROM MemoryAtoms WHERE MemoryId = ?", (str(memory_id),)).fetchone()
            if not source or source["Status"] != "active":
                raise ValueError("active memory atom not found")
            replacement.setdefault("entity", source["Entity"])
            replacement.setdefault("relation", source["Relation"])
            replacement.setdefault("kind", source["Kind"])
            replacement.setdefault("trust", "user_confirmed")
            replacement.setdefault("scope", source["Scope"])
            replacement.setdefault("scope_id", source["ScopeId"])
            replacement.setdefault("confidence", 1.0)
            kind = str(replacement.get("kind", "fact")).lower()
            trust = str(replacement.get("trust", "unconfirmed")).lower()
            scope = str(replacement.get("scope", "user")).lower()
            value = _clip(replacement.get("value"), 4000)
            confidence = float(replacement.get("confidence", 0.5))
            if kind not in ATOM_KINDS or trust not in ATOM_TRUST or scope not in ATOM_SCOPES or not value or not 0 <= confidence <= 1:
                raise ValueError("memory correction has unsupported lifecycle fields")
            now = _now()
            row = {
                "MemoryId": _identifier("memory"), "Entity": _clip(replacement.get("entity"), 120),
                "Relation": _clip(replacement.get("relation"), 120), "Value": value, "Kind": kind,
                "Trust": trust, "Status": "active", "Scope": scope, "ScopeId": _clip(replacement.get("scope_id"), 160),
                "Confidence": confidence, "SourceRef": _clip(replacement.get("source_ref"), 240), "CreatedAt": now,
                "UpdatedAt": now, "ExpiresAt": _clip(replacement.get("expires_at"), 40), "SupersedesId": str(memory_id),
            }
            conn.execute(
                "INSERT INTO MemoryAtoms (MemoryId, Entity, Relation, Value, Kind, Trust, Status, Scope, ScopeId, Confidence, SourceRef, CreatedAt, UpdatedAt, ExpiresAt, SupersedesId) VALUES (:MemoryId, :Entity, :Relation, :Value, :Kind, :Trust, :Status, :Scope, :ScopeId, :Confidence, :SourceRef, :CreatedAt, :UpdatedAt, :ExpiresAt, :SupersedesId)",
                row,
            )
            conn.execute("UPDATE MemoryAtoms SET Status = 'superseded', UpdatedAt = ? WHERE MemoryId = ?", (now, str(memory_id)))
            conn.execute("UPDATE UserPersonaTraits SET Status = 'disabled', UpdatedAt = ? WHERE SourceMemoryIds LIKE ?", (now, "%" + str(memory_id) + "%"))
            return row

        return self.memory.transaction(write)

    def delete_atom(self, memory_id):
        """Remove an atom from future recall without erasing its audit record."""
        now = _now()

        def write(conn):
            row = conn.execute("SELECT Status FROM MemoryAtoms WHERE MemoryId = ?", (str(memory_id),)).fetchone()
            if not row or row["Status"] in {"deleted", "superseded"}:
                return False
            conn.execute("UPDATE MemoryAtoms SET Status = 'deleted', UpdatedAt = ? WHERE MemoryId = ?", (now, str(memory_id)))
            conn.execute("UPDATE UserPersonaTraits SET Status = 'disabled', UpdatedAt = ? WHERE SourceMemoryIds LIKE ?", (now, "%" + str(memory_id) + "%"))
            return True

        return self.memory.transaction(write)

    def add_scenario_member(self, scenario_id, memory_id, role="context"):
        role = str(role or "context").lower()
        if role not in {"context", "decision", "constraint", "open_question"}:
            raise ValueError("unsupported scenario member role")
        self.memory.transaction(lambda conn: conn.execute(
            "INSERT OR IGNORE INTO ScenarioMembers (ScenarioId, MemoryId, Role) VALUES (?, ?, ?)",
            (str(scenario_id), str(memory_id), role),
        ))

    def derive_trait(self, trait, value, source_memory_ids, scope="user", scope_id=""):
        source_memory_ids = [str(item) for item in source_memory_ids or [] if str(item)]
        if not source_memory_ids:
            raise ValueError("persona trait requires source memory")
        placeholders = ",".join("?" for _ in source_memory_ids)
        sources = self.memory.query(
            "SELECT MemoryId, Trust, Status FROM MemoryAtoms WHERE MemoryId IN (" + placeholders + ")", tuple(source_memory_ids)
        ) or []
        approved = {row.get("MemoryId") for row in sources if row.get("Status") == "active" and row.get("Trust") in {"user_confirmed", "operator_approved"}}
        if approved != set(source_memory_ids):
            raise ValueError("persona traits require active confirmed or approved source atoms")
        trait_id = _identifier("trait")
        now = _now()
        row = {
            "TraitId": trait_id, "Trait": _clip(trait, 80), "Value": _clip(value, 240), "Confidence": 1.0,
            "SourceMemoryIds": json.dumps(source_memory_ids, separators=(",", ":")), "Status": "approved",
            "Scope": str(scope or "user")[:40], "ScopeId": _clip(scope_id, 160), "CreatedAt": now, "UpdatedAt": now,
        }
        if not row["Trait"] or not row["Value"]:
            raise ValueError("trait and value are required")
        self.memory.transaction(lambda conn: conn.execute(
            "INSERT INTO UserPersonaTraits (TraitId, Trait, Value, Confidence, SourceMemoryIds, Status, Scope, ScopeId, CreatedAt, UpdatedAt) VALUES (:TraitId, :Trait, :Value, :Confidence, :SourceMemoryIds, :Status, :Scope, :ScopeId, :CreatedAt, :UpdatedAt)", row
        ))
        return row

    def prompt_view(self, session_id, fallback_charter):
        scenario = self.ensure_scenario("session", session_id) if session_id else None
        user_atoms = self.memory.query(
            "SELECT Relation, Value, Confidence, UpdatedAt, MemoryId FROM MemoryAtoms "
            "WHERE Entity = 'User' COLLATE NOCASE AND Scope = 'user' AND Status = 'active' "
            "AND Confidence >= 0.5 AND (ExpiresAt = '' OR ExpiresAt > ?) "
            "ORDER BY UpdatedAt DESC, MemoryId DESC LIMIT 100", (_now(),)
        ) or []
        traits = self.memory.query(
            "SELECT Trait, Value, Confidence, SourceMemoryIds FROM UserPersonaTraits WHERE Status = 'approved' AND Scope = 'user' AND (ExpiresAt = '' OR ExpiresAt > ?) ORDER BY UpdatedAt DESC LIMIT 8", (_now(),)
        ) or []
        scenario_atoms = []
        if scenario:
            scenario_atoms = self.memory.query(
                "SELECT a.* FROM MemoryAtoms a JOIN ScenarioMembers m ON m.MemoryId = a.MemoryId WHERE m.ScenarioId = ? AND a.Status = 'active' AND (a.ExpiresAt = '' OR a.ExpiresAt > ?) ORDER BY a.UpdatedAt DESC LIMIT 12",
                (scenario["ScenarioId"], _now()),
            ) or []
        return {"charter": self.active_charter(fallback_charter), "scenario": scenario, "user_atoms": user_atoms, "traits": traits, "scenario_atoms": scenario_atoms}

    def inspector(self, session_id=""):
        """Return bounded metadata and provenance for the local Memory Inspector."""
        scenario = self.ensure_scenario("session", session_id) if session_id else None
        atoms = self.memory.query(
            "SELECT MemoryId, Entity, Relation, Value, Kind, Trust, Status, Scope, ScopeId, Confidence, SourceRef, CreatedAt, UpdatedAt, ExpiresAt, SupersedesId FROM MemoryAtoms ORDER BY UpdatedAt DESC LIMIT 100"
        ) or []
        traits = self.memory.query(
            "SELECT TraitId, Trait, Value, Confidence, SourceMemoryIds, Status, Scope, ScopeId, UpdatedAt, ExpiresAt FROM UserPersonaTraits ORDER BY UpdatedAt DESC LIMIT 50"
        ) or []
        claims = self.memory.query(
            "SELECT ClaimId, Content, SourceRef, Status, CreatedAt, ReviewedAt, ReviewedBy FROM IdentityClaims ORDER BY CreatedAt DESC LIMIT 50"
        ) or []
        proposals = self.memory.query(
            "SELECT ProposalId, Kind, RiskLevel, Status, EvidenceRefs, CreatedAt, ReviewedAt, ReviewedBy FROM GrowthProposals ORDER BY CreatedAt DESC LIMIT 50"
        ) or []
        return {"scenario": scenario, "atoms": atoms, "traits": traits, "identity_claims": claims, "growth_proposals": proposals}

    def register_turn(self, turn_id, session_id, provider=""):
        turn_id = _clip(turn_id, 120)
        session_id = _clip(session_id, 160)
        if not turn_id or not session_id:
            raise ValueError("turn_id and session_id are required")
        now = _now()

        def write(conn):
            existing = conn.execute("SELECT Status FROM MemoryTurns WHERE TurnId = ?", (turn_id,)).fetchone()
            if existing:
                if existing["Status"] == "failed":
                    conn.execute("UPDATE MemoryTurns SET Status = 'started', CompletedAt = '' WHERE TurnId = ?", (turn_id,))
                    return True
                return False
            conn.execute(
                "INSERT INTO MemoryTurns (TurnId, SessionId, Provider, Status, CreatedAt) VALUES (?, ?, ?, 'started', ?)",
                (turn_id, session_id, _clip(provider, 80), now),
            )
            return True

        return self.memory.transaction(write)

    def fail_turn(self, turn_id, session_id="", provider=""):
        now = _now()

        def write(conn):
            row = conn.execute("SELECT TurnId FROM MemoryTurns WHERE TurnId = ?", (str(turn_id),)).fetchone()
            if row:
                conn.execute("UPDATE MemoryTurns SET Status = 'failed', CompletedAt = ? WHERE TurnId = ? AND Status = 'started'", (now, str(turn_id)))
            else:
                conn.execute(
                    "INSERT INTO MemoryTurns (TurnId, SessionId, Provider, Status, CreatedAt, CompletedAt) VALUES (?, ?, ?, 'failed', ?, ?)",
                    (str(turn_id), _clip(session_id, 160), _clip(provider, 80), now, now),
                )

        self.memory.transaction(write)

    def complete_turn(self, turn_id):
        now = _now()

        def write(conn):
            row = conn.execute("SELECT Status FROM MemoryTurns WHERE TurnId = ?", (str(turn_id),)).fetchone()
            if not row or row["Status"] == "completed":
                return False
            conn.execute("UPDATE MemoryTurns SET Status = 'completed', CompletedAt = ? WHERE TurnId = ?", (now, str(turn_id)))
            return True

        return self.memory.transaction(write)

    def register_skill_version(self, skill_id, tools, validation_spec=""):
        risk = classify_skill_risk(tools)
        now = _now()

        def write(conn):
            row = conn.execute("SELECT COALESCE(MAX(Version), 0) AS Version FROM SkillVersions WHERE SkillId = ?", (str(skill_id),)).fetchone()
            version = int(row["Version"] or 0) + 1
            skill_version_id = _identifier("skill-version")
            conn.execute(
                "INSERT INTO SkillVersions (SkillVersionId, SkillId, Version, RiskLevel, AllowedTools, ValidationSpec, Status, CreatedAt) VALUES (?, ?, ?, ?, ?, ?, 'draft', ?)",
                (skill_version_id, str(skill_id), version, risk, _clip(tools, 200), _clip(validation_spec, 500), now),
            )
            return {"SkillVersionId": skill_version_id, "SkillId": str(skill_id), "Version": version, "RiskLevel": risk, "Status": "draft"}

        return self.memory.transaction(write)

    def record_skill_evaluation(self, skill_version_id, outcome, evidence_ref=""):
        outcome = str(outcome or "").lower()
        if outcome not in {"success", "failure"}:
            raise ValueError("skill evaluation outcome must be success or failure")
        now = _now()
        evaluation_id = _identifier("skill-eval")
        evidence_ref = _clip(evidence_ref, 240)

        def write(conn):
            if evidence_ref:
                existing = conn.execute(
                    "SELECT EvaluationId FROM SkillEvaluations WHERE SkillVersionId = ? AND Outcome = ? AND EvidenceRef = ? LIMIT 1",
                    (str(skill_version_id), outcome, evidence_ref),
                ).fetchone()
                if existing:
                    return str(existing["EvaluationId"])
            conn.execute(
                "INSERT INTO SkillEvaluations (EvaluationId, SkillVersionId, Outcome, EvidenceRef, CreatedAt) VALUES (?, ?, ?, ?, ?)",
                (evaluation_id, str(skill_version_id), outcome, evidence_ref, now),
            )
            return evaluation_id

        return self.memory.transaction(write)

    def consider_provisional_skill(self, skill_version_id):
        rows = self.memory.query("SELECT * FROM SkillVersions WHERE SkillVersionId = ? LIMIT 1", (str(skill_version_id),)) or []
        if not rows or rows[0].get("Status") != "draft" or rows[0].get("RiskLevel") != "low":
            return False
        evaluations = self.memory.query(
            "SELECT Outcome FROM SkillEvaluations WHERE SkillVersionId = ?", (str(skill_version_id),)
        ) or []
        outcomes = [row.get("Outcome") for row in evaluations]
        if outcomes.count("success") < 2 or "failure" in outcomes:
            return False
        expires_at = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)).isoformat(timespec="seconds").replace("+00:00", "Z")

        def write(conn):
            conn.execute("UPDATE SkillVersions SET Status = 'provisional', ExpiresAt = ? WHERE SkillVersionId = ?", (expires_at, str(skill_version_id)))
            conn.execute("UPDATE Skills SET Status = 'provisional', UpdatedAt = ? WHERE SkillId = ?", (_now(), rows[0]["SkillId"]))
            return True

        return self.memory.transaction(write)

    def create_growth_proposal(self, kind, payload, risk_level, evidence_refs=None):
        kind = _clip(kind, 80).lower()
        risk_level = str(risk_level or "review").lower()
        if kind not in {"skill", "memory", "scenario", "identity"} or risk_level not in SKILL_RISKS:
            raise ValueError("unsupported growth proposal")
        proposal = {
            "ProposalId": _identifier("growth"), "Kind": kind,
            "Payload": json.dumps(payload if isinstance(payload, dict) else {}, separators=(",", ":"))[:4000],
            "RiskLevel": risk_level, "Status": "proposed",
            "EvidenceRefs": json.dumps([_clip(item, 240) for item in evidence_refs or [] if _clip(item, 240)], separators=(",", ":")),
            "CreatedAt": _now(),
        }
        self.memory.transaction(lambda conn: conn.execute(
            "INSERT INTO GrowthProposals (ProposalId, Kind, Payload, RiskLevel, Status, EvidenceRefs, CreatedAt) VALUES (:ProposalId, :Kind, :Payload, :RiskLevel, :Status, :EvidenceRefs, :CreatedAt)", proposal
        ))
        return proposal

    def review_growth_proposal(self, proposal_id, decision, reviewer="operator"):
        status = "approved" if decision == "approve" else "rejected" if decision == "reject" else ""
        if not status:
            raise ValueError("growth proposal decision must be approve or reject")
        now = _now()

        def write(conn):
            row = conn.execute("SELECT * FROM GrowthProposals WHERE ProposalId = ?", (str(proposal_id),)).fetchone()
            if not row or row["Status"] != "proposed":
                return None
            conn.execute("UPDATE GrowthProposals SET Status = ?, ReviewedAt = ?, ReviewedBy = ? WHERE ProposalId = ?", (status, now, _clip(reviewer, 80), str(proposal_id)))
            return dict(conn.execute("SELECT * FROM GrowthProposals WHERE ProposalId = ?", (str(proposal_id),)).fetchone())

        return self.memory.transaction(write)


class KustoMemoryModel:
    """Append-only Kusto counterpart for structured memory records.

    Kusto tables preserve each revision. Reads select the latest revision by
    stable ID, while a bridge-local lock serializes direct-provider turn
    registration in Eva's single local bridge process.
    """

    _ATOM_COLUMNS = ["MemoryId", "Entity", "Relation", "Value", "Kind", "Trust", "Status", "Scope", "ScopeId", "Confidence", "SourceRef", "CreatedAt", "UpdatedAt", "ExpiresAt", "SupersedesId"]
    _TRAIT_COLUMNS = ["TraitId", "Trait", "Value", "Confidence", "SourceMemoryIds", "Status", "Scope", "ScopeId", "CreatedAt", "UpdatedAt", "ExpiresAt"]
    _SCENARIO_COLUMNS = ["ScenarioId", "Scope", "ScopeId", "Title", "Summary", "Status", "CreatedAt", "UpdatedAt", "ExpiresAt"]
    _PROPOSAL_COLUMNS = ["ProposalId", "Kind", "Payload", "RiskLevel", "Status", "EvidenceRefs", "CreatedAt", "ReviewedAt", "ReviewedBy"]
    _SKILL_COLUMNS = ["SkillId", "Name", "Description", "Instructions", "Tools", "Tags", "Source", "Status", "CreatedAt", "UpdatedAt"]
    _TURN_STAGE_COLUMNS = ["TurnId", "Stage", "Status", "CreatedAt"]

    def __init__(self, cluster, database, query, ingest):
        self.cluster = cluster
        self.database = database
        self.query = query
        self.ingest = ingest
        self.last_registration_was_retry = False

    @staticmethod
    def _quote(value):
        return "'" + str(value or "").replace("'", "''")[:4000] + "'"

    def _read(self, query):
        return self.query(self.cluster, self.database, query) or []

    def _write(self, table, columns, row):
        if not self.ingest(self.cluster, self.database, table, columns, [row]):
            raise ValueError("structured memory write failed")
        return row

    def _latest(self, table, key, value, time_column="UpdatedAt"):
        rows = self._read(
            f"{table} | where {key} == {self._quote(value)} | summarize arg_max({time_column}, *) by {key} | take 1"
        )
        return rows[0] if rows else None

    def active_charter(self, fallback):
        rows = self._read("CoreIdentity | where Status =~ 'approved' | top 1 by Version desc | project Content")
        return _clip(rows[0].get("Content"), 6000) if rows else fallback

    def migrate_legacy_knowledge(self):
        marker = self._latest("MemoryMigrations", "MigrationId", "legacy-knowledge-atoms-v1", "AppliedAt")
        if marker:
            return 0
        rows = self._read("Knowledge | project Timestamp, Entity, Relation, Value, Confidence, Source")
        inserted = 0
        for row in rows:
            entity = _clip(row.get("Entity"), 120)
            relation = _clip(row.get("Relation"), 120)
            kind = _legacy_kind(entity, relation)
            if kind == "identity_claim":
                continue
            source_ref = "legacy-kusto:" + hashlib.sha256(
                (str(row.get("Timestamp", "")) + "\0" + entity + "\0" + relation + "\0" + str(row.get("Value", ""))).encode("utf-8")
            ).hexdigest()[:24]
            existing = self._read("MemoryAtoms | where SourceRef == " + self._quote(source_ref) + " | take 1")
            if existing:
                continue
            self.add_atom({
                "entity": entity, "relation": relation, "value": row.get("Value", ""), "kind": kind,
                "trust": "unconfirmed", "scope": "user" if entity.lower() == "user" else "global",
                "confidence": float(row.get("Confidence", 0.5) or 0.5), "source_ref": source_ref,
            }, [{"source_type": "legacy_knowledge", "source_ref": source_ref}])
            inserted += 1
        self._write("MemoryMigrations", ["MigrationId", "AppliedAt", "Details"], {
            "MigrationId": "legacy-knowledge-atoms-v1", "AppliedAt": _now(), "Details": "Copied legacy Knowledge as unconfirmed attributed atoms",
        })
        return inserted

    def ensure_scenario(self, scope, scope_id, title=""):
        scope = str(scope or "session").lower()
        if scope not in {"session", "project"}:
            raise ValueError("scenario scope must be session or project")
        scope_id = _clip(scope_id, 160)
        if not scope_id:
            raise ValueError("scenario scope_id is required")
        scenario_id = _scenario_id(scope, scope_id)
        existing = self._latest("MemoryScenarios", "ScenarioId", scenario_id)
        if existing and existing.get("Status") == "active" and existing.get("Scope") == scope and existing.get("ScopeId") == scope_id:
            requested_title = _clip(title, 160)
            if not requested_title or requested_title == str(existing.get("Title", "")):
                return existing
        now = _revision_now()
        row = dict(existing or {})
        row.update({"ScenarioId": scenario_id, "Scope": scope, "ScopeId": scope_id, "Title": _clip(title, 160) or row.get("Title", ""), "Summary": row.get("Summary", ""), "Status": "active", "CreatedAt": row.get("CreatedAt", now), "UpdatedAt": now, "ExpiresAt": row.get("ExpiresAt", "")})
        return self._write("MemoryScenarios", self._SCENARIO_COLUMNS, row)

    def add_atom(self, record, evidence=None):
        if not isinstance(record, dict):
            raise ValueError("memory atom must be an object")
        kind = str(record.get("kind", "fact")).lower()
        trust = str(record.get("trust", "unconfirmed")).lower()
        status = str(record.get("status", "active")).lower()
        scope = str(record.get("scope", "user")).lower()
        if kind not in ATOM_KINDS or trust not in ATOM_TRUST or status not in ATOM_STATUS or scope not in ATOM_SCOPES:
            raise ValueError("memory atom has unsupported lifecycle fields")
        value = _clip(record.get("value"), 4000)
        if not value:
            raise ValueError("memory atom value is required")
        confidence = float(record.get("confidence", 0.5))
        if not 0 <= confidence <= 1:
            raise ValueError("memory atom confidence must be between 0 and 1")
        now = _revision_now()
        row = {
            "MemoryId": _identifier("memory"), "Entity": _clip(record.get("entity"), 120), "Relation": _clip(record.get("relation"), 120),
            "Value": value, "Kind": kind, "Trust": trust, "Status": status, "Scope": scope, "ScopeId": _clip(record.get("scope_id"), 160),
            "Confidence": confidence, "SourceRef": _clip(record.get("source_ref"), 240), "CreatedAt": now, "UpdatedAt": now,
            "ExpiresAt": _clip(record.get("expires_at"), 40), "SupersedesId": _clip(record.get("supersedes_id"), 80),
        }
        self._write("MemoryAtoms", self._ATOM_COLUMNS, row)
        for item in evidence or []:
            if not isinstance(item, dict):
                continue
            source_type = _clip(item.get("source_type"), 80)
            source_ref = _clip(item.get("source_ref"), 240)
            if source_type and source_ref:
                self._write("MemoryEvidence", ["EvidenceId", "MemoryId", "SourceType", "SourceRef", "CreatedAt"], {
                    "EvidenceId": _identifier("evidence"), "MemoryId": row["MemoryId"], "SourceType": source_type, "SourceRef": source_ref, "CreatedAt": now,
                })
        return row

    def supersede_atom(self, memory_id, replacement):
        current = self._latest("MemoryAtoms", "MemoryId", memory_id)
        if not current or current.get("Status") != "active":
            raise ValueError("active memory atom not found")
        replacement = dict(replacement or {})
        for source, destination in (("Entity", "entity"), ("Relation", "relation"), ("Kind", "kind"), ("Scope", "scope"), ("ScopeId", "scope_id")):
            replacement.setdefault(destination, current.get(source, ""))
        replacement.setdefault("trust", "user_confirmed")
        replacement.setdefault("confidence", 1.0)
        replacement["supersedes_id"] = str(memory_id)
        new_atom = self.add_atom(replacement)
        current["Status"] = "superseded"
        current["UpdatedAt"] = _revision_now()
        self._write("MemoryAtoms", self._ATOM_COLUMNS, current)
        traits = self._read("UserPersonaTraits | where SourceMemoryIds has " + self._quote(memory_id))
        for trait in traits:
            trait["Status"] = "disabled"
            trait["UpdatedAt"] = _revision_now()
            self._write("UserPersonaTraits", self._TRAIT_COLUMNS, trait)
        return new_atom

    def delete_atom(self, memory_id):
        current = self._latest("MemoryAtoms", "MemoryId", memory_id)
        if not current or current.get("Status") in {"deleted", "superseded"}:
            return False
        current["Status"] = "deleted"
        current["UpdatedAt"] = _revision_now()
        self._write("MemoryAtoms", self._ATOM_COLUMNS, current)
        traits = self._read("UserPersonaTraits | where SourceMemoryIds has " + self._quote(memory_id))
        for trait in traits:
            trait["Status"] = "disabled"
            trait["UpdatedAt"] = _revision_now()
            self._write("UserPersonaTraits", self._TRAIT_COLUMNS, trait)
        return True

    def add_scenario_member(self, scenario_id, memory_id, role="context"):
        role = str(role or "context").lower()
        if role not in {"context", "decision", "constraint", "open_question"}:
            raise ValueError("unsupported scenario member role")
        existing = self._read(
            "ScenarioMembers | where ScenarioId == " + self._quote(scenario_id)
            + " and MemoryId == " + self._quote(memory_id)
            + " and Role == " + self._quote(role) + " | take 1"
        )
        if not existing:
            self._write("ScenarioMembers", ["ScenarioId", "MemoryId", "Role", "CreatedAt"], {
                "ScenarioId": str(scenario_id), "MemoryId": str(memory_id), "Role": role, "CreatedAt": _revision_now(),
            })

    def derive_trait(self, trait, value, source_memory_ids, scope="user", scope_id=""):
        source_memory_ids = [str(item) for item in source_memory_ids or [] if str(item)]
        if not source_memory_ids:
            raise ValueError("persona trait requires source memory")
        sources = [self._latest("MemoryAtoms", "MemoryId", item) for item in source_memory_ids]
        if any(not item or item.get("Status") != "active" or item.get("Trust") not in {"user_confirmed", "operator_approved"} for item in sources):
            raise ValueError("persona traits require active confirmed or approved source atoms")
        now = _revision_now()
        row = {
            "TraitId": _identifier("trait"), "Trait": _clip(trait, 80), "Value": _clip(value, 240), "Confidence": 1.0,
            "SourceMemoryIds": json.dumps(source_memory_ids, separators=(",", ":")), "Status": "approved", "Scope": _clip(scope, 40) or "user",
            "ScopeId": _clip(scope_id, 160), "CreatedAt": now, "UpdatedAt": now, "ExpiresAt": "",
        }
        if not row["Trait"] or not row["Value"]:
            raise ValueError("trait and value are required")
        return self._write("UserPersonaTraits", self._TRAIT_COLUMNS, row)

    def inspector(self, session_id=""):
        scenario = self.ensure_scenario("session", session_id) if session_id else None
        latest_atoms = self._read("MemoryAtoms | summarize arg_max(UpdatedAt, *) by MemoryId | order by UpdatedAt desc | take 100")
        latest_traits = self._read("UserPersonaTraits | summarize arg_max(UpdatedAt, *) by TraitId | order by UpdatedAt desc | take 50")
        claims = self._read("IdentityClaims | summarize arg_max(CreatedAt, *) by ClaimId | order by CreatedAt desc | take 50")
        proposals = self._read("GrowthProposals | summarize arg_max(CreatedAt, *) by ProposalId | order by CreatedAt desc | take 50")
        return {"scenario": scenario, "atoms": latest_atoms, "traits": latest_traits, "identity_claims": claims, "growth_proposals": proposals}

    def register_turn(self, turn_id, session_id, provider=""):
        turn_id = _clip(turn_id, 120)
        session_id = _clip(session_id, 160)
        if not turn_id or not session_id:
            raise ValueError("turn_id and session_id are required")
        with _KUSTO_TURN_LOCK:
            existing = self._latest("MemoryTurns", "TurnId", turn_id, "CreatedAt")
            if existing:
                if existing.get("Status") == "failed":
                    existing.update({"Status": "started", "CreatedAt": _revision_now(), "CompletedAt": ""})
                    self._write("MemoryTurns", ["TurnId", "SessionId", "Provider", "Status", "CreatedAt", "CompletedAt"], existing)
                    self.last_registration_was_retry = True
                    return True
                return False
            row = {"TurnId": turn_id, "SessionId": session_id, "Provider": _clip(provider, 80), "Status": "started", "CreatedAt": _revision_now(), "CompletedAt": ""}
            self._write("MemoryTurns", ["TurnId", "SessionId", "Provider", "Status", "CreatedAt", "CompletedAt"], row)
            return True

    def has_conversation_pair(self, session_id, user_message, assistant_response):
        rows = self._read(
            "Conversations | where SessionId == " + self._quote(session_id) + " | project Role, Content"
        )
        pairs = {(str(row.get("Role", "")), str(row.get("Content", ""))) for row in rows}
        return ("user", str(user_message)) in pairs and ("assistant", str(assistant_response)) in pairs

    def completed_turn_stages(self, turn_id):
        rows = self._read(
            "MemoryTurnStages | where TurnId == " + self._quote(turn_id)
            + " | summarize arg_max(CreatedAt, *) by Stage | where Status == 'completed' | project Stage"
        )
        return {str(row.get("Stage", "")) for row in rows if row.get("Stage")}

    def has_turn_event(self, table, turn_id):
        return bool(self._read(
            str(table) + " | where TurnId == " + self._quote(turn_id) + " | take 1"
        ))

    def complete_turn_stage(self, turn_id, stage):
        stage = _clip(stage, 80)
        if not stage:
            raise ValueError("turn stage is required")
        self._write("MemoryTurnStages", self._TURN_STAGE_COLUMNS, {
            "TurnId": _clip(turn_id, 120), "Stage": stage, "Status": "completed", "CreatedAt": _revision_now(),
        })

    def complete_turn(self, turn_id):
        with _KUSTO_TURN_LOCK:
            row = self._latest("MemoryTurns", "TurnId", turn_id, "CreatedAt")
            if not row or row.get("Status") == "completed":
                return False
            row["Status"] = "completed"
            row["CreatedAt"] = _revision_now()
            row["CompletedAt"] = row["CreatedAt"]
            self._write("MemoryTurns", ["TurnId", "SessionId", "Provider", "Status", "CreatedAt", "CompletedAt"], row)
            return True

    def fail_turn(self, turn_id, session_id="", provider=""):
        with _KUSTO_TURN_LOCK:
            row = self._latest("MemoryTurns", "TurnId", turn_id, "CreatedAt")
            if row and row.get("Status") == "started":
                row["Status"] = "failed"
                row["CreatedAt"] = _revision_now()
                row["CompletedAt"] = row["CreatedAt"]
                self._write("MemoryTurns", ["TurnId", "SessionId", "Provider", "Status", "CreatedAt", "CompletedAt"], row)
            elif row is None:
                now = _revision_now()
                self._write("MemoryTurns", ["TurnId", "SessionId", "Provider", "Status", "CreatedAt", "CompletedAt"], {
                    "TurnId": _clip(turn_id, 120), "SessionId": _clip(session_id, 160), "Provider": _clip(provider, 80),
                    "Status": "failed", "CreatedAt": now, "CompletedAt": now,
                })

    def register_skill_version(self, skill_id, tools, validation_spec=""):
        rows = self._read("SkillVersions | where SkillId == " + self._quote(skill_id) + " | top 1 by Version desc")
        version = int(rows[0].get("Version") or 0) + 1 if rows else 1
        row = {
            "SkillVersionId": _identifier("skill-version"), "SkillId": str(skill_id), "Version": version,
            "RiskLevel": classify_skill_risk(tools), "TriggerSpec": "", "AllowedTools": _clip(tools, 200),
            "ValidationSpec": _clip(validation_spec, 500), "Status": "draft", "ExpiresAt": "", "CreatedAt": _revision_now(),
        }
        return self._write("SkillVersions", ["SkillVersionId", "SkillId", "Version", "RiskLevel", "TriggerSpec", "AllowedTools", "ValidationSpec", "Status", "ExpiresAt", "CreatedAt"], row)

    def record_skill_evaluation(self, skill_version_id, outcome, evidence_ref=""):
        outcome = str(outcome or "").lower()
        if outcome not in {"success", "failure"}:
            raise ValueError("skill evaluation outcome must be success or failure")
        evidence_ref = _clip(evidence_ref, 240)
        if evidence_ref and self._read("SkillEvaluations | where SkillVersionId == " + self._quote(skill_version_id) + " and Outcome == " + self._quote(outcome) + " and EvidenceRef == " + self._quote(evidence_ref) + " | take 1"):
            return ""
        evaluation_id = _identifier("skill-eval")
        self._write("SkillEvaluations", ["EvaluationId", "SkillVersionId", "Outcome", "EvidenceRef", "CreatedAt"], {
            "EvaluationId": evaluation_id, "SkillVersionId": str(skill_version_id), "Outcome": outcome, "EvidenceRef": evidence_ref, "CreatedAt": _revision_now(),
        })
        return evaluation_id

    def consider_provisional_skill(self, skill_version_id):
        row = self._latest("SkillVersions", "SkillVersionId", skill_version_id, "CreatedAt")
        if not row or row.get("Status") != "draft" or row.get("RiskLevel") != "low":
            return False
        outcomes = self._read("SkillEvaluations | where SkillVersionId == " + self._quote(skill_version_id) + " | project Outcome")
        values = [item.get("Outcome") for item in outcomes]
        if values.count("success") < 2 or "failure" in values:
            return False
        row["Status"] = "provisional"
        row["CreatedAt"] = _revision_now()
        row["ExpiresAt"] = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)).isoformat(timespec="seconds").replace("+00:00", "Z")
        self._write("SkillVersions", ["SkillVersionId", "SkillId", "Version", "RiskLevel", "TriggerSpec", "AllowedTools", "ValidationSpec", "Status", "ExpiresAt", "CreatedAt"], row)
        skill = self._latest("Skills", "SkillId", row["SkillId"])
        if skill is None:
            return False
        skill["Status"] = "provisional"
        skill["UpdatedAt"] = _revision_now()
        self._write("Skills", self._SKILL_COLUMNS, skill)
        return True

    def create_growth_proposal(self, kind, payload, risk_level, evidence_refs=None):
        kind = _clip(kind, 80).lower()
        risk_level = str(risk_level or "review").lower()
        if kind not in {"skill", "memory", "scenario", "identity"} or risk_level not in SKILL_RISKS:
            raise ValueError("unsupported growth proposal")
        row = {
            "ProposalId": _identifier("growth"), "Kind": kind, "Payload": payload if isinstance(payload, dict) else {},
            "RiskLevel": risk_level, "Status": "proposed", "EvidenceRefs": [_clip(item, 240) for item in evidence_refs or [] if _clip(item, 240)],
            "CreatedAt": _revision_now(), "ReviewedAt": "", "ReviewedBy": "",
        }
        return self._write("GrowthProposals", self._PROPOSAL_COLUMNS, row)

    def review_growth_proposal(self, proposal_id, decision, reviewer="operator"):
        status = "approved" if decision == "approve" else "rejected" if decision == "reject" else ""
        if not status:
            raise ValueError("growth proposal decision must be approve or reject")
        row = self._latest("GrowthProposals", "ProposalId", proposal_id, "CreatedAt")
        if not row or row.get("Status") != "proposed":
            return None
        row.update({"Status": status, "CreatedAt": _revision_now(), "ReviewedAt": _revision_now(), "ReviewedBy": _clip(reviewer, 80)})
        return self._write("GrowthProposals", self._PROPOSAL_COLUMNS, row)