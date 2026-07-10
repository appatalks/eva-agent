"""Event-first, exactly-once memory mutation and turn finalization."""

import datetime
import json
import re

from bridge.events import IdempotencyCollisionError, deterministic_source_message_id
from bridge.sensitive import default_conversation_consent, redact_credentials


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _payload(event):
    value = event.get("Payload", "{}")
    return json.loads(value) if isinstance(value, str) else value


def _project_once(mem, repo, conn, event, projection, table, columns, rows):
    if repo.has_legacy_receipt(event["EventId"], projection, connection=conn):
        return 0
    count = mem.insert_rows(conn, table, columns, rows)
    repo.record_legacy_receipt(
        event["EventId"], projection, count, connection=conn
    )
    return count


def _emotion_from_turn(user_message, assistant_message):
    pos = len(re.findall(
        r"\b(happy|great|excellent|wonderful|love|enjoy|glad|excited|amazing|good|thank)\b",
        assistant_message,
        re.I,
    ))
    neg = len(re.findall(
        r"\b(sorry|error|fail|wrong|bad|unfortunately|cannot|problem|issue)\b",
        assistant_message,
        re.I,
    ))
    return {
        "Joy": round(max(0.0, min(1.0, 0.5 + (pos - neg) * 0.1)), 3),
        "Curiosity": round(min(1.0, 0.6 + (0.1 if "?" in user_message else 0.0)), 3),
        "Concern": round(min(1.0, 0.2 + neg * 0.15), 3),
        "Excitement": 0.4,
        "Calm": 0.9,
        "Empathy": 0.6,
        "Trigger": user_message[:100],
        "DecayRate": 0.1,
    }


def finalize_turn(
    mem,
    repo,
    *,
    session_id,
    turn_id,
    user_message,
    assistant_message,
    model,
    correlation_id="",
    actor_id="",
    origin="bridge",
    extract_facts_fn=None,
    extract_candidates_fn=None,
):
    """Durably finalize one logical turn in one SQLite transaction.

    Event rows, outbox rows, legacy projections, and receipts either all commit
    or all roll back. A duplicate/concurrent call with the same turn is a no-op.
    """
    if not session_id or not turn_id:
        raise ValueError("session_id and turn_id are required")
    safe_user = redact_credentials(str(user_message or "")[:16000])
    safe_assistant = redact_credentials(str(assistant_message or "")[:16000])
    now = _utc_now()
    stream = f"conversation:{session_id}"
    user_message_id = deterministic_source_message_id(turn_id, "user")
    assistant_message_id = deterministic_source_message_id(turn_id, "assistant")

    with mem.transaction() as conn:
        finalized = repo.events_for_turn(turn_id, connection=conn)
        if finalized:
            user_existing = next(
                (event for event in finalized if event.get("EventType") == "conversation.user_observed"),
                None,
            )
            assistant_existing = next(
                (event for event in finalized if event.get("EventType") == "conversation.assistant_observed"),
                None,
            )
            if not user_existing or not assistant_existing:
                raise IdempotencyCollisionError(turn_id, "turn has incomplete durable events")
            user_payload = _payload(user_existing)
            assistant_payload = _payload(assistant_existing)
            if (
                user_existing.get("SessionId") != session_id
                or user_payload.get("content") != safe_user
                or assistant_payload.get("content") != safe_assistant
                or user_payload.get("model") != model
                or assistant_payload.get("model") != model
            ):
                raise IdempotencyCollisionError(
                    turn_id, "turn replay differs from the previously finalized content"
                )
            return {
                "event_ids": [event["EventId"] for event in finalized],
                "session_id": session_id,
                "turn_id": turn_id,
                "exchange_ordinal": int(user_payload.get("exchange_ordinal", 0)),
                "idempotent": True,
            }

        existing = repo.list_stream(stream, connection=conn)
        prior_assistant = sum(
            1 for event in existing
            if event.get("EventType") == "conversation.assistant_observed"
            and event.get("TurnId") != turn_id
        )
        exchange_ordinal = prior_assistant
        exchange_number = exchange_ordinal + 1

        user_event = repo.append_event(
            connection=conn,
            stream_id=stream,
            event_type="conversation.user_observed",
            payload={
                "role": "user", "content": safe_user, "model": model,
                "token_estimate": len(str(user_message or "").split()),
                "exchange_ordinal": exchange_ordinal,
            },
            actor_type="user", actor_id=actor_id, origin=origin,
            occurred_at=now, correlation_id=correlation_id,
            session_id=session_id, turn_id=turn_id,
            source_message_id=user_message_id, trust=1.0,
            sensitivity="private", consent_scope=default_conversation_consent(),
            idempotency_key=f"turn:{turn_id}:user",
        )
        assistant_event = repo.append_event(
            connection=conn,
            stream_id=stream,
            event_type="conversation.assistant_observed",
            payload={
                "role": "assistant", "content": safe_assistant, "model": model,
                "token_estimate": len(str(assistant_message or "").split()),
                "exchange_ordinal": exchange_ordinal,
            },
            actor_type="system", actor_id="eva", origin=origin,
            occurred_at=now, correlation_id=correlation_id,
            causation_id=user_event["EventId"], session_id=session_id,
            turn_id=turn_id, source_message_id=assistant_message_id,
            trust=0.9, sensitivity="private",
            consent_scope=default_conversation_consent(),
            idempotency_key=f"turn:{turn_id}:assistant",
        )

        conversation_columns = [
            "SessionId", "Timestamp", "Role", "Provider", "Model", "Content",
            "TokenEstimate", "ImageGenerated",
        ]
        _project_once(mem, repo, conn, user_event, "conversations:user", "Conversations", conversation_columns, [{
            "SessionId": session_id, "Timestamp": now, "Role": "user",
            "Provider": "bridge", "Model": model, "Content": safe_user,
            "TokenEstimate": len(str(user_message or "").split()), "ImageGenerated": 0,
        }])
        _project_once(mem, repo, conn, assistant_event, "conversations:assistant", "Conversations", conversation_columns, [{
            "SessionId": session_id, "Timestamp": now, "Role": "assistant",
            "Provider": "bridge", "Model": model, "Content": safe_assistant,
            "TokenEstimate": len(str(assistant_message or "").split()), "ImageGenerated": 0,
        }])

        events = [user_event, assistant_event]
        facts = extract_facts_fn(user_message) if extract_facts_fn else []
        for index, fact in enumerate(facts or []):
            fact_payload = {
                "entity": str(fact.get("Entity", "User"))[:200],
                "relation": str(fact.get("Relation", ""))[:200],
                "value": redact_credentials(str(fact.get("Value", ""))[:200]),
                "confidence": float(fact.get("Confidence", 0.5)),
                "extraction_method": "explicit_regex",
                "confidence_source": "pattern_match",
                "evidence_source_message_id": user_message_id,
                "provisional": True,
            }
            event = repo.append_event(
                connection=conn,
                stream_id=f"knowledge:{fact_payload['entity']}",
                event_type="memory.fact_candidate_extracted",
                payload=fact_payload,
                actor_type="system", actor_id="eva", origin=origin,
                occurred_at=now, correlation_id=correlation_id,
                causation_id=user_event["EventId"], session_id=session_id,
                turn_id=turn_id, source_message_id=user_message_id,
                trust=max(0.0, min(1.0, fact_payload["confidence"])),
                sensitivity="private", consent_scope="local_only",
                idempotency_key=f"turn:{turn_id}:fact:{index}",
            )
            _project_once(mem, repo, conn, event, "knowledge", "Knowledge", [
                "Timestamp", "Entity", "Relation", "Value", "Confidence", "Source", "Decay",
            ], [{
                "Timestamp": now, "Entity": fact_payload["entity"],
                "Relation": fact_payload["relation"], "Value": fact_payload["value"],
                "Confidence": fact_payload["confidence"],
                "Source": f"event:{event['EventId']}", "Decay": 0.005,
            }])
            events.append(event)

        candidates = extract_candidates_fn(user_message) if extract_candidates_fn else []
        if isinstance(candidates, tuple):
            candidates = candidates[0]
        for index, candidate in enumerate((candidates or [])[:3]):
            entity = str(candidate)[:200]
            entity_stream = f"entity:{entity.lower()}"
            prior = [
                event for event in repo.list_stream(entity_stream, connection=conn)
                if event.get("TurnId") != turn_id
            ]
            confidence = 0.2 if not prior else min(0.75, 0.55 + len(prior) * 0.1)
            relation = "candidate_mentioned" if confidence < 0.6 else "recurring_topic"
            event = repo.append_event(
                connection=conn,
                stream_id=entity_stream,
                event_type="memory.entity_observed",
                payload={
                    "entity": entity, "relation": relation,
                    "value": "observed in user conversation", "confidence": confidence,
                    "evidence_source_message_id": user_message_id,
                },
                actor_type="system", actor_id="eva", origin=origin,
                occurred_at=now, correlation_id=correlation_id,
                causation_id=user_event["EventId"], session_id=session_id,
                turn_id=turn_id, source_message_id=user_message_id,
                trust=confidence, sensitivity="private", consent_scope="local_only",
                idempotency_key=f"turn:{turn_id}:entity:{index}",
            )
            _project_once(mem, repo, conn, event, "knowledge", "Knowledge", [
                "Timestamp", "Entity", "Relation", "Value", "Confidence", "Source", "Decay",
            ], [{
                "Timestamp": now, "Entity": entity, "Relation": relation,
                "Value": "observed in user conversation", "Confidence": confidence,
                "Source": f"event:{event['EventId']}", "Decay": 0.02,
            }])
            _project_once(mem, repo, conn, event, "heuristics", "HeuristicsIndex", [
                "Entity", "Category", "LastSeen", "Frequency", "Sentiment", "Tags", "Context",
            ], [{
                "Entity": entity, "Category": relation, "LastSeen": now,
                "Frequency": len(prior) + 1, "Sentiment": 0.0, "Tags": "[]",
                "Context": "observed in user conversation",
            }])
            events.append(event)

        emotion = _emotion_from_turn(safe_user, safe_assistant)
        emotion_event = repo.append_event(
            connection=conn,
            stream_id=f"emotion:{session_id}",
            event_type="emotion.observed",
            payload=emotion,
            actor_type="system", actor_id="eva", origin=origin,
            occurred_at=now, correlation_id=correlation_id,
            causation_id=assistant_event["EventId"], session_id=session_id,
            turn_id=turn_id, trust=0.5, sensitivity="private",
            consent_scope="local_only", idempotency_key=f"turn:{turn_id}:emotion",
        )
        _project_once(mem, repo, conn, emotion_event, "emotion_state", "EmotionState", [
            "Timestamp", "Joy", "Curiosity", "Concern", "Excitement", "Calm",
            "Empathy", "Trigger", "DecayRate",
        ], [dict({"Timestamp": now}, **emotion)])
        events.append(emotion_event)

        significant = (
            len(str(assistant_message or "")) > 800 or len(candidates or []) >= 2
            or abs(emotion["Joy"] - 0.5) > 0.2 or emotion["Concern"] > 0.5
        )
        if exchange_number % 5 == 0 or significant:
            observation = (
                f"Exchange #{exchange_number}: processed a user turn with "
                f"Joy:{emotion['Joy']:.2f}, Concern:{emotion['Concern']:.2f}."
            )
            reflection_event = repo.append_event(
                connection=conn,
                stream_id=f"reflection:{session_id}",
                event_type="reflection.generated",
                payload={
                    "trigger": safe_user[:100], "observation": observation,
                    "exchange_number": exchange_number, "significant": significant,
                },
                actor_type="system", actor_id="eva", origin=origin,
                occurred_at=now, correlation_id=correlation_id,
                causation_id=assistant_event["EventId"], session_id=session_id,
                turn_id=turn_id, trust=0.5, sensitivity="private",
                consent_scope="local_only", idempotency_key=f"turn:{turn_id}:reflection",
            )
            _project_once(mem, repo, conn, reflection_event, "reflections", "Reflections", [
                "Timestamp", "Trigger", "Observation", "ActionTaken", "Effectiveness",
            ], [{
                "Timestamp": now, "Trigger": safe_user[:100],
                "Observation": observation, "ActionTaken": "", "Effectiveness": 0.0,
            }])
            events.append(reflection_event)

        if exchange_number % 10 == 0:
            summary = f"Session {session_id}: {exchange_number} completed exchanges."
            summary_event = repo.append_event(
                connection=conn,
                stream_id=f"summary:{session_id}",
                event_type="memory.summary_generated",
                payload={"period": f"session:{session_id}:{exchange_number}", "summary": summary},
                actor_type="system", actor_id="eva", origin=origin,
                occurred_at=now, correlation_id=correlation_id,
                causation_id=assistant_event["EventId"], session_id=session_id,
                turn_id=turn_id, trust=0.6, sensitivity="private",
                consent_scope="local_only", idempotency_key=f"turn:{turn_id}:summary",
            )
            _project_once(mem, repo, conn, summary_event, "memory_summaries", "MemorySummaries", [
                "Period", "Summary", "Timestamp",
            ], [{
                "Period": f"session:{session_id}:{exchange_number}",
                "Summary": summary, "Timestamp": now,
            }])
            events.append(summary_event)

        return {
            "event_ids": [event["EventId"] for event in events],
            "session_id": session_id,
            "turn_id": turn_id,
            "exchange_ordinal": exchange_ordinal,
        }


def mutate_event(
    mem,
    repo,
    *,
    stream_id,
    event_type,
    payload,
    session_id="",
    turn_id="",
    correlation_id="",
    actor_type="system",
    actor_id="",
    origin="bridge",
    trust=0.5,
    sensitivity="normal",
    consent_scope="local_only",
    idempotency_key=None,
    legacy_table=None,
    legacy_columns=None,
    legacy_row=None,
    projection_name=None,
    projection_destination=None,
):
    """Append one event and its optional legacy projection atomically."""
    safe_payload = redact_credentials(payload)
    safe_legacy = redact_credentials(legacy_row) if legacy_row else None
    with mem.transaction() as conn:
        event = repo.append_event(
            connection=conn,
            stream_id=stream_id, event_type=event_type, payload=safe_payload,
            session_id=session_id, turn_id=turn_id,
            correlation_id=correlation_id, actor_type=actor_type,
            actor_id=actor_id, origin=origin, trust=trust,
            sensitivity=sensitivity, consent_scope=consent_scope,
            idempotency_key=idempotency_key,
        )
        if projection_destination:
            repo.ensure_outbox(
                event["EventId"], projection_destination, connection=conn
            )
        if legacy_table and legacy_columns and safe_legacy is not None:
            _project_once(
                mem, repo, conn, event, projection_name or legacy_table.lower(),
                legacy_table, legacy_columns, [safe_legacy],
            )
        elif not repo.has_legacy_receipt(event["EventId"], "none", connection=conn):
            repo.record_legacy_receipt(event["EventId"], "none", 0, connection=conn)
        return event
