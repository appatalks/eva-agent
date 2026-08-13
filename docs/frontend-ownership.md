# Frontend Ownership Map

This map is the navigation contract for browser work. Read the owner, its
immediate collaborators, and its focused test before changing a feature. The
classic-script runtime intentionally preserves globals during migration; a
physical folder move must not change script order or public function names
without a documented compatibility plan.

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
| Remaining settings orchestration | `core/js/options.js` | static suite plus domain-focused checks |

## Features

| Feature | Current owner | Notes |
| --- | --- | --- |
| Skill import and library | `core/js/features/skills/library.js` | Manual import/edit lifecycle |
| Skill auto-learning | `core/js/features/skills/auto-learn.js` | Bounded post-outcome draft extraction |
| Proactive notifications | `core/js/features/notifications/proactive.js` | Polling, chat/voice delivery, and seen acknowledgment |
| ACP permissions | `core/js/features/permissions/acp.js` | Adaptive polling, capability headers, and one-time decisions |
| Browser and desktop automation controller | `core/js/features/automation/browser-agent.js` | Shared popup, polling, confirmation, and cancellation API |
| Camera sensing and vision | `core/js/features/automation/camera.js` | Camera lifecycle and `[[EVA_LOOK]]` vision routing |
| Voice listener, endpoint, and Voice View | `core/js/features/voice/wake-listener.js`, `endpoint.js`, `view.js` | Wake word, transcript buffering, and the Voice View lifecycle; classic-script globals remain compatible |
| Workspaces | `core/js/features/workspaces/monitor.js` | Electron preload/main and bridge workspaces are immediate collaborators |
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