# Copilot Instructions

This project is a simple web UI for interacting with OpenAI, Google Generative models, and local LLMs (lm-studio). Use this guide to keep contributions consistent and safe.

## Goals
- Keep the UI minimal and fast. Avoid heavy frameworks.
- Support multiple providers via a single routing point.
- Store transient chat state in localStorage; do not add servers unless requested.
- Prefer small, targeted PRs; preserve existing behavior unless a change is requested.

## Key Files
- `index.html`: UI, settings, and script wiring.
- `core/js/options.js`: Model routing and UI behavior.
- `core/js/gpt-core.js`: OpenAI chat/completions logic.
- `core/js/gl-google.js`: Gemini logic.
- `core/js/lm-studio.js`: Local inference via lm-studio OpenAI-compatible API.
- `core/js/dalle3.js`: Image generation.
- `config.json`: Local API keys (not committed).

## Model Routing
- Add new models to the selector in `index.html` (use `<optgroup>` to group by provider).
- Wire routing in `updateButton()` and `sendData()` in `core/js/options.js`.
- If a model uses the OpenAI Chat Completions API, route to `trboSend()`.
- If a model uses a different API, create a new send function in `core/js/*.js` and route accordingly.
 - `gpt-5-mini`: treated like other OpenAI chat models unless documentation calls for different parameters.
 - `latest` alias: allowed in the selector; treated as an OpenAI model value. If OpenAI updates how `latest` resolves (e.g., via Responses API), adjust `gpt-core.js` accordingly.
- **GitHub Copilot models** (`copilot-*` prefix): route to `copilotSend()` in `core/js/copilot.js`. Uses GitHub Models API (`models.inference.ai.azure.com`) with a GitHub PAT. The `copilot-` prefix is stripped before sending to the API.
- **Copilot ACP** (`copilot-acp`): route to `copilotSend()` which detects ACP mode and proxies through `tools/acp_bridge.py` (local Python server bridging Copilot CLI's Agent Client Protocol). Uses whatever model the Copilot CLI is configured for (GPT-4o, Claude, Gemini, etc.). No PAT needed — auth is handled by `copilot auth login`.

## Settings Panel
- The settings panel is a tabbed modal with four tabs: General, Models, Auth, and Prompts.
- **General**: Theme, TTS engine/voice, auto-speak.
- **Models**: Model selector (grouped by provider with `<optgroup>`), temperature, max tokens, reasoning effort (o3-mini).
- **Auth**: API key inputs stored in `localStorage` (override `config.json`). Keys: OpenAI, GitHub PAT, Google Gemini, Google Vision.
- **Prompts**: Personality presets and editable system/developer prompt textarea. `getSystemPrompt()` returns the textarea value.

## OpenAI Models
- Endpoint: `POST https://api.openai.com/v1/chat/completions` through Electron main's authenticated process-global provider lease and bounded direct-TLS broker.
- Required headers: `Authorization: Bearer ${OPENAI_API_KEY}`, `Content-Type: application/json`.
- Base payload: `{ model, messages, max_completion_tokens, temperature, frequency_penalty, presence_penalty, stop }`.
- Special cases:
  - `o1*` models: filter out `developer` role messages and set `temperature = 1`.
  - `o3-mini`: include `reasoning_effort` and omit `temperature` (applied in both branches).
  - `gpt-5*`: do not include `max_tokens` (use `max_completion_tokens` only); `top_p` is allowed; omit `temperature` and `stop`.

## Edge Cases
- Image input: `options.js` pushes a text+image structured message for vision-capable models.
- Auto-speak checkbox triggers Polly TTS after responses.
- Image placeholders `[Image of ...]` are detected by `renderEvaResponse()` and resolved via Wikimedia Commons search or DALL-E 3 generation.

## Testing Checklist
- Verify send flow with and without images.
- Test each model route from the selector.
- Confirm Errors 400/404/429/500 are surfaced in `txtOutput`.
- Validate localStorage message persistence and clear/reset.

## Developer Prompts
- "Add a new provider/model; wire it into the selector and routing with minimal changes."
- "Refactor to a fetch() wrapper but keep backward compatibility; don't change behavior."
- "Add unit-lite tests as plain functions or small harnesses if feasible; avoid build steps."

## Security/Privacy
- **Never commit secrets.** This includes API keys, PATs, tokens, passwords, cluster URLs, database names, and any credential material — in any file, any format (JSON, JS, Python, Markdown, HTML, comments, example snippets).
- `config.json` and `config.local.js` are local-only and gitignored. Do not add them, reference real values from them in code examples, or create alternative config files that contain real keys.
- Do not hardcode IP addresses, hostnames, ports of real servers, or internal URLs (e.g., `192.168.*`, `*.hoshisato.com`, Kusto cluster URLs). Use placeholders like `<your-cluster>`, `localhost`, or `example.com`.
- Do not commit `.data` files from `core/external/` — they contain live external data fetched at runtime.
- Do not commit audio files (`*.wav`, `*.mp3`), token caches (`.azure/`, `msal_token_cache.json`), or log files.
- Do not introduce `console.log()` or `print()` statements that dump tokens, keys, or full request/response bodies containing auth headers.
- Do not introduce external network calls except to configured providers (OpenAI, GitHub Models, Google, localhost endpoints).
- Before every commit, mentally audit: **does this diff contain any real key, token, URL, or user-specific data?** If unsure, do not commit.
- When adding example config or documentation, always use obviously fake values: `sk-FAKE...`, `ghp_EXAMPLE...`, `https://example-cluster.region.kusto.windows.net`.
- Treat `.env`, `.env.*`, and any file matching `*secret*`, `*credential*`, `*token*` as sensitive — never create or commit them.

## Versioning
- Update `README.md` Features list when adding models or user-visible features.

## Build
- After every completed code iteration, deploy and rebuild so changes are testable immediately:
  1. Pull the latest into the installed copy: `cd ~/.eva && git pull origin main`.
  2. Rebuild the Electron AppImage: `cd ~/.eva/standalone && npm run dist`.
- The AppImage bundles its own copy of `tools/` at a temporary mount path. Changes to `~/.eva/tools/` are **not** picked up until the AppImage is rebuilt.
- The build command is `npm run dist` inside `standalone/`.
- Do not skip the pull+rebuild step. If the user is testing, they are testing the installed copy, not the git working tree.

## ACP Infrastructure Roadmap
- Keep ACP deployment assumptions aligned with `README-2.md` under **ACP Infrastructure Roadmap (tracking)**.
- Until roadmap completion, treat split deployment as the default: static web tier may run on legacy hosts, while ACP Bridge runs on a compatible host.
- For ACP server changes, verify and document runtime prerequisites:
  - CPU architecture supports Copilot CLI (`x86_64` or `arm64`).
  - Node.js is `>= 24`.
  - Python is `>= 3.12`.
  - Copilot CLI authentication is active (`copilot auth login` completed).
- Do not remove localhost ACP fallback behavior in `core/js/copilot.js` until the single-host milestone is marked complete.
