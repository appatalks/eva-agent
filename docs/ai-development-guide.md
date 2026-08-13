# AI Development Guide

Eva is a framework-free browser and Electron application with a local Python
bridge. Work on one owning boundary at a time. Do not load the full repository
for a focused change: the relevant contract, owner, and validation command
should determine the context.

The active phased refactor plan and progress log live in
[issue #158](https://github.com/appatalks/eva-agent/issues/158). Update that
issue after each completed slice with the branch or PR, preserved behavior,
validation, and deferred risk.

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

## Task Bundles

| Change | Read first | Focused validation |
| --- | --- | --- |
| Add or alter a selectable model | `index.html`, `core/js/model-routing.js`, `core/js/settings/model-settings.js`, provider adapter, `docs/contracts/provider-routing.md` | `node tools/tests/test_model_catalog.js` |
| Direct OpenAI behavior | `core/js/gpt-core.js`, `core/js/options.js`, prompt budget | `python3 tools/tests/test_static.py` |
| Copilot or ACP behavior | `core/js/copilot.js`, `tools/bridge/acp_client.py`, `tools/bridge/core.py` | Model catalog test plus targeted ACP test |
| AIG request behavior | `core/js/aig.js`, `tools/bridge/aig_request.py`, `tools/bridge/core.py`, `tools/bridge/model_policy.py` | `python3 tools/tests/test_aig_request.py`, then static suite |
| Local model or MCP behavior | `core/js/lm-studio.js`, `tools/bridge/local_mcp.py`, `tools/bridge/utils.py` | Relevant local-MCP or streaming test |
| Memory or learning | `tools/bridge/memory.py`, `tools/bridge/cognition.py`, `core/js/learning.js` | Memory or learning contract test |
| Workspace UI | `core/js/workspaces.js`, `standalone/preload.js`, `standalone/main.js` | Relevant workspace test; packaged E2E for UI/lifecycle changes |
| Workspace bridge lifecycle | `tools/bridge/workspaces.py`, `tools/bridge/core.py`, Electron projection | Workspace unit/e2e test and path-confinement coverage |
| Voice, camera, browser, or desktop | Owning `core/js/` module plus matching `tools/` worker | Feature-specific focused test or manual capability check |

## Ownership Map

| Area | Owner | Boundary |
| --- | --- | --- |
| UI composition and settings markup | `index.html`, `core/style.css` | DOM IDs and user-facing settings |
| Shared browser behavior | `core/js/options.js` | Startup, settings orchestration, and rendering helpers |
| Model settings | `core/js/model-routing.js`, `core/js/settings/model-settings.js` | Selector classification, parameter controls, AIG metadata, and theme filtering |
| Goals settings | `core/js/settings/goals.js` | Goal list, form validation, and private bridge lifecycle |
| Runtime settings | `core/js/settings/runtime.js` | Data retrieval mode and local diagnostics |
| Cron settings | `core/js/settings/cron.js` | Recurring-task validation, bridge CRUD, and schedule rendering |
| Skill auto-learning | `core/js/features/skills/auto-learn.js` | Bounded post-outcome Skill draft extraction |
| Provider adapters | `core/js/gpt-core.js`, `copilot.js`, `gl-google.js`, `lm-studio.js`, `aig.js`, `dalle3.js` | Provider request/response lifecycle |
| Conversation storage | `core/js/sessions.js`, `idb-store.js`, `profiles.js` | Session and browser-local state |
| Bridge HTTP and AIG orchestration | `tools/bridge/core.py` | Private loopback API and request lifecycle |
| Bridge domains | `tools/bridge/*.py` | Memory, skills, MCP, background, workspaces, telemetry, policy |
| Privileged desktop boundary | `standalone/main.js`, `preload.js` | IPC, path-bearing operations, PTY ownership, secure storage |
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