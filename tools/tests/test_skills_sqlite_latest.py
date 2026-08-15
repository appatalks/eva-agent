#!/usr/bin/env python3
"""SQLite Skill listing returns current state, not append-only history."""
import os
import sys
import tempfile
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))

from bridge.core import _sqlite_latest_skill_rows
from bridge.cognition import _active_skill_rows_for_decision
from sqlite_memory import SqliteMemory


COLUMNS = [
    "SkillId", "Name", "Description", "Category", "Instructions", "Tools",
    "Tags", "Source", "Status", "CreatedAt", "UpdatedAt",
]


def row(skill_id, status, updated_at, name="Playlist"):
    return {
        "SkillId": skill_id,
        "Name": name,
        "Description": "Play the saved playlist.",
        "Category": "Documents & Data",
        "Instructions": "Open the exact saved URL.",
        "Tools": "eva_harness.open_external_url",
        "Tags": "playlist",
        "Source": "voice",
        "Status": status,
        "CreatedAt": "2026-08-15T00:00:00Z",
        "UpdatedAt": updated_at,
    }


def main():
    with tempfile.TemporaryDirectory(prefix="eva-skills-override-") as directory:
        database = os.path.join(directory, "memory.db")
        memory = SqliteMemory(database)
        custom_weather = "Use Seattle as the default location when the request does not specify one."
        memory.transaction(lambda connection: connection.execute(
            "UPDATE Skills SET Instructions = ?, Source = 'user-override' WHERE SkillId = 'skill-weather'",
            (custom_weather,),
        ))

        restarted = SqliteMemory(database)
        weather = restarted.query(
            "SELECT Instructions, Source FROM Skills WHERE SkillId = 'skill-weather' "
            "ORDER BY UpdatedAt DESC, rowid DESC LIMIT 1"
        )[0]
        assert weather["Instructions"] == custom_weather, weather
        assert weather["Source"] == "user-override", weather

    with tempfile.TemporaryDirectory(prefix="eva-skills-latest-") as directory:
        memory = SqliteMemory(os.path.join(directory, "memory.db"))
        memory.transaction(lambda connection: connection.execute("DELETE FROM Skills"))
        memory.ingest("Skills", COLUMNS, [row("sk-playlist", "draft", "2026-08-15T00:00:00Z")])
        memory.ingest("Skills", COLUMNS, [row("sk-playlist", "active", "2026-08-15T00:01:00Z")])
        memory.ingest("Skills", COLUMNS, [row("sk-other", "active", "2026-08-15T00:02:00Z", "Other")])
        memory.ingest("Skills", COLUMNS, [row("sk-tie", "draft", "2026-08-15T00:04:00Z", "Tie")])
        memory.ingest("Skills", COLUMNS, [row("sk-tie", "active", "2026-08-15T00:04:00Z", "Tie")])
        latest = _sqlite_latest_skill_rows(memory)
        assert len(latest) == 3, latest
        playlist = next(item for item in latest if item["SkillId"] == "sk-playlist")
        assert playlist["Status"] == "active", playlist
        tied = next(item for item in latest if item["SkillId"] == "sk-tie")
        assert tied["Status"] == "active", tied
        with patch("bridge.cognition._resolve_memory_backend", return_value="sqlite"), \
                patch("bridge.cognition._get_sqlite_mem", return_value=memory):
            decision_rows = _active_skill_rows_for_decision()
        assert {item["SkillId"] for item in decision_rows} == {"sk-playlist", "sk-other", "sk-tie"}, decision_rows

        memory.ingest("Skills", COLUMNS, [row("sk-playlist", "deleted", "2026-08-15T00:03:00Z")])
        remaining = _sqlite_latest_skill_rows(memory)
        assert {item["SkillId"] for item in remaining} == {"sk-tie", "sk-other"}, remaining
        memory.ingest("Skills", COLUMNS, [row("sk-tie", "deleted", "2026-08-15T00:04:00Z", "Tie")])
        after_tied_delete = _sqlite_latest_skill_rows(memory)
        assert [item["SkillId"] for item in after_tied_delete] == ["sk-other"], after_tied_delete

    print("SQLite latest Skill listing tests: PASS")


if __name__ == "__main__":
    main()
