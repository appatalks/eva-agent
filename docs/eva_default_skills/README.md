# Eva Default Skills

This directory is the canonical, repository-authored catalog for Eva's shipped
skills. `manifest.json` is machine-readable and is the only authored source for
the fifteen default skill definitions. The document abilities are clean-room
Eva-native implementations based on public formats and permissive libraries.

## Runtime Projection

The SQLite memory backend loads this manifest from the repository or packaged
app and projects each entry into the `Skills` table. The Kusto seed block is
generated from the same manifest by `tools/generate_skill_seed.py`. Run the
generator with `--check` in CI or before a release to detect drift; it never
uses the network.

The runtime projection keeps the compact operational fields used by the bridge:
`id` becomes `SkillId`, `preferred_tools` becomes `Tools`, and
`trigger_examples` become `Tags`. `configurable_defaults` and the approved
fallback list are stored as bounded JSON in `Config`, so values such as the
Weather skill's default location remain structured and editable. Rich manifest
fields remain available to catalog tooling and documentation. User edits are
stored as `user-override` rows and are not overwritten by startup backfill.

## Bounded Abilities

`skill-docx`, `skill-pdf`, `skill-pptx`, and `skill-xlsx` execute real bounded
operations through `tools/skills/`. PDF supports AcroForm inspection/fill,
pdfplumber table extraction, and fixed Poppler plus Tesseract OCR. DOCX and
PPTX can render to PDF, and XLSX recalculation writes an explicit new workbook
through fixed LibreOffice/soffice conversion before scanning cached formula
results and errors. The fixed bridge action is `POST /v1/skills/execute`; it
accepts only a known skill and operation, keeps paths under `EVA_ARTIFACTS_DIR`
or the trusted `EVA_SKILLS_WORKSPACE_ROOTS`, rejects traversal and symlinks,
and returns an action receipt. Missing Python or system packages are reported
without installation. `skill-mcp-builder` scaffolds and validates small Python
FastMCP or TypeScript MCP SDK projects without network access or package
installation. When `EVA_SKILLS_WORKSPACE_ROOTS` contains multiple path-separated
entries, requests use `workspace` for the first and `workspace-2`, `workspace-3`,
and so on for subsequent approved roots.

The MCP Builder provenance and Apache-2.0 attribution are recorded in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), with the packaged license at
[licenses/Apache-2.0.txt](licenses/Apache-2.0.txt). The four document skills do
not copy or derive from vendor-specific skill files.

## Categories

Every skill belongs to exactly one primary category. These labels are stable UI
and API values:

- Information & Research
- Documents & Data
- Development & Integrations
- Browser & Desktop Automation
- Vision & Media
- Communication
- Memory & Personalization
- Uncategorized

Rows imported before Category existed are mapped to `Uncategorized`. Community
imports are intentionally excluded from this runtime catalog; see
`docs/community_skills/README.md`.