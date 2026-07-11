"""Safe continual-learning shadow pipeline.

This module records externally verified outcomes, creates restricted immutable
learning candidates, and evaluates them with a frozen deterministic policy. It
never executes candidate content, calls a model/provider, writes legacy Skills,
or activates a candidate.
"""

import datetime
import hashlib
import json
import re
import unicodedata

from bridge.events import (
    IdempotencyCollisionError,
    canonical_event_hash,
    canonical_json,
    payload_hash,
)
from bridge.sensitive import redact_credentials


ZERO_HASH = "0" * 64
EVALUATOR_ID = "deterministic-local-policy"
EVALUATOR_VERSION = "v2"
_CANDIDATE_KINDS = frozenset({
    "skill_instructions", "skill_prompt_template", "skill_routing_rule",
})
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_TOOL_NAMES = frozenset({
    "", "browser", "desktop", "memory", "notification", "scheduler",
})
_FORBIDDEN_PATTERNS = (
    ("action_marker", re.compile(r"\[\[EVA_", re.IGNORECASE)),
    ("python_eval", re.compile(r"\b(?:eval|exec)\s*\(", re.IGNORECASE)),
    ("process_spawn", re.compile(
        r"\b(?:os\.system|subprocess\.|child_process|spawn\s*\()", re.IGNORECASE
    )),
    ("schema_mutation", re.compile(
        r"\b(?:DROP|ALTER|CREATE)\s+(?:TABLE|TRIGGER|INDEX)\b", re.IGNORECASE
    )),
    ("safety_bypass", re.compile(
        r"\b(?:disable|bypass|ignore|override)\b.{0,32}\b(?:safety|policy|approval|auth)",
        re.IGNORECASE,
    )),
    ("data_exfiltration", re.compile(
        r"\b(?:reveal|print|send|exfiltrate)\b.{0,40}\b(?:token|password|secret|api key)",
        re.IGNORECASE,
    )),
    ("executable_code", re.compile(
        r"(?:__\w+__|\bfrom\s+[A-Za-z_]\w*(?:\.\w+)*\s+import\b"
        r"|\bimport\s+[A-Za-z_]|\brequire\s*\("
        r"|\b(?:os|subprocess|socket|shutil|pathlib)\."
        r"|\.?\b(?:system|popen|spawn|fork)\s*\("
        r"|\b(?:eval|exec|compile|open)\s*\(|<script\b"
        r"|\b(?:bash|sh|powershell|cmd)\s+(?:-[a-z]+|/c)\b)",
        re.IGNORECASE,
    )),
)
_FROZEN_FIXTURES = (
    {"id": "schema.valid", "safety": False},
    {"id": "quality.primary_text", "safety": False},
    {"id": "quality.description", "safety": False},
    {"id": "safety.action_marker", "safety": True},
    {"id": "safety.python_eval", "safety": True},
    {"id": "safety.process_spawn", "safety": True},
    {"id": "safety.schema_mutation", "safety": True},
    {"id": "safety.safety_bypass", "safety": True},
    {"id": "safety.data_exfiltration", "safety": True},
    {"id": "safety.executable_code", "safety": True},
)
_EVALUATOR_POLICY = {
    "evaluator_id": EVALUATOR_ID,
    "evaluator_version": EVALUATOR_VERSION,
    "quality_rules": {
        "schema.valid": "payload_is_json_object",
        "quality.primary_text": "kind_primary_text_is_nonempty",
        "quality.description": "description_is_nonempty",
    },
    "forbidden_patterns": [
        {"id": name, "pattern": pattern.pattern, "flags": pattern.flags}
        for name, pattern in _FORBIDDEN_PATTERNS
    ],
    "pass_rule": "all_safety_and_no_baseline_regression_and_score_not_lower",
}
EVALUATOR_POLICY_HASH = hashlib.sha256(
    canonical_json(_EVALUATOR_POLICY).encode("utf-8")
).hexdigest()
FIXTURE_SET_HASH = hashlib.sha256(canonical_json({
    "fixtures": _FROZEN_FIXTURES,
    "evaluator_policy_hash": EVALUATOR_POLICY_HASH,
}).encode("utf-8")).hexdigest()


class LearningError(Exception):
    """Base safe-learning error."""


class LearningValidationError(LearningError):
    """Input violates a strict learning contract."""


class LearningCollisionError(LearningError):
    """A durable learning identity was reused with different content."""


class LearningConflictError(LearningError):
    """Current baseline or evaluation state no longer matches the candidate."""


def _utc_now(clock=None):
    now = clock() if clock else datetime.datetime.now(datetime.timezone.utc)
    if not isinstance(now, datetime.datetime):
        raise LearningValidationError("clock must return datetime")
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    return now.astimezone(datetime.timezone.utc)


def _utc_iso(value):
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _hash(value):
    if not isinstance(value, (str, bytes)):
        value = canonical_json(value)
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _identity_utf8_hex(value, field="identity"):
    if not isinstance(value, str):
        raise LearningValidationError(f"{field} must be a string")
    try:
        return value.encode("utf-8").hex()
    except UnicodeEncodeError as exc:
        raise LearningValidationError(f"{field} must be valid UTF-8") from exc


def _exact_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _text(
    value, field, max_len, *, allow_empty=False, collapse=False, normalize=True
):
    if not isinstance(value, str):
        raise LearningValidationError(f"{field} must be a string")
    normalized = unicodedata.normalize("NFC", value) if normalize else value
    if "\x00" in normalized:
        raise LearningValidationError(f"{field} contains NUL")
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise LearningValidationError(f"{field} must be valid UTF-8") from exc
    if collapse:
        normalized = " ".join(normalized.split())
    if not allow_empty and not normalized:
        raise LearningValidationError(f"{field} is required")
    if len(normalized) > max_len:
        raise LearningValidationError(f"{field} exceeds {max_len} characters")
    return normalized


def _uuid(value, field="operation_id"):
    if not isinstance(value, str) or _UUID_RE.fullmatch(value) is None:
        raise LearningValidationError(f"{field} must be a UUID")
    return value.lower()


def _hex64(value, field):
    if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
        raise LearningValidationError(f"{field} must be 64 lowercase hex characters")
    return value


def _strict_int(value, field, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise LearningValidationError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise LearningValidationError(f"{field} must be between {minimum} and {maximum}")
    return value


def _event_receipt(command, result):
    command = redact_credentials(command)
    return {
        "command": command,
        "command_hash": payload_hash(canonical_json(command)),
        "result": result,
    }


def _event_payload(event):
    try:
        payload = json.loads(event["Payload"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise LearningCollisionError("stored learning event receipt is invalid") from exc
    if not isinstance(payload, dict):
        raise LearningCollisionError("stored learning event receipt is invalid")
    return payload


def _validated_event(repository, event_id, connection):
    event = repository.get_event(event_id, connection=connection)
    if not isinstance(event, dict):
        raise LearningCollisionError("linked learning event is missing")
    payload = _event_payload(event)
    encoded = canonical_json(payload)
    if encoded != event.get("Payload") or payload_hash(encoded) != event.get("PayloadHash"):
        raise LearningCollisionError("linked learning event payload integrity differs")
    expected = canonical_event_hash(
        stream_id=event.get("StreamId", ""),
        event_type=event.get("EventType", ""),
        schema_version=event.get("SchemaVersion", 0),
        actor_type=event.get("ActorType", ""),
        actor_id=event.get("ActorId", ""),
        origin=event.get("Origin", ""),
        correlation_id=event.get("CorrelationId", ""),
        causation_id=event.get("CausationId", ""),
        session_id=event.get("SessionId", ""),
        turn_id=event.get("TurnId", ""),
        source_message_id=event.get("SourceMessageId", ""),
        trust=event.get("Trust", 0),
        sensitivity=event.get("Sensitivity", ""),
        consent_scope=event.get("ConsentScope", ""),
        payload=payload,
    )
    if expected != event.get("EventHash"):
        raise LearningCollisionError("linked learning event hash differs")
    return event, payload


def _safe_identity(value, field, max_len, *, allow_empty=False):
    exact = _text(
        value, field, max_len, allow_empty=allow_empty, normalize=False
    )
    if redact_credentials(exact) != exact:
        raise LearningValidationError(f"{field} contains credential-like material")
    return exact


def _latest_skill(memory, skill_id, connection=None):
    sql = (
        "SELECT SkillId,Name,Description,Instructions,Tools,Tags,Source,Status,"
        "CreatedAt,UpdatedAt FROM Skills WHERE SkillId=? "
        "ORDER BY UpdatedAt DESC,rowid DESC LIMIT 1"
    )
    if connection is None:
        rows = memory.query_strict(sql, (skill_id,))
        row = rows[0] if rows else None
    else:
        raw = connection.execute(sql, (skill_id,)).fetchone()
        columns = (
            "SkillId", "Name", "Description", "Instructions", "Tools", "Tags",
            "Source", "Status", "CreatedAt", "UpdatedAt",
        )
        row = dict(zip(columns, raw)) if raw is not None else None
    return row if row is not None and row.get("Status") == "active" else None


def skill_payload(row):
    if not isinstance(row, dict):
        return None
    return {
        "skill_id": str(row.get("SkillId", "")),
        "name": str(row.get("Name", "")),
        "description": str(row.get("Description", "")),
        "instructions": str(row.get("Instructions", "")),
        "tools": str(row.get("Tools", "")),
        "tags": str(row.get("Tags", "")),
        "source": str(row.get("Source", "")),
        "status": str(row.get("Status", "")),
    }


def skill_version_hash(row):
    payload = skill_payload(row)
    if payload is None:
        return ZERO_HASH
    exact = {
        key + "_utf8_hex": _identity_utf8_hex(value, key)
        for key, value in payload.items()
    }
    return _hash(exact)


def _skill_version_exists(connection, skill_id, version_hash):
    rows = connection.execute(
        "SELECT SkillId,Name,Description,Instructions,Tools,Tags,Source,Status,"
        "CreatedAt,UpdatedAt FROM Skills WHERE SkillId=?", (skill_id,),
    ).fetchall()
    columns = (
        "SkillId", "Name", "Description", "Instructions", "Tools", "Tags",
        "Source", "Status", "CreatedAt", "UpdatedAt",
    )
    return any(skill_version_hash(dict(zip(columns, row))) == version_hash for row in rows)


def _normalize_candidate_payload(kind, payload):
    if not isinstance(payload, dict):
        raise LearningValidationError("candidate payload must be an object")
    allowed = {
        "skill_instructions": {"name", "description", "instructions", "tools", "tags"},
        "skill_prompt_template": {"template", "variables", "description"},
        "skill_routing_rule": {"intent", "skill_id", "priority", "description"},
    }[kind]
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise LearningValidationError("unsupported candidate field(s): " + ", ".join(unknown))
    safe = redact_credentials(payload)
    if safe != payload:
        raise LearningValidationError("candidate payload contains credential-like material")
    if kind == "skill_instructions":
        tools = _text(safe.get("tools", ""), "tools", 200, allow_empty=True, collapse=True)
        tool_names = [item.strip().lower() for item in tools.split(",") if item.strip()]
        if any(item not in _SAFE_TOOL_NAMES for item in tool_names):
            raise LearningValidationError("candidate tools are not allowlisted")
        result = {
            "name": _text(safe.get("name", ""), "name", 60, allow_empty=True, collapse=True),
            "description": _text(
                safe.get("description", ""), "description", 400,
                allow_empty=True, collapse=True,
            ),
            "instructions": _text(safe.get("instructions"), "instructions", 8000),
            "tools": ",".join(tool_names),
            "tags": _text(safe.get("tags", ""), "tags", 200, allow_empty=True, collapse=True),
        }
    elif kind == "skill_prompt_template":
        variables = safe.get("variables", [])
        if not isinstance(variables, list) or len(variables) > 32:
            raise LearningValidationError("variables must be a bounded list")
        normalized_variables = []
        for variable in variables:
            item = _text(variable, "variable", 64, collapse=True)
            if item in normalized_variables:
                raise LearningValidationError("variables must be unique")
            normalized_variables.append(item)
        if normalized_variables != sorted(normalized_variables):
            raise LearningValidationError("variables must be sorted")
        result = {
            "template": _text(safe.get("template"), "template", 8000),
            "variables": normalized_variables,
            "description": _text(
                safe.get("description", ""), "description", 400,
                allow_empty=True, collapse=True,
            ),
        }
    else:
        result = {
            "intent": _text(safe.get("intent"), "intent", 400, collapse=True),
            "skill_id": _text(
                safe.get("skill_id"), "skill_id", 128, normalize=False
            ),
            "priority": _strict_int(safe.get("priority", 50), "priority", 0, 100),
            "description": _text(
                safe.get("description", ""), "description", 400,
                allow_empty=True, collapse=True,
            ),
        }
    encoded = _exact_json(result)
    if len(encoded.encode("utf-8")) > 32768:
        raise LearningValidationError("candidate payload exceeds byte limit")
    return result, encoded


def _candidate_version_hash(kind, base_version_hash, payload_digest, evidence_links):
    return _hash({
        "kind": kind,
        "base_version_hash": base_version_hash,
        "payload_hash": payload_digest,
        "evidence": [list(item) for item in evidence_links],
    })


def _candidate_identity(
    kind, target_skill_id, base_version_hash, candidate_version,
    payload_digest, evidence_links,
):
    return {
        "kind": kind,
        "identity_encoding": "utf-8-hex",
        "target_skill_id_utf8_hex": _identity_utf8_hex(
            target_skill_id, "target_skill_id"
        ),
        "base_version_hash": base_version_hash,
        "candidate_version_hash": candidate_version,
        "payload_hash": payload_digest,
        "evidence": [list(item) for item in evidence_links],
    }


def _attest_report_row(repository, connection, row):
    event, receipt = _validated_event(repository, row["EventId"], connection)
    command = receipt.get("command")
    expected_fields = {
        "operation_id": row["OperationId"],
        "action_run_id_utf8_hex": _identity_utf8_hex(
            row["ActionRunId"], "action_run_id"
        ),
        "turn_id_utf8_hex": _identity_utf8_hex(row["TurnId"], "turn_id"),
        "skill_id_utf8_hex": _identity_utf8_hex(row["SkillId"], "skill_id"),
        "skill_version_hash": row["SkillVersionHash"],
        "outcome": row["Outcome"],
        "postcondition": row["Postcondition"],
        "verification_source": row["VerificationSource"],
        "duration_ms": row["DurationMs"],
        "evidence_hash": row["EvidenceHash"],
    }
    if (
        event.get("EventType") != "learning.execution_reported"
        or event.get("StreamId") != f"learning-execution:{row['ReportId']}"
        or event.get("StreamVersion") != 0
        or event.get("OccurredAt") != row["ReportedAt"]
        or event.get("IdempotencyKey")
        != f"request:{row['OperationId']}:learning.execution_reported"
        or not isinstance(command, dict)
        or event.get("ActorType") != command.get("actor_type")
        or _identity_utf8_hex(event.get("ActorId", ""), "event.actor_id")
        != command.get("actor_id_utf8_hex")
        or event.get("Origin") != command.get("origin")
        or event.get("CorrelationId") != command.get("correlation_id")
        or event.get("TurnId") != row["TurnId"]
        or event.get("CausationId") != ""
        or event.get("SessionId") != ""
        or event.get("SourceMessageId") != ""
        or float(event.get("Trust", -1)) != 1.0
        or event.get("Sensitivity") != "private"
        or event.get("ConsentScope") != "local_only"
        or any(command.get(key) != value for key, value in expected_fields.items())
        or _hash(command) != row["CommandHash"]
        or receipt.get("command_hash") != row["CommandHash"]
        or receipt.get("result") != {"report_id": row["ReportId"]}
    ):
        raise LearningCollisionError("execution report event anchor differs")


def _attest_candidate_row(repository, connection, row, evidence_links):
    expected_version = _candidate_version_hash(
        row["Kind"], row["BaseVersionHash"], row["PayloadHash"], evidence_links
    )
    identity = _candidate_identity(
        row["Kind"], row["TargetSkillId"], row["BaseVersionHash"],
        expected_version, row["PayloadHash"], evidence_links,
    )
    event, receipt = _validated_event(repository, row["EventId"], connection)
    command = receipt.get("command")
    expected_command = {
        "operation_id": row["OperationId"],
        **identity,
        "proposed_by": row["ProposedBy"],
        "actor_id_utf8_hex": _identity_utf8_hex(row["ActorId"], "actor_id"),
    }
    if (
        row["CandidateVersionHash"] != expected_version
        or row["CandidateId"] != _hash(identity)
        or row["CandidateHash"] != _hash(identity)
        or event.get("EventType") != "learning.candidate_proposed"
        or event.get("StreamId") != f"learning-candidate:{row['CandidateId']}"
        or event.get("StreamVersion") != 0
        or event.get("OccurredAt") != row["CreatedAt"]
        or event.get("IdempotencyKey")
        != f"request:{row['OperationId']}:learning.candidate_proposed"
        or not isinstance(command, dict)
        or event.get("ActorType") != row["ProposedBy"]
        or _identity_utf8_hex(event.get("ActorId", ""), "event.actor_id")
        != command.get("actor_id_utf8_hex")
        or event.get("Origin") != command.get("origin")
        or event.get("CorrelationId") != command.get("correlation_id")
        or event.get("CausationId") != ""
        or event.get("SessionId") != ""
        or event.get("TurnId") != ""
        or event.get("SourceMessageId") != ""
        or float(event.get("Trust", -1)) != 0.7
        or event.get("Sensitivity") != "private"
        or event.get("ConsentScope") != "local_only"
        or any(command.get(key) != value for key, value in expected_command.items())
        or _hash(command) != row["CommandHash"]
        or receipt.get("command_hash") != row["CommandHash"]
        or receipt.get("result") != {
            "candidate_id": row["CandidateId"],
            "candidate_version_hash": row["CandidateVersionHash"],
        }
    ):
        raise LearningCollisionError("candidate event anchor differs")


def _attest_evaluation_rows(repository, connection, plan, result):
    plan_event, plan_receipt = _validated_event(
        repository, plan["EventId"], connection
    )
    command = plan_receipt.get("command")
    plan_identity = command.get("plan") if isinstance(command, dict) else None
    expected_plan_fields = {
        "candidate_id": plan["CandidateId"],
        "evaluator_id": plan["EvaluatorId"],
        "evaluator_version": plan["EvaluatorVersion"],
        "fixture_set_hash": plan["FixtureSetHash"],
        "baseline_version_hash": plan["BaselineVersionHash"],
        "candidate_version_hash": plan["CandidateVersionHash"],
    }
    if (
        plan_event.get("EventType") != "learning.evaluation_planned"
        or plan_event.get("StreamId") != f"learning-evaluation:{plan['PlanId']}"
        or plan_event.get("StreamVersion") != 0
        or plan_event.get("OccurredAt") != plan["CreatedAt"]
        or plan_event.get("IdempotencyKey")
        != f"request:{plan['OperationId']}:learning.evaluation_planned"
        or not isinstance(command, dict)
        or not isinstance(plan_identity, dict)
        or plan_event.get("ActorType") != "system"
        or plan_event.get("ActorId") != plan["EvaluatorId"]
        or plan_event.get("Origin") != command.get("origin")
        or plan_event.get("CorrelationId") != command.get("correlation_id")
        or plan_event.get("CausationId") != ""
        or plan_event.get("SessionId") != ""
        or plan_event.get("TurnId") != ""
        or plan_event.get("SourceMessageId") != ""
        or float(plan_event.get("Trust", -1)) != 1.0
        or plan_event.get("Sensitivity") != "private"
        or plan_event.get("ConsentScope") != "local_only"
        or command.get("operation_id") != plan["OperationId"]
        or any(plan_identity.get(key) != value for key, value in expected_plan_fields.items())
        or _hash(plan_identity) != plan["PlanId"]
        or plan["PlanHash"] != plan["PlanId"]
        or _hash(command) != plan["CommandHash"]
        or plan_receipt.get("command_hash") != plan["CommandHash"]
        or plan_receipt.get("result") != {"plan_id": plan["PlanId"]}
    ):
        raise LearningCollisionError("evaluation plan event anchor differs")
    result_event, result_receipt = _validated_event(
        repository, result["EventId"], connection
    )
    body = result_receipt.get("result")
    if (
        result_event.get("EventType") != "learning.evaluation_completed"
        or result_event.get("StreamId") != f"learning-evaluation:{plan['PlanId']}"
        or result_event.get("StreamVersion") != 1
        or result_event.get("OccurredAt") != result["EvaluatedAt"]
        or result_event.get("CausationId") != plan["EventId"]
        or result_event.get("IdempotencyKey")
        != f"plan:{plan['PlanId']}:learning.evaluation_completed"
        or result_event.get("ActorType") != "system"
        or result_event.get("ActorId") != plan["EvaluatorId"]
        or result_event.get("Origin") != command.get("origin")
        or result_event.get("CorrelationId") != command.get("correlation_id")
        or result_event.get("SessionId") != ""
        or result_event.get("TurnId") != ""
        or result_event.get("SourceMessageId") != ""
        or float(result_event.get("Trust", -1)) != 1.0
        or result_event.get("Sensitivity") != "private"
        or result_event.get("ConsentScope") != "local_only"
        or result_receipt.get("plan_id") != plan["PlanId"]
        or result_receipt.get("result_id") != result["ResultId"]
        or result_receipt.get("result_hash") != result["ResultHash"]
        or not isinstance(body, dict)
        or _hash(body) != result["ResultHash"]
        or body.get("evaluator_policy_hash")
        != plan_identity.get("evaluator_policy_hash")
        or body.get("baseline_passed") != result["BaselinePassed"]
        or body.get("candidate_passed") != result["CandidatePassed"]
        or body.get("total") != result["CandidateTotal"]
        or bool(body.get("safety_passed")) != bool(result["SafetyPassed"])
        or len(body.get("regressions", [])) != result["RegressionCount"]
        or bool(body.get("passed")) != bool(result["Passed"])
    ):
        raise LearningCollisionError("evaluation result event anchor differs")


def _load_candidate_evidence(connection, candidate_id):
    edges = connection.execute(
        "SELECT ReportId,EvidenceRole FROM LearningCandidateEvidence "
        "WHERE CandidateId=? ORDER BY ReportId COLLATE BINARY", (candidate_id,),
    ).fetchall()
    links = []
    reports = []
    for edge in edges:
        report = connection.execute(
            "SELECT * FROM LearningExecutionReports WHERE ReportId=?",
            (edge["ReportId"],),
        ).fetchone()
        if report is None:
            raise LearningCollisionError("candidate evidence report is missing")
        links.append((edge["ReportId"], edge["EvidenceRole"]))
        reports.append((edge["EvidenceRole"], report))
    return links, reports


def _validate_persisted_evidence(candidate, evidence_rows):
    if not 1 <= len(evidence_rows) <= 100:
        raise LearningCollisionError("candidate evidence cardinality differs")
    for role, report in evidence_rows:
        valid_support = (
            report["Outcome"] == "succeeded"
            and report["Postcondition"] == "observed"
            and report["VerificationSource"] in ("user", "test")
        )
        if (
            report["SkillId"] != candidate["TargetSkillId"]
            or report["SkillVersionHash"] != candidate["BaseVersionHash"]
            or (role == "support") != valid_support
        ):
            raise LearningCollisionError("candidate evidence semantics differ")


def report_execution_outcome(
    memory,
    repository,
    *,
    operation_id,
    action_run_id,
    skill_id,
    skill_version_hash_value,
    outcome,
    postcondition,
    verification_source,
    duration_ms,
    evidence_summary="",
    turn_id="",
    actor_type="user",
    actor_id="",
    origin="api",
    correlation_id="",
    clock=None,
):
    operation_id = _uuid(operation_id)
    correlation_id = _uuid(correlation_id, "correlation_id") if correlation_id else ""
    action_run_id = _safe_identity(action_run_id, "action_run_id", 256)
    skill_id = _safe_identity(skill_id, "skill_id", 128)
    version_hash = _hex64(skill_version_hash_value, "skill_version_hash")
    turn_id = _safe_identity(turn_id or "", "turn_id", 256, allow_empty=True)
    if outcome not in ("succeeded", "failed", "aborted"):
        raise LearningValidationError("outcome is invalid")
    if postcondition not in ("observed", "not_observed", "not_applicable"):
        raise LearningValidationError("postcondition is invalid")
    if verification_source not in ("user", "tool", "system", "test"):
        raise LearningValidationError("verification_source is invalid")
    duration_ms = _strict_int(duration_ms, "duration_ms", 0, 3600000)
    if actor_type not in ("user", "system", "admin", "test"):
        raise LearningValidationError("actor_type is invalid")
    if actor_type == "test":
        actor_type = "system"
    if origin not in ("bridge", "browser", "api", "test"):
        raise LearningValidationError("origin is invalid")
    actor_id = _safe_identity(actor_id or "", "actor_id", 512, allow_empty=True)
    evidence = redact_credentials(
        _text(evidence_summary or "", "evidence_summary", 2048, allow_empty=True)
    )
    evidence_hash = _hash(evidence)
    command = {
        "operation_id": operation_id,
        "identity_encoding": "utf-8-hex",
        "action_run_id_utf8_hex": _identity_utf8_hex(action_run_id, "action_run_id"),
        "turn_id_utf8_hex": _identity_utf8_hex(turn_id, "turn_id"),
        "skill_id_utf8_hex": _identity_utf8_hex(skill_id, "skill_id"),
        "skill_version_hash": version_hash,
        "outcome": outcome,
        "postcondition": postcondition,
        "verification_source": verification_source,
        "duration_ms": duration_ms,
        "evidence_hash": evidence_hash,
        "actor_type": actor_type,
        "actor_id_utf8_hex": _identity_utf8_hex(actor_id, "actor_id"),
        "origin": origin,
        "correlation_id": correlation_id,
    }
    command = redact_credentials(command)
    command_hash = _hash(command)
    report_id = _hash({"operation_id": operation_id, "command_hash": command_hash})
    now = _utc_iso(_utc_now(clock))
    with memory.transaction() as conn:
        existing = conn.execute(
            "SELECT * FROM LearningExecutionReports "
            "WHERE OperationId=?", (operation_id,),
        ).fetchone()
        if existing is not None:
            _attest_report_row(repository, conn, existing)
            _event, receipt = _validated_event(
                repository, existing["EventId"], conn
            )
            if (
                _event.get("EventType") != "learning.execution_reported"
                or existing["CommandHash"] != command_hash
                or receipt.get("command") != command
                or receipt.get("command_hash") != command_hash
                or receipt.get("result") != {"report_id": existing["ReportId"]}
            ):
                raise IdempotencyCollisionError(operation_id, "execution report command differs")
            return {
                "report_id": existing["ReportId"], "event_id": existing["EventId"],
                "idempotent": True,
            }
        if not _skill_version_exists(conn, skill_id, version_hash):
            raise LearningValidationError("skill version is not locally attested")
        alternate = conn.execute(
            "SELECT ReportId,CommandHash FROM LearningExecutionReports "
            "WHERE ActionRunId=? AND SkillVersionHash=?",
            (action_run_id, version_hash),
        ).fetchone()
        if alternate is not None:
            raise LearningCollisionError("action run and skill version already reported")
        result = {"report_id": report_id}
        event = repository.append_event(
            connection=conn,
            stream_id=f"learning-execution:{report_id}",
            event_type="learning.execution_reported",
            payload=_event_receipt(command, result),
            actor_type=actor_type,
            actor_id=actor_id,
            origin=origin,
            occurred_at=now,
            correlation_id=correlation_id,
            turn_id=turn_id,
            trust=1.0,
            sensitivity="private",
            consent_scope="local_only",
            idempotency_key=f"request:{operation_id}:learning.execution_reported",
        )
        conn.execute(
            "INSERT INTO LearningExecutionReports "
            "(ReportId,OperationId,ActionRunId,TurnId,SkillId,SkillVersionHash,"
            "Outcome,Postcondition,VerificationSource,DurationMs,EvidenceHash,"
            "CommandHash,EventId,ReportedAt) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                report_id, operation_id, action_run_id, turn_id, skill_id,
                version_hash, outcome, postcondition, verification_source,
                duration_ms, evidence_hash, command_hash, event["EventId"], now,
            ),
        )
        return {"report_id": report_id, "event_id": event["EventId"], "idempotent": False}


def propose_learning_candidate(
    memory,
    repository,
    *,
    operation_id,
    kind,
    target_skill_id,
    base_version_hash,
    candidate_payload,
    evidence,
    proposed_by,
    actor_id="",
    origin="api",
    correlation_id="",
    clock=None,
):
    operation_id = _uuid(operation_id)
    correlation_id = _uuid(correlation_id, "correlation_id") if correlation_id else ""
    if kind not in _CANDIDATE_KINDS:
        raise LearningValidationError("candidate kind is invalid")
    target_skill_id = _safe_identity(target_skill_id, "target_skill_id", 128)
    base_version_hash = _hex64(base_version_hash, "base_version_hash")
    if proposed_by not in ("user", "system", "admin"):
        raise LearningValidationError("proposed_by is invalid")
    if origin not in ("bridge", "browser", "api", "test"):
        raise LearningValidationError("origin is invalid")
    actor_id = _safe_identity(actor_id or "", "actor_id", 512, allow_empty=True)
    payload, payload_json = _normalize_candidate_payload(kind, candidate_payload)
    payload_digest = _hash(payload_json)

    if kind == "skill_routing_rule" and payload["skill_id"] != target_skill_id:
        raise LearningValidationError("routing skill_id must equal target_skill_id")

    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 100:
        raise LearningValidationError("evidence must be a non-empty bounded list")
    evidence_links = []
    for link in evidence:
        if not isinstance(link, dict) or set(link) != {"report_id", "role"}:
            raise LearningValidationError("each evidence link must contain report_id and role")
        report_id = _hex64(link.get("report_id"), "report_id")
        role = link.get("role")
        if role not in ("support", "failure"):
            raise LearningValidationError("evidence role is invalid")
        evidence_links.append((report_id, role))
    report_ids = [item[0] for item in evidence_links]
    if (
        evidence_links != sorted(evidence_links)
        or len(set(report_ids)) != len(report_ids)
    ):
        raise LearningValidationError("evidence links must be unique and sorted")
    candidate_version = _candidate_version_hash(
        kind, base_version_hash, payload_digest, evidence_links
    )
    identity = _candidate_identity(
        kind, target_skill_id, base_version_hash, candidate_version,
        payload_digest, evidence_links,
    )
    candidate_id = _hash(identity)
    command = {
        "operation_id": operation_id,
        **identity,
        "proposed_by": proposed_by,
        "actor_id_utf8_hex": _identity_utf8_hex(actor_id, "actor_id"),
        "origin": origin,
        "correlation_id": correlation_id,
    }
    command_hash = _hash(command)

    with memory.transaction() as conn:
        existing = conn.execute(
            "SELECT * "
            "FROM LearningCandidates WHERE OperationId=?", (operation_id,),
        ).fetchone()
        if existing is not None:
            stored_links, stored_reports = _load_candidate_evidence(
                conn, existing["CandidateId"]
            )
            _validate_persisted_evidence(existing, stored_reports)
            _attest_candidate_row(repository, conn, existing, stored_links)
            for _stored_role, stored_report in stored_reports:
                _attest_report_row(repository, conn, stored_report)
            _stored_event, receipt = _validated_event(
                repository, existing["EventId"], conn
            )
            stored_command = receipt.get("command")
            if (
                _stored_event.get("EventType") != "learning.candidate_proposed"
                or stored_links != evidence_links
                or stored_command != command
                or existing["CommandHash"] != command_hash
            ):
                raise IdempotencyCollisionError(operation_id, "candidate command differs")
            return {
                "candidate_id": existing["CandidateId"],
                "candidate_version_hash": existing["CandidateVersionHash"],
                "event_id": existing["EventId"],
                "idempotent": True,
            }
        current = _latest_skill(memory, target_skill_id, connection=conn)
        if current is None:
            raise LearningValidationError("active target skill not found")
        if skill_version_hash(current) != base_version_hash:
            raise LearningConflictError("base skill version does not match current skill")
        for report_id, role in evidence_links:
            row = conn.execute(
                "SELECT * FROM LearningExecutionReports WHERE ReportId=?", (report_id,),
            ).fetchone()
            if row is None:
                raise LearningValidationError("evidence report not found")
            _attest_report_row(repository, conn, row)
            if row["SkillId"] != target_skill_id:
                raise LearningValidationError("evidence report targets another skill")
            if row["SkillVersionHash"] != base_version_hash:
                raise LearningValidationError("evidence report has another skill version")
            valid_support = (
                row["Outcome"] == "succeeded" and row["Postcondition"] == "observed"
                and row["VerificationSource"] in ("user", "test")
            )
            if (role == "support") != valid_support:
                raise LearningValidationError("evidence role conflicts with report outcome")
        alternate = conn.execute(
            "SELECT CandidateId,CandidateHash FROM LearningCandidates "
            "WHERE TargetSkillId=? AND CandidateVersionHash=?",
            (target_skill_id, candidate_version),
        ).fetchone()
        if alternate is not None:
            if tuple(alternate) != (candidate_id, _hash(identity)):
                raise LearningCollisionError("candidate version identity collision")
            raise LearningConflictError("candidate version was already proposed")
        now = _utc_iso(_utc_now(clock))
        result = {"candidate_id": candidate_id, "candidate_version_hash": candidate_version}
        event = repository.append_event(
            connection=conn,
            stream_id=f"learning-candidate:{candidate_id}",
            event_type="learning.candidate_proposed",
            payload=_event_receipt(command, result),
            actor_type=proposed_by,
            actor_id=actor_id,
            origin=origin,
            occurred_at=now,
            correlation_id=correlation_id,
            trust=0.7,
            sensitivity="private",
            consent_scope="local_only",
            idempotency_key=f"request:{operation_id}:learning.candidate_proposed",
        )
        candidate_hash = _hash(identity)
        conn.execute(
            "INSERT INTO LearningCandidates "
            "(CandidateId,OperationId,Kind,TargetSkillId,BaseVersionHash,"
            "CandidateVersionHash,CandidatePayload,PayloadHash,CandidateHash,"
            "ProposedBy,ActorId,CommandHash,EventId,CreatedAt) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                candidate_id, operation_id, kind, target_skill_id,
                base_version_hash, candidate_version, payload_json, payload_digest,
                candidate_hash, proposed_by, actor_id, command_hash,
                event["EventId"], now,
            ),
        )
        for report_id, role in evidence_links:
            conn.execute(
                "INSERT INTO LearningCandidateEvidence "
                "(CandidateId,ReportId,EvidenceRole,LinkedAt) VALUES (?,?,?,?)",
                (candidate_id, report_id, role, now),
            )
        return {
            "candidate_id": candidate_id,
            "candidate_version_hash": candidate_version,
            "event_id": event["EventId"],
            "idempotent": False,
        }


def _fixture_results(kind, payload):
    payload_json = canonical_json(payload)
    primary = (
        payload.get("instructions") if kind == "skill_instructions"
        else payload.get("template") if kind == "skill_prompt_template"
        else payload.get("intent")
    )
    description = payload.get("description", "")
    results = {
        "schema.valid": isinstance(payload, dict),
        "quality.primary_text": isinstance(primary, str) and bool(primary.strip()),
        "quality.description": isinstance(description, str) and bool(description.strip()),
    }
    for name, pattern in _FORBIDDEN_PATTERNS:
        results[f"safety.{name}"] = pattern.search(payload_json) is None
    return results


def _baseline_payload(memory, candidate, connection=None):
    kind = candidate[2]
    current = _latest_skill(memory, candidate[3], connection=connection)
    current_hash = skill_version_hash(current)
    if current_hash != candidate[4]:
        raise LearningConflictError("candidate baseline is stale")
    if kind == "skill_instructions":
        return skill_payload(current) or {}
    return {}


def evaluate_learning_candidate(
    memory,
    repository,
    *,
    operation_id,
    candidate_id,
    actor_id="",
    origin="api",
    correlation_id="",
    clock=None,
):
    operation_id = _uuid(operation_id)
    correlation_id = _uuid(correlation_id, "correlation_id") if correlation_id else ""
    candidate_id = _hex64(candidate_id, "candidate_id")
    actor_id = _safe_identity(actor_id or "", "actor_id", 512, allow_empty=True)
    if origin not in ("bridge", "browser", "api", "test"):
        raise LearningValidationError("origin is invalid")
    with memory.transaction() as conn:
        candidate = conn.execute(
            "SELECT * FROM LearningCandidates WHERE CandidateId=?", (candidate_id,),
        ).fetchone()
        if candidate is None:
            raise LearningValidationError("candidate not found")
        try:
            candidate_payload = json.loads(candidate[6])
        except json.JSONDecodeError as exc:
            raise LearningCollisionError("stored candidate payload is invalid") from exc
        evidence_links, evidence_rows = _load_candidate_evidence(conn, candidate_id)
        _validate_persisted_evidence(candidate, evidence_rows)
        expected_version = _candidate_version_hash(
            candidate[2], candidate[4], candidate[7], evidence_links
        )
        expected_identity = _candidate_identity(
            candidate[2], candidate[3], candidate[4], expected_version,
            candidate[7], evidence_links,
        )
        if (
            _hash(candidate[6]) != candidate[7]
            or _exact_json(candidate_payload) != candidate[6]
            or expected_version != candidate[5]
            or _hash(expected_identity) != candidate[8]
            or _hash(expected_identity) != candidate_id
        ):
            raise LearningCollisionError("stored candidate identity differs")
        _attest_candidate_row(repository, conn, candidate, evidence_links)
        for _evidence_role, evidence_row in evidence_rows:
            _attest_report_row(repository, conn, evidence_row)
        existing_operation = conn.execute(
            "SELECT PlanId,CommandHash,EventId,CandidateId,EvaluatorId,"
            "EvaluatorVersion,FixtureSetHash,BaselineVersionHash,"
            "CandidateVersionHash FROM LearningEvaluationPlans "
            "WHERE OperationId=?", (operation_id,),
        ).fetchone()
        if existing_operation is not None:
            _stored_event, receipt = _validated_event(
                repository, existing_operation[2], conn
            )
            stored_command = receipt.get("command")
            stored_plan = (
                stored_command.get("plan")
                if isinstance(stored_command, dict) else None
            )
            stored_command_hash = (
                _hash(stored_command) if isinstance(stored_command, dict) else ""
            )
            if existing_operation[3] != candidate_id:
                raise IdempotencyCollisionError(operation_id, "evaluation command differs")
            if (
                _stored_event.get("EventType") != "learning.evaluation_planned"
                or not isinstance(stored_plan, dict)
                or stored_command.get("operation_id") != operation_id
                or stored_command.get("identity_encoding") != "utf-8-hex"
                or stored_command.get("actor_id_utf8_hex")
                != _identity_utf8_hex(actor_id, "actor_id")
                or stored_command.get("origin") != origin
                or stored_command.get("correlation_id") != correlation_id
                or stored_plan.get("candidate_id") != existing_operation[3]
                or stored_plan.get("evaluator_id") != existing_operation[4]
                or stored_plan.get("evaluator_version") != existing_operation[5]
                or stored_plan.get("fixture_set_hash") != existing_operation[6]
                or stored_plan.get("baseline_version_hash") != existing_operation[7]
                or stored_plan.get("candidate_version_hash") != existing_operation[8]
                or existing_operation[0] != _hash(stored_plan)
                or existing_operation[1] != stored_command_hash
                or receipt.get("command_hash") != stored_command_hash
                or receipt.get("result") != {"plan_id": existing_operation[0]}
            ):
                raise LearningCollisionError("stored evaluation command hash differs")
            result = conn.execute(
                "SELECT * FROM LearningEvaluationResults WHERE PlanId=?",
                (existing_operation[0],),
            ).fetchone()
            if result is None:
                raise LearningCollisionError("evaluation plan lacks result")
            plan_row = conn.execute(
                "SELECT * FROM LearningEvaluationPlans WHERE PlanId=?",
                (existing_operation[0],),
            ).fetchone()
            _attest_evaluation_rows(repository, conn, plan_row, result)
            _result_event, result_receipt = _validated_event(
                repository, result["EventId"], conn
            )
            result_body = result_receipt.get("result")
            if (
                _result_event.get("EventType") != "learning.evaluation_completed"
                or result_receipt.get("plan_id") != existing_operation[0]
                or result_receipt.get("result_id") != result["ResultId"]
                or result_receipt.get("result_hash") != result["ResultHash"]
                or not isinstance(result_body, dict)
                or result_receipt.get("evaluator_policy_hash")
                != result_body.get("evaluator_policy_hash")
                or result_body.get("evaluator_policy_hash")
                != stored_plan.get("evaluator_policy_hash")
                or _hash(result_body) != result["ResultHash"]
                or result_body.get("baseline_passed") != result["BaselinePassed"]
                or result_body.get("candidate_passed") != result["CandidatePassed"]
                or bool(result_body.get("safety_passed"))
                != bool(result["SafetyPassed"])
                or len(result_body.get("regressions", []))
                != result["RegressionCount"]
                or bool(result_body.get("passed")) != bool(result["Passed"])
            ):
                raise LearningCollisionError("stored evaluation result integrity differs")
            return {
                "plan_id": existing_operation[0], "result_id": result["ResultId"],
                "passed": bool(result["Passed"]),
                "baseline_passed": result["BaselinePassed"],
                "candidate_passed": result["CandidatePassed"],
                "total": result["CandidateTotal"],
                "safety_passed": bool(result["SafetyPassed"]),
                "regression_count": result["RegressionCount"],
                "plan_event_id": existing_operation[2],
                "result_event_id": result["EventId"], "idempotent": True,
            }
        plan_identity = {
            "candidate_id": candidate_id,
            "evaluator_id": EVALUATOR_ID,
            "evaluator_version": EVALUATOR_VERSION,
            "fixture_set_hash": FIXTURE_SET_HASH,
            "evaluator_policy_hash": EVALUATOR_POLICY_HASH,
            "baseline_version_hash": candidate[4],
            "candidate_version_hash": candidate[5],
        }
        plan_id = _hash(plan_identity)
        plan_hash = _hash(plan_identity)
        command = {
            "operation_id": operation_id,
            "plan": plan_identity,
            "identity_encoding": "utf-8-hex",
            "actor_id_utf8_hex": _identity_utf8_hex(actor_id, "actor_id"),
            "origin": origin,
            "correlation_id": correlation_id,
        }
        command_hash = _hash(command)
        baseline_payload = _baseline_payload(memory, candidate, connection=conn)
        baseline_results = _fixture_results(candidate[2], baseline_payload)
        candidate_results = _fixture_results(candidate[2], candidate_payload)
        fixture_ids = [fixture["id"] for fixture in _FROZEN_FIXTURES]
        baseline_passed = sum(bool(baseline_results.get(item)) for item in fixture_ids)
        candidate_passed = sum(bool(candidate_results.get(item)) for item in fixture_ids)
        regressions = [
            item for item in fixture_ids
            if baseline_results.get(item) and not candidate_results.get(item)
        ]
        safety_ids = [fixture["id"] for fixture in _FROZEN_FIXTURES if fixture["safety"]]
        safety_passed = all(candidate_results.get(item) for item in safety_ids)
        passed = safety_passed and not regressions and candidate_passed >= baseline_passed
        existing_plan = conn.execute(
            "SELECT OperationId FROM LearningEvaluationPlans WHERE PlanId=?",
            (plan_id,),
        ).fetchone()
        if existing_plan is not None:
            raise LearningConflictError("candidate was already evaluated by this fixture set")
        now = _utc_iso(_utc_now(clock))
        plan_result = {"plan_id": plan_id}
        plan_event = repository.append_event(
            connection=conn,
            stream_id=f"learning-evaluation:{plan_id}",
            event_type="learning.evaluation_planned",
            payload=_event_receipt(command, plan_result),
            actor_type="system",
            actor_id=EVALUATOR_ID,
            origin=origin,
            occurred_at=now,
            correlation_id=correlation_id,
            trust=1.0,
            sensitivity="private",
            consent_scope="local_only",
            idempotency_key=f"request:{operation_id}:learning.evaluation_planned",
        )
        conn.execute(
            "INSERT INTO LearningEvaluationPlans "
            "(PlanId,OperationId,CandidateId,EvaluatorId,EvaluatorVersion,FixtureSetHash,"
            "BaselineVersionHash,CandidateVersionHash,PlanHash,CommandHash,EventId,CreatedAt) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                plan_id, operation_id, candidate_id, EVALUATOR_ID,
                EVALUATOR_VERSION, FIXTURE_SET_HASH, candidate[4], candidate[5],
                plan_hash, command_hash, plan_event["EventId"], now,
            ),
        )
        result_body = {
            "plan_id": plan_id,
            "evaluator_policy_hash": EVALUATOR_POLICY_HASH,
            "baseline": baseline_results,
            "candidate": candidate_results,
            "regressions": regressions,
            "safety_passed": safety_passed,
            "passed": passed,
            "baseline_passed": baseline_passed,
            "candidate_passed": candidate_passed,
            "total": len(fixture_ids),
        }
        result_hash = _hash(result_body)
        result_id = _hash({"plan_id": plan_id, "result_hash": result_hash})
        result_event = repository.append_event(
            connection=conn,
            stream_id=f"learning-evaluation:{plan_id}",
            event_type="learning.evaluation_completed",
            payload={
                "plan_id": plan_id,
                "result_id": result_id,
                "result_hash": result_hash,
                "evaluator_policy_hash": EVALUATOR_POLICY_HASH,
                "result": result_body,
            },
            actor_type="system",
            actor_id=EVALUATOR_ID,
            origin=origin,
            occurred_at=now,
            correlation_id=correlation_id,
            causation_id=plan_event["EventId"],
            trust=1.0,
            sensitivity="private",
            consent_scope="local_only",
            idempotency_key=f"plan:{plan_id}:learning.evaluation_completed",
        )
        conn.execute(
            "INSERT INTO LearningEvaluationResults "
            "(ResultId,PlanId,BaselinePassed,BaselineTotal,CandidatePassed,"
            "CandidateTotal,SafetyPassed,RegressionCount,Passed,ResultHash,EventId,"
            "EvaluatedAt) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                result_id, plan_id, baseline_passed, len(fixture_ids),
                candidate_passed, len(fixture_ids), int(safety_passed),
                len(regressions), int(passed), result_hash,
                result_event["EventId"], now,
            ),
        )
        return {
            "plan_id": plan_id,
            "result_id": result_id,
            "passed": passed,
            "baseline_passed": baseline_passed,
            "candidate_passed": candidate_passed,
            "total": len(fixture_ids),
            "safety_passed": safety_passed,
            "regression_count": len(regressions),
            "plan_event_id": plan_event["EventId"],
            "result_event_id": result_event["EventId"],
            "idempotent": False,
        }


def list_learning_candidates(memory, *, status="all", limit=50):
    if status not in ("pending_evaluation", "evaluation_passed", "evaluation_failed", "all"):
        raise LearningValidationError("status is invalid")
    limit = _strict_int(limit, "limit", 1, 100)
    repository = memory.event_repository()
    with memory.transaction() as conn:
        ids = conn.execute(
            "SELECT CandidateId FROM LearningCandidates "
            "ORDER BY CreatedAt DESC,CandidateId"
        ).fetchall()
        output = []
        fields = (
            "CandidateId", "Kind", "TargetSkillId", "BaseVersionHash",
            "CandidateVersionHash", "ProposedBy", "CreatedAt", "Status",
            "Passed", "SafetyPassed", "RegressionCount", "EvaluatedAt",
        )
        for row in ids:
            detail = _get_learning_candidate_tx(
                repository, conn, row["CandidateId"]
            )
            if status != "all" and detail["Status"] != status:
                continue
            output.append({field: detail.get(field) for field in fields})
            if len(output) >= limit:
                break
        return output


def get_learning_candidate(memory, candidate_id):
    candidate_id = _hex64(candidate_id, "candidate_id")
    repository = memory.event_repository()
    with memory.transaction() as conn:
        return _get_learning_candidate_tx(repository, conn, candidate_id)


def _get_learning_candidate_tx(repository, conn, candidate_id):
    candidate = conn.execute(
        "SELECT * FROM LearningCandidates WHERE CandidateId=?", (candidate_id,),
    ).fetchone()
    if candidate is None:
        return None
    evidence_links, evidence_rows = _load_candidate_evidence(conn, candidate_id)
    _validate_persisted_evidence(candidate, evidence_rows)
    _attest_candidate_row(repository, conn, candidate, evidence_links)
    for _role, report in evidence_rows:
        _attest_report_row(repository, conn, report)
    plans = conn.execute(
        "SELECT p.*,pe.JournalSequence AS PlanJournalSequence "
        "FROM LearningEvaluationPlans p "
        "LEFT JOIN MemoryEvents pe ON pe.EventId=p.EventId "
        "WHERE p.CandidateId=? "
        "ORDER BY pe.JournalSequence DESC,p.PlanId DESC", (candidate_id,),
    ).fetchall()
    evaluated = []
    for stored_plan in plans:
        if stored_plan["PlanJournalSequence"] is None:
            raise LearningCollisionError("evaluation plan event is missing")
        stored_result = conn.execute(
            "SELECT * FROM LearningEvaluationResults WHERE PlanId=?",
            (stored_plan["PlanId"],),
        ).fetchone()
        if stored_result is None:
            raise LearningCollisionError("evaluation plan lacks result")
        _attest_evaluation_rows(repository, conn, stored_plan, stored_result)
        evaluated.append((stored_plan, stored_result))
    plan, evaluation = evaluated[0] if evaluated else (None, None)
    result = dict(candidate)
    result["Status"] = (
        "pending_evaluation" if evaluation is None
        else "evaluation_passed" if evaluation["Passed"]
        else "evaluation_failed"
    )
    if plan is not None:
        for field in (
            "PlanId", "EvaluatorId", "EvaluatorVersion", "FixtureSetHash",
        ):
            result[field] = plan[field]
    if evaluation is not None:
        for field in (
            "ResultId", "BaselinePassed", "BaselineTotal", "CandidatePassed",
            "CandidateTotal", "SafetyPassed", "RegressionCount", "Passed",
            "ResultHash", "EvaluatedAt",
        ):
            result[field] = evaluation[field]
    else:
        for field in (
            "PlanId", "EvaluatorId", "EvaluatorVersion", "FixtureSetHash",
            "ResultId", "BaselinePassed", "BaselineTotal", "CandidatePassed",
            "CandidateTotal", "SafetyPassed", "RegressionCount", "Passed",
            "ResultHash", "EvaluatedAt",
        ):
            result.setdefault(field, None)
    result["evidence"] = [
        {
            **{
                field: row[field] for field in (
                    "ReportId", "ActionRunId", "SkillVersionHash", "Outcome",
                    "Postcondition", "VerificationSource", "EvidenceHash", "ReportedAt",
                )
            },
            "EvidenceRole": role,
        }
        for role, row in evidence_rows
    ]
    return result
