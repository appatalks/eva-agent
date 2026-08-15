#!/usr/bin/env python3
"""Generate and validate the canonical Skills block in eva_seed.kql."""

import argparse
import csv
import io
import json
import os
import re
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_PATH = os.path.join(ROOT, "docs", "eva_default_skills", "manifest.json")
SEED_PATH = os.path.join(ROOT, "tools", "eva_seed.kql")
BEGIN = "// BEGIN GENERATED DEFAULT SKILLS"
END = "// END GENERATED DEFAULT SKILLS"
PRIMARY_CATEGORIES = {
    "Information & Research",
    "Documents & Data",
    "Development & Integrations",
    "Browser & Desktop Automation",
    "Vision & Media",
    "Communication",
    "Memory & Personalization",
    "Uncategorized",
}
REQUIRED_FIELDS = {
    "id", "name", "description", "category", "trigger_examples", "instructions",
    "preferred_tools", "allowed_fallbacks", "prerequisites", "configurable_defaults",
    "source", "license", "version",
}
OPTIONAL_FIELDS = {"provenance"}


def load_manifest():
    try:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as stream:
            manifest = json.load(stream)
    except (OSError, ValueError) as exc:
        raise ValueError("could not read " + MANIFEST_PATH + ": " + str(exc)) from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")
    categories = manifest.get("categories")
    if not isinstance(categories, list) or set(categories) != PRIMARY_CATEGORIES or len(categories) != len(PRIMARY_CATEGORIES):
        raise ValueError("categories do not match the primary taxonomy")
    skills = manifest.get("skills")
    if not isinstance(skills, list) or len(skills) != 15:
        raise ValueError("the canonical catalog must contain exactly 15 default skills")
    ids = set()
    for index, skill in enumerate(skills, 1):
        if not isinstance(skill, dict) or not REQUIRED_FIELDS.issubset(set(skill)) or set(skill) - REQUIRED_FIELDS - OPTIONAL_FIELDS:
            raise ValueError("skill " + str(index) + " has invalid fields")
        skill_id = str(skill["id"]).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", skill_id) or skill_id in ids:
            raise ValueError("skill ids must be unique and safe: " + skill_id)
        if skill["category"] not in PRIMARY_CATEGORIES:
            raise ValueError("invalid category for " + skill_id)
        if not str(skill["name"]).strip() or not str(skill["description"]).strip() or not str(skill["instructions"]).strip():
            raise ValueError("name, description, and instructions are required for " + skill_id)
        for field in ("trigger_examples", "preferred_tools", "allowed_fallbacks", "prerequisites"):
            if not isinstance(skill[field], list) or not skill[field] or any(not str(item).strip() for item in skill[field]):
                raise ValueError(field + " must be a non-empty list for " + skill_id)
        if not isinstance(skill["configurable_defaults"], dict):
            raise ValueError("configurable_defaults must be an object for " + skill_id)
        ids.add(skill_id)
    return manifest


def _kql_csv_value(value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return str(value).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")


def render_block(manifest):
    output = io.StringIO()
    output.write(BEGIN + "\n")
    output.write("// Generated from docs/eva_default_skills/manifest.json; do not edit this block.\n")
    output.write(".create-merge table Skills (\n")
    output.write("    SkillId: string,\n")
    output.write("    Name: string,\n")
    output.write("    Description: string,\n")
    output.write("    Category: string,\n")
    output.write("    Instructions: string,\n")
    output.write("    Tools: string,\n")
    output.write("    Tags: string,\n")
    output.write("    Config: dynamic,\n")
    output.write("    Source: string,\n")
    output.write("    Status: string,\n")
    output.write("    CreatedAt: datetime,\n")
    output.write("    UpdatedAt: datetime\n")
    output.write(")\n\n")
    mapping = [
        {"column": column, "Properties": {"Ordinal": str(index)}}
        for index, column in enumerate((
            "SkillId", "Name", "Description", "Category", "Instructions", "Tools",
            "Tags", "Config", "Source", "Status", "CreatedAt", "UpdatedAt",
        ))
    ]
    output.write(".create-or-alter table Skills ingestion csv mapping \"SkillsDefaultSeedMapping\" '")
    output.write(json.dumps(mapping, separators=(",", ":")))
    output.write("'\n\n")
    output.write(".ingest inline into table Skills with (format='csv', ingestionMappingReference='SkillsDefaultSeedMapping') <|\n")
    writer = csv.writer(output, lineterminator="\n")
    for skill in manifest["skills"]:
        writer.writerow([
            _kql_csv_value(skill["id"]),
            _kql_csv_value(skill["name"]),
            _kql_csv_value(skill["description"]),
            _kql_csv_value(skill["category"]),
            _kql_csv_value(skill["instructions"]),
            _kql_csv_value(",".join(str(item).strip() for item in skill["preferred_tools"])),
            _kql_csv_value(",".join(str(item).strip() for item in skill["trigger_examples"])),
            _kql_csv_value({
                "defaults": skill["configurable_defaults"],
                "allowed_fallbacks": skill["allowed_fallbacks"],
            }),
            "seed",
            "active",
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
        ])
    output.write(END + "\n")
    return output.getvalue()


def update_seed(block):
    with open(SEED_PATH, "r", encoding="utf-8") as stream:
        seed = stream.read()
    pattern = re.escape(BEGIN) + r"\n.*?" + re.escape(END) + r"\n?"
    if not re.search(pattern, seed, flags=re.DOTALL):
        raise ValueError("generated Skills markers are missing from " + SEED_PATH)
    updated = re.sub(pattern, lambda _match: block, seed, count=1, flags=re.DOTALL)
    with open(SEED_PATH, "w", encoding="utf-8", newline="") as stream:
        stream.write(updated)


def check_seed(block):
    with open(SEED_PATH, "r", encoding="utf-8") as stream:
        seed = stream.read()
    pattern = re.escape(BEGIN) + r"\n.*?" + re.escape(END) + r"\n?"
    match = re.search(pattern, seed, flags=re.DOTALL)
    if not match:
        raise ValueError("generated Skills markers are missing from " + SEED_PATH)
    if match.group(0) != block:
        raise ValueError("Skills seed drift detected; run tools/generate_skill_seed.py --write")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the generated block in eva_seed.kql")
    parser.add_argument("--check", action="store_true", help="fail if eva_seed.kql differs from the manifest")
    args = parser.parse_args()
    if args.write and args.check:
        parser.error("choose --write or --check")
    try:
        block = render_block(load_manifest())
        if args.write:
            update_seed(block)
            print("Generated " + SEED_PATH)
        else:
            check_seed(block)
            print("Skills manifest and Kusto seed are in sync")
    except (OSError, ValueError) as exc:
        print("Skills catalog error: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())