#!/usr/bin/env python3
"""Focused contracts for the canonical Skills catalog and Category migration."""

import json
import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))

from generate_skill_seed import check_seed, load_manifest, render_block
from sqlite_memory import SqliteMemory, _load_default_skill_rows


def test_manifest_and_kusto_projection():
    manifest = load_manifest()
    assert len(manifest["skills"]) == 15
    assert {skill["category"] for skill in manifest["skills"]} <= set(manifest["categories"])
    rows = _load_default_skill_rows()
    assert len(rows) == 15
    assert all(row["Source"] == "seed" and row["Status"] == "active" for row in rows)
    weather = next(row for row in rows if row["SkillId"] == "skill-weather")
    config = json.loads(weather["Config"])
    assert config["defaults"]["default_location"] == ""
    assert any("web search" in item.lower() for item in config["allowed_fallbacks"])
    bounded = {skill["id"]: skill for skill in manifest["skills"]}
    assert {"skill-docx", "skill-pdf", "skill-pptx", "skill-xlsx", "skill-mcp-builder"} <= set(bounded)
    assert all("provenance" in bounded[item] for item in ("skill-docx", "skill-pdf", "skill-pptx", "skill-xlsx", "skill-mcp-builder"))
    assert bounded["skill-mcp-builder"]["license"] == "Apache-2.0"
    check_seed(render_block(manifest))


def test_malformed_manifest_fails_actionably():
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as stream:
        json.dump({"schema_version": 1, "categories": [], "skills": []}, stream)
        path = stream.name
    try:
        with patch("sqlite_memory._default_skill_manifest_path", return_value=path):
            try:
                _load_default_skill_rows()
            except RuntimeError as error:
                assert "categories do not match" in str(error)
            else:
                raise AssertionError("malformed manifest was accepted")
    finally:
        os.unlink(path)


def test_existing_skills_table_gets_category_and_defaults_are_seeded():
    with tempfile.TemporaryDirectory(prefix="eva-skills-catalog-") as directory:
        database = os.path.join(directory, "memory.db")
        connection = sqlite3.connect(database)
        connection.execute(
            "CREATE TABLE Skills (SkillId TEXT NOT NULL, Name TEXT NOT NULL, Description TEXT, "
            "Instructions TEXT, Tools TEXT, Tags TEXT, Source TEXT, Status TEXT, CreatedAt TEXT, UpdatedAt TEXT)"
        )
        connection.execute(
            "INSERT INTO Skills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("legacy-skill", "Legacy", "Old", "Do the old thing", "", "", "legacy", "active", "", ""),
        )
        connection.commit()
        connection.close()

        memory = SqliteMemory(database)
        legacy = memory.query("SELECT Category FROM Skills WHERE SkillId = 'legacy-skill'")
        assert legacy == [{"Category": "Uncategorized"}], legacy
        seeded = memory.query("SELECT SkillId, Category FROM Skills WHERE Source = 'seed'")
        assert len(seeded) == 15, seeded
        assert "Uncategorized" not in {row["Category"] for row in seeded}
        migration = memory.query("SELECT MigrationId FROM MemoryMigrations WHERE MigrationId = 'skills-category-v1'")
        assert migration == [{"MigrationId": "skills-category-v1"}], migration
        config_migration = memory.query("SELECT MigrationId FROM MemoryMigrations WHERE MigrationId = 'skills-config-v1'")
        assert config_migration == [{"MigrationId": "skills-config-v1"}], config_migration
        assert memory.query("SELECT Config FROM Skills WHERE SkillId = 'legacy-skill'")[0]["Config"] == "{}"


if __name__ == "__main__":
    test_manifest_and_kusto_projection()
    test_malformed_manifest_fails_actionably()
    test_existing_skills_table_gets_category_and_defaults_are_seeded()
    print("Skills catalog tests: PASS")