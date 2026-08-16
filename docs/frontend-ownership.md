# Frontend Ownership Map

Status: living map. Last reviewed 2026-08-16 against Eva 5.6.2.

This map is the navigation contract for browser work. Read the owner, its
immediate collaborators, and its focused test before changing a feature. The
classic-script runtime intentionally preserves globals during migration; a
physical folder move must not change script order or public function names
without a documented compatibility plan.

Script order in `index.html` is itself a contract. `node
tools/tests/test_frontend_script_order.js` protects it, because a classic-script
module that loads before its dependency fails silently at runtime.

## Runtime And Providers

| Area | Owner | Primary collaborators |
| --- | --- | --- |
| Selector classification | `core/js/model-routing.js` | `core/js/options.js`, provider adapters |
| Shared private bridge transport | `core/js/runtime/bridge-client.js` | Settings, Skills, Workspaces, and Memory Inspector |
| Prompt budget and request classification | `core/js/prompt-budget.js`, `request-routing.js` | all provider adapters |
| OpenAI direct | `core/js/providers/openai.js` | model settings, prompt budget |
| Copilot and GitHub Models | `core/js/providers/copilot.js` | bridge, model settings |
| Eva AIG | `core/js/providers/aig.js` | bridge AIG lifecycle, cognition |
| Gemini and LM Studio | `core/js/providers/gemini.js`, `core/js/providers/lm-studio.js` | prompt budget, local MCP |
| Image generation | `core/js/providers/image-generation.js` | response renderer |
| Internal cognition loop | `core/js/cognition.js` | `/v1/aig/chat`, footer status line |
| Learning signals and consent | `core/js/learning.js` | bridge learning endpoints, settings |
| In-app dialogs | `core/js/dialogs.js` | any surface needing a prompt Electron disables |
| Aggregate runtime logging | `standalone/runtime-logger.js` | Electron, renderer, bridge, Local Voices, lifecycle, crash, and sanitized audit output; terminal content excluded |
| External data snapshots | `core/js/external.js` | `core/external/*.data` files fetched at runtime |

`core/js/pandora.js` is an experimental self-modification sketch. Model-produced
code is deliberately not executable in the renderer; do not add an execution
path to it.

`core/js/aws-sdk-2.1304.0.min.js` is vendored and minified. It is not coding-agent
context.

## Settings

| Workflow | Owner | Focused contract |
| --- | --- | --- |
| Model controls and theme filtering | `core/js/settings/model-settings.js` | `test_provider_token_budget.js`, `test_model_catalog.js` |
| System prompt and personality presets | `core/js/settings/prompts.js` | `test_prompts_settings.js` |
| Goals | `core/js/settings/goals.js` | `test_goals_settings.js` |
| Data mode and diagnostics | `core/js/settings/runtime.js` | `test_runtime_settings.js` |
| Cron schedules | `core/js/settings/cron.js` | `test_cron_settings.js` |
| Background controls and proposals | `core/js/settings/background.js` | `test_background_settings.js` |
| Alert rules and delivery limits | `core/js/settings/alerts.js` | `test_alerts_settings.js` |
| Audio devices and voice preferences | `core/js/settings/audio.js` | `test_audio_settings.js` |
| Email accounts, recipient consent, and sending | `core/js/settings/email.js` | `test_email_settings.js`; bridge policy and delivery contracts remain under `tools/tests/test_email_*.py` |
| Remaining settings orchestration | `core/js/options.js` | static suite plus domain-focused checks |

Three settings surfaces do not live where their name suggests. Record the real
owner before searching:

| Surface | Actual owner | Reason |
| --- | --- | --- |
| Tools & memory: MCP config, Kusto seeding, artifact purge, memory backend selector | `core/js/providers/copilot.js` | The GitHub PAT and MCP configuration grew alongside the Copilot adapter; extraction is a candidate, not a completed move |
| Protected memory status, unlock, and chat capture interception | `core/js/providers/copilot.js` | Same module; the chat interception must run before provider dispatch |
| Auth keys and Profile | `core/js/options.js`, `index.html` | Never extracted into `core/js/settings/` |

Extract any of these only behind a focused contract, following the migration
rules below.

## Features

| Feature | Current owner | Notes |
| --- | --- | --- |
| Native Eva control surface | `core/js/harness-control.js` | Allowlisted navigation and actions on Eva's own surfaces. It never simulates pointer or keyboard input, and its manifest is the source of the prompt contract the bridge advertises. Contract: `test_harness_control.js` |
| Structured memory inspection | `core/js/memory-inspector.js` | Atom, persona-trait, and scenario review with source tracing and maintainer reset controls |
| Skill import and library | `core/js/features/skills/library.js` | Manual import/edit lifecycle. Conversational Skill management is routed natively; contract: `test_skills_voice_management.js` |
| Skill auto-learning | `core/js/features/skills/auto-learn.js` | Bounded post-outcome draft extraction |
| Proactive notifications | `core/js/features/notifications/proactive.js` | Polling, chat/voice delivery, and seen acknowledgment |
| ACP permissions | `core/js/features/permissions/acp.js` | Adaptive polling, capability headers, and one-time decisions |
| Browser and desktop automation controller | `core/js/features/automation/browser-agent.js` | Shared popup, polling, confirmation, and cancellation API |
| Camera sensing and vision | `core/js/features/automation/camera.js` | Camera lifecycle and `[[EVA_LOOK]]` vision routing |
| Voice listener, endpoint, and Voice View | `core/js/features/voice/wake-listener.js`, `endpoint.js`, `view.js` | Wake word, transcript buffering, and the Voice View lifecycle; classic-script globals remain compatible |
| Workspaces | `core/js/features/workspaces/monitor.js` | Electron preload/main and bridge workspaces are immediate collaborators; owns the run list, live chat drawer, and removal controls |
| Assets library | `core/js/features/assets/library.js` | Generated and workspace file library |
| Agent Operations | `core/js/features/agents/operations.js` | Agent cards and memory topology view |
| Sessions and profiles | `core/js/features/sessions/explorer.js`, `idb-store.js`, `profiles.js` | Storage contracts are compatibility-sensitive |

## Migration Rules

1. Create the destination module before deleting the previous owner.
2. Retain global compatibility functions while classic scripts and inline markup
   still call them.
3. Add the destination script before dependent scripts in `index.html`.
4. Add or update a focused executable contract before moving behavior.
5. Preserve bridge, Electron, storage, and approval contracts unless a migration
   is explicitly approved.
6. Update this map and issue #158 after a validated owner changes.