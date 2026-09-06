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
- `core/js/providers/openai.js`: OpenAI chat/completions logic.
- `core/js/providers/gemini.js`: Gemini logic.
- `core/js/providers/lm-studio.js`: Local inference via lm-studio OpenAI-compatible API.
- `core/js/providers/image-generation.js`: Image generation.
- `config.json`: Local API keys (not committed).

## Model Routing
- Keep `aig` as the only top-level chat selector value; normal sends must call `aigSend()`.
- Add or change selectable responder models only under the AIG backend selector in `index.html`.
- AIG requests go through `tools/acp_bridge.py`, which chooses OpenAI direct, Copilot ACP, or LM Studio according to availability, request needs, and policy mode.
- Automatic policy is the default. Tool-required requests must use ACP or local MCP and fail closed when no tool-capable route is available.
- `gpt-5-mini` and `latest` may remain accepted internal compatibility values, but they are not top-level selector values.
- **Copilot ACP** remains available for tool-capable model execution through `core/js/providers/copilot.js` and `tools/acp_bridge.py`. GitHub PAT authentication is for GitHub MCP and private repository imports only.

## Settings Panel
- The settings panel is a tabbed modal with four tabs: General, Models, Auth, and Prompts.
- **General**: Theme, TTS engine/voice, auto-speak.
- **Models**: AIG backend selector, automatic/pinned policy, temperature, max tokens, reasoning effort, and LM Studio settings.
- **Auth**: API key inputs stored in `localStorage` (override `config.json`). Keys: OpenAI, GitHub PAT for MCP/imports, and Google Gemini compatibility.
- **Prompts**: Personality presets and editable system/developer prompt textarea. `getSystemPrompt()` returns the textarea value.

## OpenAI AIG Backends
- Endpoint: `POST https://api.openai.com/v1/chat/completions` when AIG policy selects a direct OpenAI responder.
- Required headers: `Authorization: Bearer ${OPENAI_API_KEY}`, `Content-Type: application/json`.
- Base payload: `{ model, messages, max_completion_tokens, temperature, frequency_penalty, presence_penalty, stop }`.
- Special cases:
  - `o1*` models: filter out `developer` role messages and set `temperature = 1`.
  - `o3-mini`: include `reasoning_effort` and omit `temperature` (applied in both branches).
  - `gpt-5*`: do not include `max_tokens` (use `max_completion_tokens` only); `top_p` is allowed; omit `temperature` and `stop`.

## Edge Cases
- Image input: `aig.js` pushes a text+image structured message and the bridge uses the selected vision-capable responder.
- Auto-speak checkbox triggers Polly TTS after responses.
- Image placeholders `[Image of ...]` are detected by `renderEvaResponse()` and resolved via Wikimedia Commons search or DALL-E 3 generation.

## Testing Checklist
- Verify send flow with and without images.
- Test normal AIG chat, automatic model selection, tool-required ACP/local-MCP routing, and pinned override behavior.
- Confirm Errors 400/404/429/500 are surfaced in `txtOutput`.
- Validate localStorage message persistence and clear/reset.

## Development Efficiency
- Work autonomously through focused inspection, implementation, and the smallest relevant manual or executable check.
- When diagnosing behavior observed in the installed Eva app, inspect a bounded set of the newest relevant rows from the installed local `Conversations` table. Raw stored assistant content shows what Eva saw and emitted, including action markers hidden by rendering; correlate the same timestamp, session, or turn with the privacy-safe runtime audit before deciding that an action actually executed.
- Treat installed conversation data as sensitive debugging evidence: query only the minimum relevant rows, do not copy real addresses, names, message bodies, or other user data into repository files or committed tests, and replace any necessary regression fixture values with obvious examples.
- A conversational promise such as "I will check" is not an execution receipt. If the audit has no matching native action, bridge call, or tool outcome, treat the turn as a routing/actionability defect and fix the controlling path rather than Eva's prose.
- Product code lives under `tools/`; validation code lives under `tools/tests/`. Do not read or use the test suite as product context unless the current change needs a focused check or the user asks for test work.
- Keep ad hoc regression scripts under `tools/tests/local/`, which is ignored. Promote a local regression into the committed suite only when the user explicitly asks for a CI contract.
- CI runs an explicit curated set of scripts from `tools/tests/`; do not add broad test discovery or make the application package test files.
- Invoke the `reviewer` agent only when the user asks for a review, a PR is being prepared, or a difficult design, security, data-integrity, or cross-provider question remains unresolved after local investigation.
- Do not request reviewer approval for routine implementation, validation, version bumps, or usability work. Summarize residual risks directly when no escalation is needed.

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
- Do not introduce external network calls except to configured providers (OpenAI, GitHub MCP, Google, localhost endpoints).
- Before every commit, mentally audit: **does this diff contain any real key, token, URL, or user-specific data?** If unsure, do not commit.
- When adding example config or documentation, always use obviously fake values: `sk-FAKE...`, `ghp_EXAMPLE...`, `https://example-cluster.region.kusto.windows.net`.
- Treat `.env`, `.env.*`, and any file matching `*secret*`, `*credential*`, `*token*` as sensitive — never create or commit them.

## Versioning
- Update `README.md` Features list when adding models or user-visible features.

## Build
- After every completed code change or update that will be tested manually, install the current workspace version system-wide before asking the user to test it. Do not treat a workspace AppImage as the manual-test target.
- Preserve the installed runtime state and secrets while syncing the active workspace into `~/.eva`; never copy `config.json`, `config.local.js`, `.env*`, `.azure/`, token caches, `memory.db`, or backups from the workspace.
- For feature branches or uncommitted work, do not pull `main` into the installed copy. Sync the current workspace, then run `cd ~/.eva && ./install.sh --yes --no-update --build`.
- Refresh `~/.local/bin/eva` and the desktop launcher to point to the latest `~/.eva/standalone/dist/Eva Standalone-<version>.AppImage`, then launch `eva` for the independent manual test.
- Before packaging a committed default-branch release, pull the latest into `~/.eva` first. The installed copy, not the git working tree, is always the manual-test target.
- The AppImage bundles its own copy of `tools/` at a temporary mount path. Changes to `~/.eva/tools/` are **not** picked up until the AppImage is rebuilt.
- The build command is `npm run dist` inside `standalone/`.

## ACP Infrastructure Roadmap
- Keep ACP deployment assumptions aligned with `README-2.md` under **ACP Infrastructure Roadmap (tracking)**.
- Until roadmap completion, treat split deployment as the default: static web tier may run on legacy hosts, while ACP Bridge runs on a compatible host.
- For ACP server changes, verify and document runtime prerequisites:
  - CPU architecture supports Copilot CLI (`x86_64` or `arm64`).
  - Node.js is `>= 24`.
  - Python is `>= 3.12`.
  - Copilot CLI authentication is active (`copilot auth login` completed).
- Do not remove localhost ACP fallback behavior in `core/js/providers/copilot.js` until the single-host milestone is marked complete.
