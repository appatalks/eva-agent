# AI Development Guide

Status: living guide. Last reviewed 2026-08-15 against Eva 5.6.2.

Eva is a framework-free browser and Electron application with a local Python
bridge. Work on one owning boundary at a time. Do not load the full repository
for a focused change: the relevant contract, owner, and validation command
should determine the context.

[docs/README.md](README.md) is the index for this folder and states which
document owns each kind of change.

## Progress Tracking

Record each completed slice where the work is reviewable: the branch or pull
request, the preserved behavior, the validation commands and their results, and
any deferred risk. The modularization sprint used
[issue #158](https://github.com/appatalks/eva-agent/issues/158) as its progress
log; that issue remains the historical record for the module-ownership moves
described below. When a change belongs to a newer tracked effort, use that
effort's issue instead of appending to a closed one, and update the owning
document in this folder in the same change.

## Working Rules

1. Identify the user-visible behavior and its owning module before editing.
2. Read the owning code, its immediate collaborator, and the narrowest relevant
   test or contract.
3. Make the smallest behavior-preserving change that can prove the hypothesis.
4. Run a focused validation immediately after the edit, then run the broader
   static suite before the slice is complete.
5. Preserve public contracts: model values, saved settings, bridge endpoints,
   Electron IPC names, workspace record formats, approval behavior, and runtime
   paths stay compatible unless a change explicitly requires a migration.
6. Keep security checks visible. Repeated loopback, capability, path-confinement,
   or approval checks are intentional defense in depth, not refactoring targets.

## Context Boundaries

Do not include these in ordinary coding-agent context:

- `core/js/aws-sdk-2.1304.0.min.js` is vendored and minified.
- `core/img/`, `core/audio/`, generated artifacts, browser profiles, runtime
  databases, token caches, logs, and build output are not normal source context.
- `tools/tests/` is validation-only. Read it when a focused check is needed,
  not as a substitute for product ownership.

Never read, create, or commit API keys, tokens, cookies, browser profiles,
runtime databases, logs, or other private data unless an explicit task requires
local inspection and its privacy implications have been established.

Before removing a deprecated or fallback path, consult
`docs/deprecation-inventory.md` and provide the listed usage/migration evidence.

## Task Bundles

| Change | Read first | Focused validation |
| --- | --- | --- |
| Add or alter a selectable model | `index.html`, `core/js/model-routing.js`, `core/js/settings/model-settings.js`, provider adapter, `docs/contracts/provider-routing.md` | `node tools/tests/test_model_catalog.js` |
| Direct OpenAI behavior | `core/js/providers/openai.js`, `core/js/options.js`, prompt budget | `python3 tools/tests/test_static.py` |
| Copilot or ACP behavior | `core/js/providers/copilot.js`, `tools/bridge/acp_client.py`, `tools/bridge/core.py` | Model catalog test plus targeted ACP test |
| AIG request behavior | `core/js/providers/aig.js`, `tools/bridge/aig_request.py`, `tools/bridge/core.py`, `tools/bridge/model_policy.py` | `python3 tools/tests/test_aig_request.py`, then static suite |
| Local model or MCP behavior | `core/js/providers/lm-studio.js`, `tools/bridge/local_mcp.py`, `tools/bridge/utils.py` | Relevant local-MCP or streaming test |
| Memory or learning | `tools/bridge/memory.py`, `tools/bridge/cognition.py`, `core/js/learning.js` | Memory or learning contract test |
| Structured memory records or inspection | `tools/bridge/memory_model.py`, `tools/sqlite_memory.py`, `core/js/memory-inspector.js` | `python3 tools/tests/test_memory_recall.py` |
| Workspace UI | `core/js/features/workspaces/monitor.js`, `standalone/preload.js`, `standalone/main.js` | Relevant workspace test; packaged E2E for UI/lifecycle changes |
| Workspace bridge lifecycle | `tools/bridge/workspaces.py`, `tools/bridge/core.py`, Electron projection | Workspace unit/e2e test and path-confinement coverage |
| Terminal broker or PTY lifecycle | `standalone/terminal-broker.js`, `standalone/preload.js`, `core/js/features/workspaces/monitor.js` | `node tools/tests/test_terminal_broker.js`, then `node tools/tests/test_terminal_e2e.js` |
| Native Eva control surface | `core/js/harness-control.js`, `tools/bridge/core.py` prompt contract | `node tools/tests/test_harness_control.js` |
| GitHub operations from Eva | `standalone/main.js` GitHub IPC handlers, `tools/bridge/core.py`, `tools/bridge/utils.py` | `python3 tools/tests/test_static.py` plus `python3 tools/tests/test_streaming.py` |
| Skill catalog, routing, or lifecycle | `tools/bridge/skills.py`, `docs/eva_default_skills/manifest.json` | `python3 tools/tests/test_skills_catalog.py`, `node tools/tests/test_skills_voice_management.js` |
| Bounded document abilities | `tools/skills/document_ops.py`, `tools/skills/mcp_builder.py` | `python3 tools/tests/test_skills_document_ops.py` with the managed bridge interpreter |
| Protected memory | `tools/protected_memory.py`, `tools/bridge/core.py` protected endpoints | `python3 tools/tests/test_protected_memory.py` |
| Voice, camera, browser, or desktop | Owning `core/js/` module plus matching `tools/` worker | Feature-specific focused test or manual capability check |

## Ownership Map

| Area | Owner | Boundary |
| --- | --- | --- |
| UI composition and settings markup | `index.html`, `core/style.css` | DOM IDs and user-facing settings |
| Shared browser behavior | `core/js/options.js` | Startup, settings orchestration, and rendering helpers |
| Voice View lifecycle | `core/js/features/voice/view.js` | Ambient HUD, listening, endpoint handoff, barge-in, and compact voice globals |
| Model settings | `core/js/model-routing.js`, `core/js/settings/model-settings.js` | Selector classification, parameter controls, AIG metadata, and theme filtering |
| Goals settings | `core/js/settings/goals.js` | Goal list, form validation, and private bridge lifecycle |
| Runtime settings | `core/js/settings/runtime.js` | Data retrieval mode and local diagnostics |
| Cron settings | `core/js/settings/cron.js` | Recurring-task validation, bridge CRUD, and schedule rendering |
| Skill auto-learning | `core/js/features/skills/auto-learn.js` | Bounded post-outcome Skill draft extraction |
| Native Eva control surface | `core/js/harness-control.js` | Allowlisted navigation and actions on Eva's own surfaces; never synthetic input |
| Structured memory inspection | `core/js/memory-inspector.js` | Atom, trait, and scenario review plus maintainer reset controls |
| Internal cognition loop | `core/js/cognition.js`, `tools/bridge/cognition.py` | Optional draft/review agent cycle over `/v1/aig/chat` |
| In-app dialogs | `core/js/dialogs.js` | Replacement for browser prompts that Electron disables |
| Provider adapters | `core/js/providers/openai.js`, `copilot.js`, `gemini.js`, `lm-studio.js`, `aig.js`, `image-generation.js` | Provider request/response lifecycle |
| Tools & memory settings and protected-memory UI | `core/js/providers/copilot.js` | MCP config, Kusto seeding, artifact purge, memory backend selector, and protected-memory unlock/capture live here for historical reasons |
| Conversation storage | `core/js/features/sessions/explorer.js`, `idb-store.js`, `profiles.js` | Session and browser-local state |
| Bridge HTTP and AIG orchestration | `tools/bridge/core.py` | Private loopback API and request lifecycle |
| Fixed bridge route tables | `tools/bridge/http_routes.py` | Pure method/path matching; authorization remains in the handler |
| Bridge domains | `tools/bridge/*.py` | Memory, skills, MCP, background, workspaces, telemetry, policy |
| Bounded skill execution | `tools/skills/` | Document, spreadsheet, presentation, and MCP scaffold abilities under path confinement |
| Privileged desktop boundary | `standalone/main.js`, `preload.js` | IPC, path-bearing operations, PTY ownership, secure storage |
| PTY broker | `standalone/terminal-broker.js` | Terminal creation, replay, resize, and process-group cancellation |
| Workspace projection | `standalone/workspace-projection.js` | Opaque workspace paths exposed to the renderer |
| Test contracts | `tools/tests/` | Curated regression checks; not bundled into the app |

## Refactoring Policy

Prefer a data table over repeated conditionals when the entries share one stable
policy, such as model metadata, settings metadata, or route metadata. Do not
invent a generic abstraction when provider behavior or a security boundary is
meaningfully different.

Extract a function or module only after its current behavior is protected by a
focused check. Keep a compatibility wrapper when callers must migrate gradually.
Do not combine a behavioral change with a broad cleanup unless the behavior
cannot be safely changed otherwise.

## Completion Checklist

- The owner and public contract are still clear.
- New code does not duplicate a known policy or persistence key.
- A focused validation falsified the intended behavior if it were wrong.
- `python3 tools/tests/test_static.py` passes for a completed source slice.
- A packaged/manual test is run when Electron, bridge packaging, UI layout, or
  privilege boundaries changed.
- Documentation is updated when ownership, setup, user-visible behavior, or a
  durable contract changes.

## Documentation Maintenance

Documentation is part of the slice, not a follow-up. Use this mapping:

| Change | Document to update in the same slice |
| --- | --- |
| A `core/js/` module is added, moved, split, or renamed | `docs/frontend-ownership.md` and the ownership map above |
| A bridge module, Electron IPC channel, or `tools/skills/` ability is added | The ownership map above |
| A selector value, sender route, or GitHub Models mapping changes | `docs/contracts/provider-routing.md` |
| AIG normalization, preflight, or handler ownership changes | `docs/contracts/aig-request-lifecycle.md` |
| A test is added to or removed from the CI job | `docs/testing-contracts.md` |
| A compatibility path gains callers or a migration | `docs/deprecation-inventory.md` |
| A plan phase reaches an exit criterion | The owning plan's phase status |
| A user-visible feature or model is added | `README.md` Features list |

Write what shipped and how it is proven. Do not describe intended behavior in
the present tense before it exists; mark it Planned in the owning plan instead.