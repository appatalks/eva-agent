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
| OpenAI direct | `core/js/gpt-core.js` | model settings, prompt budget |
| Copilot and GitHub Models | `core/js/copilot.js` | bridge, model settings |
| Eva AIG | `core/js/aig.js` | bridge AIG lifecycle, cognition |
| Gemini and LM Studio | `core/js/gl-google.js`, `lm-studio.js` | prompt budget, local MCP |
| Image generation | `core/js/dalle3.js` | response renderer |

## Settings

| Workflow | Owner | Focused contract |
| --- | --- | --- |
| Model controls and theme filtering | `core/js/settings/model-settings.js` | `test_provider_token_budget.js`, `test_model_catalog.js` |
| Goals | `core/js/settings/goals.js` | `test_goals_settings.js` |
| Data mode and diagnostics | `core/js/settings/runtime.js` | `test_runtime_settings.js` |
| Cron schedules | `core/js/settings/cron.js` | `test_cron_settings.js` |
| Background controls and proposals | `core/js/settings/background.js` | `test_background_settings.js` |
| Alert rules and delivery limits | `core/js/settings/alerts.js` | `test_alerts_settings.js` |
| Remaining settings orchestration | `core/js/options.js` | static suite plus domain-focused checks |

## Features

| Feature | Current owner | Notes |
| --- | --- | --- |
| Skill import and library | `core/js/skills.js` | Manual import/edit lifecycle |
| Skill auto-learning | `core/js/features/skills/auto-learn.js` | Bounded post-outcome draft extraction |
| Proactive notifications | `core/js/features/notifications/proactive.js` | Polling, chat/voice delivery, and seen acknowledgment |
| Browser automation | `core/js/browser-agent.js` | Do not move without updating packaging/docs/test path contracts |
| Camera and desktop interaction | `core/js/camera.js`, renderer logic in `options.js` | Permission and agent-confirmation boundaries remain explicit |
| Voice | `core/js/voice.js`, `voice-endpoint.js`, voice-view logic in `options.js` | Split only behind focused lifecycle tests |
| Workspaces | `core/js/workspaces.js` | Electron preload/main and bridge workspaces are immediate collaborators |
| Agents and Assets | `core/js/agents.js`, `assets.js` | Main view owners |
| Sessions and profiles | `core/js/sessions.js`, `idb-store.js`, `profiles.js` | Storage contracts are compatibility-sensitive |

## Migration Rules

1. Create the destination module before deleting the previous owner.
2. Retain global compatibility functions while classic scripts and inline markup
   still call them.
3. Add the destination script before dependent scripts in `index.html`.
4. Add or update a focused executable contract before moving behavior.
5. Preserve bridge, Electron, storage, and approval contracts unless a migration
   is explicitly approved.
6. Update this map and issue #158 after a validated owner changes.