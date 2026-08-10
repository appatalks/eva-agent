# Technical Documentation

Detailed architecture, dependencies, and implementation notes for Eva AI Assistant.

> **Current release:** Eva 5.5.8. This document describes the matching browser UI,
> Python bridge, and Electron package in this repository.

> **Recommended experience:** Select **Eva (AIG)** from the model dropdown for the full
> Eva experience: persistent memory, emotion tracking, proactive data retrieval, and
> intelligent cross-model orchestration. All other models work standalone, but AIG is the
> way Eva was designed to be used.

## Providers

| Provider | Models |
|---|---|
| Eva (AIG) | Orchestration over direct OpenAI API, GitHub Models, ACP, and LM Studio |
| OpenAI | GPT-4o, GPT-4o Mini, o1, o1-preview, o1-mini, o3-mini, latest |
| GitHub Copilot (PAT) | GPT-4o, GPT-4o Mini, o3-mini, GPT-5.6 Sol/Terra/Luna, GPT-5, o4-mini, DeepSeek-R1, Llama 4 Maverick |
| GitHub Copilot (ACP) | Claude, GPT-5.x, GPT-4.1 via Copilot CLI |
| Google Gemini | Gemini 2.0 Flash (Thinking Exp) |
| LM Studio | Any local OpenAI-compatible model (fully offline) |
| gpt-image-1 | Image generation |

## Highlights

- Multi-agent AIG with eva and reviewer cognitive pipeline
- Dual-mode data retrieval: cloud (Copilot CLI + MCP) or local (LM Studio + direct MCP)
- MCP tool access (Kusto, GitHub, Azure, web search) hot-reloadable at runtime
- Persistent memory via Azure Data Explorer (Kusto) or local SQLite
- Signal text messaging (send-only via signal-cli, keyword-triggered fallback for local models)
- Autonomous browser control (Playwright + CDP) and desktop control (pyautogui)
- Webcam presence detection (OpenCV face + motion)
- Inline image search (Wikimedia) and generation (gpt-image-1)
- Downloadable artifact creation (PDF, text, CSV, markdown) with auto-open
- Skill import/normalization from paste, URL, GitHub, or file upload
- Hands-off background cognition with applied/failed proposal and activity audit trails
- Cron scheduler for recurring tasks (briefings, checks, reminders)
- Alert system (SEC filings, weather, space weather, keyword watch) with Signal delivery
- Mode persistence across restarts (bridge-side mode.txt, frontend localStorage)
- TTS: OpenAI (default), browser, user-authorized Local Voices, and Amazon Polly (standard/neural/generative)
- LCARS and Eva themes (7 Eva variants)
- Standalone Electron AppImage with bundled bridge
- Durable coding workspaces with automatic Eva-ready Git project provisioning,
  isolated worktrees, workspace-confined ACP agents, and durable run records
- Real Electron PTY terminal with xterm rendering, bounded replay, resize/search,
  lower-half Workspace Monitor docking, and process-session cancellation
- Main-window Workspace Monitor, unified generated/workspace Assets library,
  and searchable Skills library/editor with source/status organization
- Full behavioral eval harness with mock and live modes

## Architecture

```
+---------------------------------------------------------------------------+
|                           Browser / Electron                              |
|  index.html + core/js/*.js + core/style.css                               |
|                                                                           |
|  +---------+ +----------+ +--------+ +-----------+ +-------------+       |
|  | OpenAI  | | Copilot  | | Gemini | | Copilot   | |  LM Studio  |       |
|  | Direct  | | PAT API  | | Direct | | ACP/AIG   | |  Direct     |       |
|  +----+----+ +----+-----+ +---+----+ +-----+-----+ +------+------+       |
+-------|---------|-----------|-----------|--------------|-----------------+
        |         |           |           |              |
        v         v           v           v              v
   api.openai  models.    google    +------------+   localhost
     .com     github.ai  generative | ACP Bridge |     :1234
                          apis.com  | (Python)   |  (LM Studio)
                                    | port 8888  |
                                    +--+-----+---+
                             +---------+     +----------+
                             v                          v
                    +--------------+          +--------------+
                    | Copilot CLI  |          | Local MCP    |
                    | (ACP/stdio)  |          | Servers      |
                    |              |          | (subprocess) |
                    +------+-------+          +------+-------+
                           | spawns                  | JSON-RPC
                           v                         v
                    +--------------+          +--------------+
                    | MCP Servers  |          | eva-web-     |
                    | kusto, gh,   |          | search       |
                    | azure        |          | (DDG/Google) |
                    +--------------+          +--------------+
```

### Two Operating Modes

Eva operates in two data retrieval modes, selected automatically based on the
model or manually via Settings > General.

**Cloud mode** (default for Copilot/OpenAI models):
- Copilot CLI (ACP) provides chat, tool execution, web search
- MCP servers spawned by Copilot subprocess
- Requires GitHub Copilot license, consumes tokens

**Local mode** (automatic for LM Studio):
- LM Studio provides chat completions and tool-calling reasoning
- MCP servers spawned directly by the bridge as subprocesses
- Web search via `web_search_mcp.py` (DuckDuckGo HTML scraping, no API key)
- Zero cloud AI, zero tokens, fully offline-capable

**Mode persistence:** The selected mode is persisted to `~/.config/eva-standalone/mode.txt`
by the bridge. On startup, the bridge reads this file to restore the previous mode.
The frontend seeds its selector from the bridge via `GET /v1/mode` after init, and
skips auto-switch logic during startup to avoid overriding the persisted choice.

### Request Flow

**Direct models (OpenAI, Copilot PAT, Gemini):**
Browser -> `GET /v1/memory/context` with session ID -> Provider API -> JSON response ->
`renderEvaResponse()` -> one `POST /v1/memory/reflect`

The memory context is an ephemeral request view and does not mutate browser
history. Gemini follows this same lifecycle, including a reflection request only
after a successful final response.

**ACP models (Copilot CLI):**
1. Browser -> `POST /v1/chat/completions` -> ACP Bridge (HTTP)
2. Bridge -> `session/prompt` -> Copilot CLI (JSON-RPC over NDJSON/stdio)
3. Copilot may request MCP tools; standalone Eva shows an in-chat Allow once/Reject decision unless revocable routine read/search consent applies
4. Copilot streams `session/update` notifications with text chunks
5. Bridge accumulates chunks and can forward them as flushed NDJSON events; the final event remains an OpenAI-compatible response
6. Bridge owns the single post-response reflection for ACP; the browser renderer
  does not submit a duplicate reflection request

### Streaming contract

`POST /v1/aig/chat` and `POST /v1/chat/completions` accept `stream: true`. A supported
stream responds as newline-delimited JSON (`application/x-ndjson`):

```json
{"type":"chunk","text":"partial assistant text"}
{"type":"done","response":{"object":"chat.completion","choices":[...]}}
```

The bridge keeps the existing JSON response when `stream` is false or a client does
not receive the streaming content type. AIG streams only the final responder; its
memory, ACP preflight, and cognition calls remain internal and non-streaming. The
browser puts chunks in a text-only provisional bubble, so action, Signal, file, and
browser markers cannot execute early. It removes that bubble and calls
`renderEvaResponse()` once for the final response, preserving persistence, reflection,
and auto-speak behavior. Bridge telemetry records request start, TTFT, completion and
total timing, chunk count, route, and model labels without prompt or response text;
`GET /v1/telemetry` aggregates TTFT under `summary.stream_ttft_ms`.

**LM Studio (local):**
1. Browser fetches `/v1/memory/context` + `/v1/data/retrieve` in parallel from bridge
2. Bridge injects memory context from SQLite/Kusto
3. Bridge runs data retrieval via local MCP tool-calling loop (see below)
4. Browser prepends memory + data to system prompt
5. Browser sends directly to the configured LM Studio base URL; the default is
  `http://localhost:1234/v1/chat/completions`
6. Response processed by `Cognition.executeActions()` for any action blocks
7. Rendered via `renderEvaResponse()`

**Eva (AIG) with cognition layer:**
1. Browser calls `Cognition.run()` which drives the draft/review/revise pipeline
2. Each agent call goes to `POST /v1/aig/chat` on the bridge
3. Bridge runs Step 1 (memory), Step 2 (data retrieval), Step 3 (persona), Step 4 (LLM call)
4. LLM call routes to GitHub Models API (PAT), ACP (Copilot CLI), or LM Studio based on model
5. Step 5: background reflection thread logs conversation, extracts entities, computes emotion

**Image handling:**
1. `_detectGenerationIntent()` captures user's intent + subject before send
2. AI responds with `[Image of ...]` placeholder
3. `renderEvaResponse()` detects placeholder, routes to:
   - **gpt-image-1** if user said "generate/create/draw" (uses user's simple subject)
   - **Wikimedia Commons** otherwise (progressive query: full -> 2 words -> 1 word)
4. Image inserted inline with lightbox click-to-expand

## Project Structure

```
index.html                 Main UI: chat, settings modal, LCARS sidebar,
                           monitors dock, input area, lightbox
config.json                API keys (not committed, gitignored)
config.example.json        Template for config.json
config.local.example.js    Template for file:// usage (inlined config)
mcp.json                   Tracked default MCP server configuration; credentials
                           are resolved from environment/local settings

core/
  style.css                All styling: base theme, settings panel,
                           monitors, chat bubbles, responsive
  themes/
    eva.css                Eva dark theme overrides
    lcars.css              LCARS (Star Trek) theme overrides
  js/
    options.js             Core application logic (5000+ lines):
                           - Config loading (auth(), applyConfig())
                           - Auth key management (getAuthKey, saveAuthKeys)
                           - System prompt management (getSystemPrompt, applyPersonalityPreset)
                           - Model routing (updateButton, sendData)
                           - Data mode switching (switchDataMode, loadDataMode)
                           - Theme management (applyTheme)
                           - Token/network/session monitors
                           - Image handling (renderEvaResponse, _searchImage, _generateImage)
                           - Markdown renderer (renderMarkdown)
                           - Artifact download/open links (appendArtifactLinks)
                           - Auto-open artifacts via bridge /v1/files/<name>?open=1
                           - AWS Polly TTS (speakText)
                           - Speech recognition, print, clear memory
    gpt-core.js            OpenAI Chat Completions API (trboSend)
                           - XHR-based (legacy, not fetch)
                           - Model-specific params (o3-mini reasoning, gpt-5 top_p)
                           - External data augmentation (weather, news, markets, solar)
    gl-google.js           Google Gemini API (geminiSend)
                           - Thinking mode (extracts thoughts vs non-thoughts)
    lm-studio.js           Local LLM via LM Studio (lmsSend)
                           - OpenAI-compatible endpoint on localhost:1234
                           - Parallel memory context + data retrieval from bridge
                           - Action block execution (Cognition.executeActions)
                           - File capability documentation in system prompt
                           - Post-response reflection via bridge
    copilot.js             GitHub Copilot integration (copilotSend)
                           - Dual mode: GitHub Models API (PAT) + ACP Bridge
                           - MCP configuration (applyMCPConfig, refreshMCPStatus)
    aig.js                 Eva AIG orchestration (aigSend)
                           - Routes through bridge /v1/aig/chat
                           - Optional browser-side cognitive layer
                           - Phrase triggers force cognition for single turn
    cognition.js           Browser-side multi-agent cognitive layer:
                           - Two role-specific agents: eva (planner), reviewer (critic)
                           - Bounded review loop (cogMaxCycles, default 1)
                           - Capability registry (file.download, file.open)
                           - Action protocol: [[EVA_ACTION]]{...}[[/EVA_ACTION]]
                           - Built-in PDF generator (Helvetica, Latin-1, multi-page)
                           - Marker protocol: [[EVA_BROWSER]], [[EVA_DESKTOP]],
                             [[EVA_LOOK]], [[EVA_FILE]]
    agents.js              Agent Operations scorecard, task detail, steering,
                           keyed card updates, and memory topology canvas
    assets.js              Main Assets library: generated artifacts plus
                 changed files from retained coding worktrees
    dialogs.js             Promise-based in-app text prompt for Electron
    dalle3.js              Image generation via gpt-image-1 (dalle3Send)
    idb-store.js           IndexedDB storage backend (sessions + blobs)
    sessions.js            Session persistence, legacy-visible restoration,
                 terminal/xterm renderer and dock navigation
    voice.js               Wake-word "Eva" via Web Speech API
    camera.js              Webcam capture for [[EVA_LOOK]] vision
    browser-agent.js       Frontend integration for browser agent runs
    pandora.js             Pandora box / Easter egg system
    skills.js              Full-view Skills library/editor: search, status/source
                 filters, sort, import, edit, enable/disable, delete
    workspaces.js          Workspace Monitor, coding-run creation, progress
                 narration, terminal/chat handoff, lifecycle actions
    external.js            External data fetching at page load

tools/
  acp_bridge.py            Entry point (imports bridge/ package)
  bridge/
    __init__.py
    __main__.py            Allows `python -m bridge`
    core.py                Main HTTP server, AIG pipeline, all endpoints (~4500 lines)
    acp_client.py          ACPClient: Copilot CLI subprocess, JSON-RPC, model pool
    cognition.py           Memory context builder, entity extraction, emotion computation
    memory.py              Backend switching (Kusto/SQLite), embeddings, synonyms
    skills.py              Skill import, evarise normalization, SSRF-safe URL fetching
    kusto.py               Azure Data Explorer queries, ingest, token management
    config.py              All constants, paths, thresholds, table schemas
    state.py               Mutable runtime state (thread-safe)
    local_mcp.py           Local MCP client, tool-calling agent loop
    background.py          Background job system (13 job types, proposal audit)
    cron.py                Cron scheduler (5-field expressions)
    alerts.py              Alert/notification system (SEC, weather, space weather)
                           Signal messaging via signal-cli
    telemetry.py           Structured event logging (latency, routing decisions)
    utils.py               URL validation, LM Studio validation, config persistence
    workspaces.py          SQLite workspace domain: projects, checkouts, coding
                 runs, agent runs, worktrees, assets, safe cleanup
  web_search_mcp.py        MCP server: DuckDuckGo + Google fallback (no API key)
  sqlite_memory.py         SQLite memory backend (SqliteMemory class)
  kusto_mcp.py             MCP server for Azure Data Explorer (10 tools)
  browser_agent.py         Autonomous web browsing (Playwright + CDP)
  desktop_agent.py         Autonomous desktop control (pyautogui + vision)
  camera_sense.py          Webcam presence detection (OpenCV face + motion)
  local_voices_bridge.py   Token-protected local Chatterbox TTS + Faster Whisper STT bridge
  voice_clone_module/      Eva-maintained Chatterbox adapter; no reference audio
  eva_seed.kql             Sanitized database seed (public-safe)
  acp_bridge.service       Systemd unit file
  acp_setup.sh             One-command installer
  test_static.py           CI-safe static tests
  test_eva.py              Live bridge integration suite
  test_latency.py          Latency benchmarks
  test_skills_e2e.py       Skill import end-to-end tests
  test_terminal_broker.js  PTY confinement, replay, process-session cleanup
  test_terminal_e2e.js     Packaged terminal, Sessions, and Skills UI workflow
  test_workspace_electron_e2e.js
                           Packaged Workspace/Assets/terminal workflow
  test_workspaces.py       Git worktree and symlink-confinement lifecycle tests
  test_workspaces_e2e.py   Workspace bridge, agent dispatch, Assets HTTP lifecycle
  eval/                    Behavioral eval harness

standalone/
  main.js                  Electron shell, bridge spawn, workspace-only capability,
                           project picker, opaque IPC projections, asset opening
  preload.js               Narrow allowlisted renderer IPC surface
  terminal-broker.js       Approved-root PTY ownership, replay, resize, termination
  workspace-projection.js  Redacts known project/worktree paths from reports
  package.json             Electron + electron-builder config (v5.5.8)
```

## Dependencies

### Browser-side (no install needed)
- Barlow Condensed font (loaded from Google Fonts CDN)
- AWS SDK v2.1304.0 (bundled, for Polly TTS)

### Electron terminal runtime

| Package | Purpose |
|---|---|
| `node-pty` | Native PTY creation and streaming |
| `@xterm/xterm` | Terminal rendering |
| `@xterm/addon-fit` | Measured container/grid fitting |
| `@xterm/addon-search` | Scrollback search |
| `@xterm/addon-web-links` | Safe terminal link detection |
| `playwright-core` (dev) | Attach to packaged Electron through CDP for E2E tests |

### Server-side (for ACP Bridge)
| Dependency | Required for | Install |
|---|---|---|
| Python 3.12+ | ACP Bridge, Kusto MCP | System package or `pyenv` |
| Node.js 24+ | Copilot CLI | `nvm install 24` or system package |
| `@github/copilot` | Copilot CLI | `npm install -g @github/copilot` |
| `azure-identity` | Kusto MCP auth | `pip install azure-identity` |
| `requests` | AIG HTTP calls | `pip install requests` |
| Docker | GitHub MCP server | [docker.com](https://docker.com) |
| Playwright | Browser agent | `pip install playwright && playwright install` |
| pyautogui | Desktop agent | `pip install pyautogui` |
| opencv-python | Camera presence | `pip install opencv-python` |
| signal-cli | Signal messaging | Native binary from [GitHub releases](https://github.com/AsamK/signal-cli/releases), or `install.sh` auto-installs |
| Local speech runtime (optional) | Chatterbox English + Multilingual V3 TTS, Faster Whisper STT | `./install.sh --voice-deps` |

### API Keys
| Key | Used by | Get it from |
|---|---|---|
| `OPENAI_API_KEY` | Direct OpenAI Eva backends, OpenAI models, image generation, TTS/transcription, embeddings | [platform.openai.com](https://platform.openai.com/api-keys) |
| `GITHUB_PAT` | Copilot Models API | [github.com/settings/tokens](https://github.com/settings/tokens) (needs "Models" permission) |
| `GOOGLE_GL_KEY` | Google Gemini | [aistudio.google.com](https://aistudio.google.com/apikey) |
| `GOOGLE_VISION_KEY` | Google Vision (image analysis) | [console.cloud.google.com](https://console.cloud.google.com/apis/credentials) |
| AWS credentials | Amazon Polly TTS | [AWS IAM Console](https://console.aws.amazon.com/iam/) |
| None | LM Studio (local mode) | Free, runs locally |

## ACP Bridge

### Protocol

The bridge implements the [Agent Client Protocol (ACP)](https://agentclientprotocol.com/overview/introduction), GitHub's JSON-RPC 2.0 protocol over NDJSON (newline-delimited JSON) on stdio. The `copilot` CLI speaks this protocol natively; the bridge translates it to HTTP for the browser frontend.

**ACP methods handled:**

| Method | Direction | Purpose |
|---|---|---|
| `initialize` | Client -> Agent | Negotiate version, exchange capabilities |
| `session/new` | Client -> Agent | Create conversation session |
| `session/prompt` | Client -> Agent | Send user message |
| `session/update` | Agent -> Client | Stream response chunks, tool calls, plans |
| `session/request_permission` | Agent -> Client | Request tool execution permission (in-chat decision; routine read/search may use revocable standing consent) |
| `session/cancel` | Client -> Agent | Cancel ongoing operation |
| `terminal/create` | Agent -> Client | Unsupported and rejected; Eva does not advertise ACP terminal capability |
| `terminal/output` | Agent -> Client | Unsupported and rejected |
| `terminal/release` | Agent -> Client | Unsupported and rejected |

### ACP Client Lifecycles

The primary AIG/chat path maintains a pool of up to 4 `ACPClient` instances, keyed
by model, reasoning effort, route tool profile, and a secret-safe MCP configuration
fingerprint. Each pooled client is a separate warm `copilot` subprocess. Within a
client, prompts are routed to bounded conversation-scoped ACP sessions using the
browser session ID. A conversation session rotates after 20 prompts or 30 minutes
of idle time, and each client retains at most 8 session routes. This keeps warm
process reuse without allowing one hidden ACP context to grow forever. ACP does
not expose session deletion in the protocol version used here, so rotation drops
old sessions from bridge routing while retaining only the bounded live route set.

Copilot CLI MCP exposure is process-scoped: an existing ACP session cannot safely
change its server set for one request. The bridge therefore selects a narrow
profile (`none`, `web`, `github`, or `kusto`) for ordinary routes and uses `broad`
only when the request needs general tool access. Profile-specific warm processes
are isolated in the same bounded pool, so a web-search request does not inherit
GitHub, Kusto, or computer-use tools. MCP fingerprints hash only non-secret
configuration shape; credential values are excluded from pool keys and telemetry.

```python
  acp_pool: dict[profiled_model_key -> ACPClient]  # model + profile + config
  acp_pool_order: list[profiled_model_key]         # LRU eviction order
acp_pool_lock: threading.RLock()         # thread-safe access
ACP_POOL_MAX = 4
```

When a primary request arrives for a model not in the pool, the bridge spawns a
new Copilot CLI process (`copilot --acp --stdio`), runs the ACP `initialize` and
`session/new` handshake, and registers it in the pool. New browser conversations
use `session/new` on the existing warm process instead of spawning another one.
If the pool is full, the least-recently-used client is evicted and its subprocess
terminated.

Subagents do not use this model-keyed pool. Every accepted subagent worker owns
a dedicated `ACPClient` and session for the task lifetime, including when other
tasks select the same model. The worker stops that subprocess in `finally`, so
parallel agents cannot share conversation context or leak a client after exit.

### Available ACP Models

Models available through the Copilot CLI (requires a GitHub Copilot license). The
catalog evolves; this list reflects a recent `copilot --list-models` output.

| Provider | Model ID | Notes |
|---|---|---|
| **Anthropic** | `claude-opus-4.8` | AIG backend only |
| | `claude-opus-4.7` | Variants: `-high`, `-xhigh` |
| | `claude-opus-4.6` | Variant: `-1m` (1M context) |
| | `claude-opus-4.5` | |
| | `claude-sonnet-4.6` | |
| | `claude-sonnet-4.5`, `claude-sonnet-4` | |
| | `claude-haiku-4.5` | Fastest Claude |
| **OpenAI** | `gpt-5.6-luna` | Default AIG backend (High reasoning) |
| | `gpt-5.5` | |
| | `gpt-5.4`, `gpt-5.4-mini` | |
| | `gpt-5.3-codex`, `gpt-5.2-codex` | |
| | `gpt-5.2`, `gpt-5-mini` | |
| | `gpt-4.1` | |

### CLI Flags

```bash
python3 tools/acp_bridge.py [options]

Options:
  --port PORT              HTTP port (default: 8888)
  --bind ADDRESS           Bind address (default: 127.0.0.1)
  --copilot-path PATH      Path to copilot binary (default: copilot)
  --model MODEL            Default AI model (e.g. claude-sonnet-4.6, gpt-5.2)
  --cwd DIR                Working directory for ACP session
  --enable-kusto-mcp       Enable Kusto MCP server
  --kusto-cluster URL      Kusto cluster URL
  --kusto-database NAME    Default Kusto database
  --enable-azure-mcp       Enable Azure MCP server (requires az login)
  --enable-github-mcp      Enable GitHub MCP server (requires Docker + PAT)
  --mcp-config PATH        Custom MCP config JSON file
```

### HTTP Endpoints

**Core:**

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/chat/completions` | POST | OpenAI-compatible chat (routes to ACP) |
| `/v1/aig/chat` | POST | AIG pipeline: memory + data + persona + LLM |
| `/v1/models` | GET | Available models list |
| `/health` | GET | Status, session ID, model, MCP servers |

**Memory:**

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/memory/context` | GET | Build and return memory context for injection |
| `/v1/memory/reflect` | POST | Trigger post-response reflection (entities, emotion) |
| `/v1/memory/backend` | GET/POST | Get or switch memory backend (kusto/sqlite) |
| `/v1/kusto/seed` | POST | Loopback-only Kusto schema seed |

**Data Retrieval:**

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/data/retrieve` | GET | Retrieve live data for any model path |
| `/v1/mode` | GET/POST | Get or switch data retrieval mode (cloud/local) |

**Skills:**

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/skills` | GET | List all active skills |
| `/v1/skills` | POST | Create a new skill |
| `/v1/skills/evarise` | POST | Normalize raw skill text into Eva schema |
| `/v1/skills/auto-learn` | POST | Extract skill from conversation context |
| `/v1/skills/<id>` | PATCH | Update a skill (enable/disable/edit) |
| `/v1/skills/<id>` | DELETE | Soft-delete a skill |

**Goals:**

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/goals` | GET/POST | List or create goals |
| `/v1/goals/<id>` | PATCH/DELETE | Update or soft-delete a goal |

**Files (Artifacts):**

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/files/write` | POST | Write artifact to ARTIFACTS_DIR |
| `/v1/files/<name>` | GET | Serve artifact (download or auto-open with `?open=1`) |

**Coding Workspaces (Standalone capability required):**

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/workspaces/eva-ready` | POST | Create/register the managed Eva Ready Workspace |
| `/v1/workspaces/projects` | GET/POST | List or register Git projects |
| `/v1/workspaces/runs` | GET/POST | List runs or create worktree + dispatch agent |
| `/v1/workspaces/runs/<id>` | GET | Read one run and latest AgentRun |
| `/v1/workspaces/runs/<id>/dispatch` | POST | Recover/dispatch active run |
| `/v1/workspaces/runs/<id>/archive` | POST | Archive run and retain checkout |
| `/v1/workspaces/runs/<id>/discard` | POST | Explicit safe cleanup |
| `/v1/workspaces/checkouts/<id>/status` | GET | Revalidate checkout and dirty state |
| `/v1/workspaces/assets` | GET | List changed workspace files |
| `/v1/workspaces/assets/resolve` | POST | Resolve one contained relative file for Electron main |

**Background:**

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/background/status` | GET | Loop status, interval, last tick |
| `/v1/background/control` | POST | Enable/disable, change interval, run now |
| `/v1/background/proposals` | GET | Proposal audit records, optionally filtered by status |
| `/v1/background/proposals/<id>/approve` | POST | Apply a pending proposal record |
| `/v1/background/proposals/<id>/reject` | POST | Reject a pending proposal record |
| `/v1/background/activity` | GET | Recent background tick activity |

**Cron:**

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/cron` | GET/POST | List or create cron tasks |
| `/v1/cron/<id>` | PATCH/DELETE | Update or delete a cron task |

**Alerts:**

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/alerts` | GET/POST | List or create alert rules |
| `/v1/alerts/<id>` | PATCH/DELETE | Update or delete an alert rule |
| `/v1/alerts/settings` | GET/POST | Get or update alert settings (quiet hours, rate limits, Signal numbers) |
| `/v1/notifications` | GET | Unseen notifications |

**MCP:**

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/mcp` | GET | Active MCP servers (secrets redacted) |
| `/v1/mcp/configure` | POST | Restart copilot with new MCP config |

**Browser/Desktop Agents:**

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/browser/run` | POST | Start autonomous browser task |
| `/v1/browser/status?run_id=<id>` | GET | Status for the selected browser run |
| `/v1/browser/screenshot?run_id=<id>` | GET | Latest screenshot for the selected browser run |
| `/v1/browser/confirm` | POST | Answer confirmation prompt; JSON body includes `run_id`, `approve`, optional `text` |
| `/v1/browser/cancel` | POST | Cancel a browser run; JSON body includes `run_id` |
| `/v1/desktop/run` | POST | Start autonomous desktop task |
| `/v1/desktop/status?run_id=<id>` | GET | Status for the selected desktop run |
| `/v1/desktop/screenshot?run_id=<id>` | GET | Latest screenshot for the selected desktop run |
| `/v1/desktop/confirm` | POST | Answer confirmation prompt; JSON body includes `run_id`, `approve`, optional `text` |
| `/v1/desktop/cancel` | POST | Cancel a desktop run; JSON body includes `run_id` |

**Agent Operations:**

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/agents/overview` | GET | Loopback-only normalized agent activity, background state, and bounded memory graph snapshot |
| `/v1/subagent/status` | GET | Raw status for all subagents or one task selected with `?id=` |
| `/v1/subagent/spawn` | POST | Start an isolated ACP subagent; accepts `prompt`, `label`, optional `model`, and optional `session_id` |
| `/v1/subagent/spawn-batch` | POST | Atomically reserve and start 1-4 independent or collaborative tasks |
| `/v1/subagent/steer` | POST | Queue or resume an existing subagent with `id` and `instruction` |
| `/v1/subagent/<id>` | DELETE | Dismiss a completed, failed, or cancelled task from Agent Operations |

**Camera:**

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/camera/start` | POST | Start webcam presence detection |
| `/v1/camera/stop` | POST | Stop webcam |
| `/v1/camera/status` | GET | Presence state (faces, motion) |
| `/v1/camera/frame` | GET | Latest captured frame (JPEG) |

**Diagnostics:**

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/doctor` | GET | System diagnostics (runtimes, tools, auth) |
| `/v1/telemetry` | GET | Recent telemetry events |
| `/v1/logs` | GET | Recent bridge log lines |
| `/v1/prefs` | GET/POST | Client preferences (non-secret toggles) |

## Local MCP System

When data retrieval mode is "local", the bridge spawns MCP servers as direct
subprocesses and manages them through `LocalMCPManager`.

### MCPServer Class

Each MCP server is a subprocess communicating via JSON-RPC over stdio:

```python
class MCPServer:
    name: str                    # server identifier
    command: str                 # executable (e.g. python3)
    args: list[str]              # command-line args
    process: subprocess.Popen    # running subprocess
    tools: list[dict]            # discovered via tools/list
    alive: bool                  # health state
    protocol_era: str            # modern (2026-07-28) or legacy
    protocol_version: str        # selected protocol version
    tool_cache_ttl_ms: int       # modern tool-list freshness hint
    tool_cache_scope: str        # modern public/private cache hint
```

**Lifecycle:**
  1. `start()`: Spawn the process and probe `server/discover` with the MCP
    `2026-07-28` per-request `_meta` fields.
  2. Modern servers use stateless `tools/list` / `tools/call` requests with
    required `resultType` handling. Tool discovery validates every page, follows
    bounded pagination, and retains valid TTL/cache hints without polling.
  3. A valid discovery response selects `2026-07-28` or falls back to
    `2024-11-05` when the server advertises that legacy version. An incompatible
    modern-only server fails clearly. Other unrecognized probe errors or timeouts
    use the specification's dual-era fallback; if a delayed modern probe causes
    legacy initialization to return modern `-32022`, Eva retries discovery once.
  4. `call_tool(name, arguments, timeout)`: Send `tools/call` JSON-RPC and
    parse content responses. Interactive `input_required` responses are surfaced
    as unavailable until Eva's approval continuation support is implemented.
  5. `stop()`: Terminate the process; failed startup also reaps the child before
    surfacing the error.

**Threading:** Background reader thread per server matches JSON-RPC responses by ID. Stderr is logged to bridge debug log.

  **Compatibility scope:** This local path is tested against the official MCP
  Python SDK v2 and TypeScript SDK v2 in addition to a deterministic legacy
  fixture. It remains an allowlisted local stdio subprocess transport. Streamable
  HTTP, remote OAuth, multi-round-trip approval continuations, Tasks, Resources,
  Prompts, Apps, Roots, and Sampling are intentionally separate follow-up work;
  Roots and Sampling are deprecated in MCP `2026-07-28` and are not planned for
  new Eva support.

### LocalMCPManager

Manages multiple MCP servers with a unified tool catalog:

```python
class LocalMCPManager:
    servers: dict[name -> MCPServer]
    _tool_map: dict[tool_name -> server_name]  # routes calls
```

### Tool-Calling Agent Loop

`local_agent_query()` implements an iterative tool-calling agent using LM Studio:

1. Send user message + full tool schemas to LM Studio `/chat/completions`
2. If model returns `tool_calls` in the response, execute each via `mcp_manager.call_tool()`
3. Inject tool results back as assistant messages
4. Repeat until model produces a text answer (max 5 iterations, 90s timeout)
5. Return `(data_text, model_used)` matching the ACP retrieval signature

### Web Search MCP Server

`tools/web_search_mcp.py` provides web search without any API key:

| Tool | Args | Description |
|---|---|---|
| `web_search` | query, max_results (8) | DuckDuckGo HTML scraping + Google fallback |
| `web_search_news` | query, max_results (8) | DDG with news-biased queries |
| `web_fetch` | url, max_length (6000) | Extract readable text from URL |

**Search cascade:** DuckDuckGo HTML -> DuckDuckGo Lite -> Google HTML scraping. User-Agent spoofs Chrome 131.

### Auto-Configuration

When switching to local mode, the bridge:
1. Checks for ACP's MCP config (if ACP is connected)
2. Falls back to persisted config (`~/.config/eva-standalone/mcp_config.json`)
3. Always auto-adds `eva-web-search` MCP if not already present
4. Searches multiple paths for `web_search_mcp.py` (bridge directory, `~/.eva/tools/`)

## Memory System

Eva supports two memory backends, switchable at runtime via Settings or the
`/v1/memory/backend` endpoint.

### Backends

**Azure Data Explorer (Kusto):**
- Full KQL query language
- Managed cloud service with auto-scaling
- Device code authentication with token caching
- Best for multi-device or production deployments

**SQLite (local):**
- Zero-dependency local file (`~/.eva/memory.db`)
- Automatic table creation and migration
- Best for local-only or offline deployments

### Memory Tables

| Table | Columns | Purpose |
|---|---|---|
| `Knowledge` | Entity, Relation, Value, Confidence, Source, Decay, Timestamp | Facts about user and world |
| `Conversations` | SessionId, Role, Content, Timestamp | Chat history |
| `EmotionState` | Joy, Curiosity, Concern, Excitement, Calm, Empathy, Trigger, Timestamp | Emotional readings |
| `MemorySummaries` | Period, Summary, Timestamp | Compressed session summaries |
| `Reflections` | Trigger, Observation, ActionTaken, Effectiveness, Timestamp | Self-reflections |
| `Goals` | GoalId, Title, Description, Category, Status, Priority, RelatedTopics, CreatedAt, UpdatedAt | Persistent intentions |
| `SelfState` | Capability, Status, Timestamp | Active capabilities |
| `HeuristicsIndex` | Entity, Category, Frequency, Timestamp | Pattern tracking |
| `EmotionBaseline` | Dimension, Value, Timestamp | Emotional defaults |
| `BackgroundProposals` | ProposalId, JobType, TargetTable, Payload, Status, ... | Applied/failed proposal audit records; pending records remain API-compatible |
| `BackgroundActivity` | TickId, Status, ProposalCount, Timestamp | Background loop ticks |
| `Skills` | SkillId, Name, Description, Instructions, Tools, Tags, Source, Status, CreatedAt, UpdatedAt | Imported reusable skills |

### Memory Context Injection

`_build_memory_context(user_message)` builds a structured system prompt section
injected into every AIG request. Both SQLite and Kusto paths produce the same
output structure:

| Section | When | Source |
|---|---|---|
| `[Current Date & Time]` | Always | System clock |
| `[Skills]` | Always | Hardcoded capability catalog (13 built-in capabilities) |
| `[Active MCP Servers]` | When servers running | Live server state + tool names |
| `[Workflow: ...]` | Always | 6 workflow instruction sections |
| `[Core Identity Charter]` | Always | Operator-approved identity and design principles |
| `[Adaptive Guidance]` | When active | Bounded effects from retained explicit feedback signals, scoped to the current session |
| `[User Profile]` | Always | Knowledge where Entity="User", Confidence >= 0.5, framed as untrusted data |
| `[Morning Reflection]` | First msg of day | MemorySummaries (latest 3), framed as untrusted data |
| `[Memory: Core Facts]` | Always | Knowledge where Confidence >= 0.6 (top 15), framed as untrusted data |
| `[Active Goals]` | When present | Goals where Status="active" (top 10), framed as untrusted data |
| `[Active Skill: ...]` | On semantic match | User-managed workflow reference, marker-neutralized and bounded by core policy |
| `[Init: First Conversation]` | Empty Knowledge | Introduction prompts |
| `[Emotion State]` | Always | Latest EmotionState row |
| `[Memory: Relevant]` | On keyword match | Lexical + semantic recall against user message, framed as untrusted data |

All prompt-facing persisted memory, including conversation previews, emotion
triggers, table samples, released protected values, and active-skill workflow
text, is marker-neutralized. Ordinary memory is quoted as untrusted data;
skills are reference data that cannot override core policy. The Core Identity
Charter and fixed runtime policy are the only memory-adjacent prompt sections
with instruction authority.

**Skills manifest (always injected):**
- data-retrieval, weather-news, web-search
- browser-control ([[EVA_BROWSER]]), desktop-control ([[EVA_DESKTOP]]), camera-vision ([[EVA_LOOK]])
- signal-messaging ([[EVA_SIGNAL]])
- file-creation ([[EVA_ACTION]] file.download)
- image-search, image-generation
- persistent-memory (table list)
- cron-scheduling, skill-learning

**Skill matching:** When a user message arrives, all active skills are compared by
embedding cosine similarity (threshold 0.30, OpenAI `text-embedding-3-small`).
Up to 2 matching skills have their instructions injected (capped at 1500 chars each).
Falls back to lexical keyword matching if embeddings are unavailable.

### Entity Extraction

Post-response reflection extracts facts using strict regex patterns. User-derived
facts are framed as untrusted data when included in prompts; they cannot change
Eva's Core Identity Charter. Claims about Eva's design or origins require an
operator-approved identity workflow and are not automatically extracted.

| Pattern | Relation | Confidence |
|---|---|---|
| "my kids/children are [Name]" | user_children | 0.85 |
| "my motto/mantra is [text]" | user_motto | 0.85 |
| "my wife/husband is [Name]" | user_partner_name | 0.85 |
| "my dog/cat is [Name]" | user_pet_* | 0.85 |
| "i work at/for [text]" | user_employment | 0.80 |
| "i live in [Location]" | user_location | 0.80 |
| "my hobby is [text]" | user_interest | 0.70 |
| "my favorite [thing] is [text]" | user_favorite_* | 0.65 |
| "i love/enjoy [text]" | user_preference | 0.65 |
| "i am a [role]" | user_role_self_described | 0.65 |

All facts stored with `Source: "explicit_user_fact"`, `Decay: 0.005` (confidence
decays per day via log-decay model).

### Synonym Expansion

Memory recall expands query terms via synonyms to catch differently-worded facts:

```
"playlist" -> playlist, playlists, song, songs, music, track, tracks, tunes
"trip"     -> trip, travel, vacation, holiday, journey
"job"      -> job, work, employer, company, occupation, career
"home"     -> home, location, address, city, based
```

14 synonym groups cover common recall topics.

## Eva (AIG) Pipeline

### How AIG Works

```
Browser -> POST /v1/aig/chat -> ACP Bridge
  |
  +-- Step 0: Fast-route simple greetings, basic arithmetic, and plain date/time asks
  |   +-- One responder model call; skip memory assembly and ACP preflight
  |
  +-- Step 1: Build memory context (Kusto/SQLite queries)
  |   +-- User Profile (Knowledge where Entity="User")
  |   +-- Skills manifest + workflow instructions
  |   +-- Active MCP servers
  |   +-- Day lifecycle / morning reflection
  |   +-- Core knowledge + message-relevant recall
  |   +-- Active goals, emotion state
  |   +-- Semantic skill matching
  |
  +-- Step 2: Data retrieval (skipped for trivial/meta messages)
  |   +-- Cloud: ACP tool call (MCP web search, Kusto, GitHub)
  |   +-- Local: Tool-calling agent loop (LM Studio + direct MCP)
  |   +-- Request classification: news, weather, financial, kusto, web, general
  |
  +-- Step 3: Build Eva persona prompt
  |   +-- Base system prompt + memory context + [Data Retrieved]
  |
  +-- Step 4: Generate response
  |   +-- Route: GitHub Models API (PAT) | ACP (Copilot CLI) | LM Studio
  |
  +-- Step 5: Background reflection (async thread)
      +-- Log to Conversations table
      +-- Extract entities -> Knowledge table
      +-- Update HeuristicsIndex
      +-- Compute emotion vector -> EmotionState table
```

### AIG Request Classification

The bridge classifies each user message to determine routing and prompt tuning:

| Classification | Pattern | Action |
|---|---|---|
| fast/greeting | Whole-message greeting or acknowledgement | One responder call; skip memory and ACP preflight |
| fast/basic-arithmetic | Self-contained numeric expression | One responder call; model answers normally, without deterministic arithmetic output |
| fast/date-time | Plain current date/time question | One responder call with bridge UTC context; skip memory and ACP preflight |
| greeting/trivial | "hi", "thanks", etc. (<=4 words) | Skip data retrieval |
| meta-question | "what can you do", "who are you" (<=6 words) | Skip data retrieval |
| news-search | "news", "headlines", "breaking" | Web search prompt |
| weather-search | "weather", "forecast", "temperature" | Web search prompt |
| financial-data | "stock", "price", "$TICKER" | Web search prompt |
| kusto-query | KQL keywords, table names | Kusto tool prompt |
| web-search | "search", "look up", "find" | Web search prompt |
| general | Everything else | General-purpose prompt |

Every browser provider keeps its complete conversation history in local storage but
sends an ephemeral bounded view through `core/js/prompt-budget.js`. The view keeps
system/developer instructions pinned, deduplicates exact repeated static messages,
retains recent turns, and carries dropped action outcomes, corrections, and open task
state in a bounded rolling summary. Telemetry records only component sizes and token
estimates, never prompt text. Requests outside the strict fast-route patterns retain
the existing factual, action, and high-risk escalation behavior.

### AIG vs Copilot ACP

| Feature | Copilot ACP | Eva (AIG) |
|---|---|---|
| Chat | yes | yes |
| MCP Tools | yes | yes |
| Persistent memory injection | no | yes |
| Emotion tracking | no | yes |
| Entity extraction | no | yes |
| Morning reflection | no | yes |
| Proactive data retrieval | no | yes |
| Persona consistency | Basic | Full Eva system prompt |
| Background consolidation | no | yes |

## Cognition Layer

Eva has two complementary cognitive systems. The **bridge cognition layer** runs
server-side and adds persistent intelligence (memory injection, emotion tracking,
post-response reflection). The **browser cognitive layer** runs in the page and
adds an optional multi-agent draft/review loop.

### Adaptive Review (`core/js/cognition.js`)

Settings > Models > **Adaptive Review** keeps routine conversation on Eva's
selected AIG backend. It invokes an independent reviewer only for action/tool
turns, current-data or factual work, complex decisions, or an explicit cognition
request. The default reviewer is GPT-5.6 Terra; one review/revise pass is used.

```
User turn
  -> Eva selected AIG backend (fast direct path for routine chat)
  -> adaptive gate for consequential turns
    -> independent reviewer (VERDICT: APPROVE | REQUEST_CHANGES)
      -> Eva revises against material feedback
  -> executeActions(): runs any [[EVA_ACTION]] blocks
  -> renderEvaResponse(): renders the final approved draft
```

The selected AIG backend remains responsible for planning and final responses.
ACP owns MCP-backed retrieval and ACP tool calls; browser capabilities and
renderer-dispatched actions follow their local routes. The reviewer is
deliberately tool-free. This prevents a model label from becoming a cosmetic
tool router: routing follows available capabilities and the request type, while
review adds independent scrutiny when the risk warrants it.

**Activation:**

| Trigger | Behavior |
|---|---|
| Adaptive Review on | Reviews consequential turns; routine chat stays direct |
| Phrase in user message | Force-enabled for that single turn |
| Neither | Single-shot AIG path; system note prevents fabricated phase narration |

Trigger phrases: `trigger the chain`, `use cognition`, `use the cognitive layer`,
`run eva`, `run the reviewer`, `engage cognition`, `cognition: on`.

**Configuration (localStorage):**
- `cogEnabled`: "0" or "1"
- `cogReviewerModel`: model name for the independent reviewer (default `gpt-5.6-terra`)
- `cogReviewerPrompt`: optional reviewer prompt override

Legacy `cogEvaModel`, `cogMaxCycles`, and trace values remain readable for
existing profiles but are no longer exposed in Settings.

### Capability Registry

Capabilities are registered functions that Eva can invoke via action blocks:

```js
Cognition.registerCapability({
  id: 'my.capability',
  description: 'What it does and when to use it.',
  run: async function (args) {
    // Return { html: '...' } to replace the action block
  }
});
```

**Built-in capabilities:**

| Capability | Args | Description |
|---|---|---|
| `file.download` | filename, content, mime? | Create downloadable artifact. Genuine PDF for `.pdf` or `application/pdf`. Writes to bridge ARTIFACTS_DIR via `/v1/files/write`. |
| `file.open` | filename | Open existing artifact with system viewer via `/v1/files/<name>?open=1` (xdg-open). |

**Action protocol:**
```
[[EVA_ACTION]]{"id":"file.download","args":{"filename":"report.pdf","content":"..."}}[[/EVA_ACTION]]
```

The regex also handles unclosed blocks (local models often forget `[[/EVA_ACTION]]`):
```javascript
/\[\[EVA_ACTION\]\]([\s\S]*?)\[\[\/EVA_ACTION\]\]|\[\[EVA_ACTION\]\]([\s\S]+)$/g
```

**File behavior defaults:**
- Inline answers by default. Eva only creates file artifacts when the user
  explicitly asks for a file format ("create a PDF", "download as markdown").
- "Give me a briefing" = inline text. "Create a PDF report" = file.download.
- Asking to open an already-created file uses file.open (not re-create).

### Marker Protocol

Eva uses marker blocks for agent capabilities:

| Marker | Purpose | Example |
|---|---|---|
| `[[EVA_BROWSER]]` | Launch Playwright browser agent | `[[EVA_BROWSER]]{"goal":"search for cats","start_url":"https://example.com"}[[/EVA_BROWSER]]` |
| `[[EVA_DESKTOP]]` | Launch desktop vision agent | `[[EVA_DESKTOP]]{"goal":"open GIMP and create canvas"}[[/EVA_DESKTOP]]` |
| `[[EVA_LOOK]]` | Capture webcam frame | `[[EVA_LOOK]]{"question":"what am I holding?"}[[/EVA_LOOK]]` |
| `[[EVA_SIGNAL]]` | Send Signal text message | `[[EVA_SIGNAL]]{"message":"hello world"}[[/EVA_SIGNAL]]` |
| `[[EVA_FILE]]` | Artifact download/open links | `[[EVA_FILE]] report.pdf` (rendered by `renderEvaResponse`) |

## Autonomous Agents

### Browser Agent (`tools/browser_agent.py`)

Autonomous web browsing via Playwright with a persistent Chrome profile.

**Architecture:**
- Director agent (Claude via ACP): text-only, high-level planning
- Executor agent (GPT-4o via OpenAI): vision-based, concrete actions
- Re-consult director every 4 executor steps
- Long-lived Chrome via CDP on port 9333, persistent profile at `~/.config/eva-standalone/browser_profile`

**Action types:** click, double_click, click_ref, type, type_ref, press, scroll, navigate, wait, done, ask

**Safety:** Sensitive actions (buy, purchase, payment, checkout) require user
confirmation before execution. The run parks and waits for
`POST /v1/browser/confirm` with `run_id`, `approve`, and optional `text` in the
JSON body.

**Trajectories:** Each step logged as JSONL + PNG screenshot to `~/.config/eva-standalone/browser_trajectories/` for fine-tuning.

### Desktop Agent (`tools/desktop_agent.py`)

Autonomous desktop control via pyautogui screenshot-and-act loop.

**Architecture:** Same director/executor pattern as browser agent. `pyautogui.FAILSAFE = True` (mouse to corner = emergency stop).

**Safety:** Broader sensitive action set includes delete, sudo, rm, shutdown, reboot, transfer money, send email/message. All require user confirmation.

### Camera Presence (`tools/camera_sense.py`)

Local webcam face and motion detection.

**Architecture:** Subprocess worker (avoids V4L2 GIL wedge). State exposed via JSON file (`~/.config/eva-standalone/camera/state.json`).

**Detection:** OpenCV Haar cascade for faces, frame-difference for motion. Hysteresis: 2 frames to detect presence, 8 to lose it.

**Privacy:** Camera off by default. Only activates on explicit `POST /v1/camera/start`.

### Agent Operations View

The **Agents** sidebar destination replaces the chat workspace with a live
operations scorecard. On screens narrower than 600 px, the Eva theme hides its
sidebar and exposes the same destination through a compact grid button. The view
is implemented by `core/js/agents.js`, with structure in `index.html` and styles
in `core/style.css`.

The client polls `GET /v1/agents/overview` every two seconds while the view is
open and every 15 seconds while it is closed so the sidebar count remains
current. The memory graph is included at most every 30 seconds; intervening
requests fetch only lightweight agent/background status and retain the prior
graph client-side. Closing the view cancels its fast polling and animation
frame. Opening Voice View closes Agent Operations and vice versa.

Cards are keyed by task ID and updated in place. Polling changes only the text,
status classes, and controls that changed; it does not recreate the card grid or
replay entry animation, so active sessions remain visually stable. Terminal
subagent cards (`done`, `error`, or `cancelled`) expose a dismiss button. Active
tasks cannot be dismissed, and a completed upstream task remains protected while
an active synthesis still depends on it. Successful dismissal removes the card
and forces an immediate topology refresh so its graph node disappears at the
same time.

#### Scorecard Data

The bridge normalizes four activity sources into one `agents` array. The response
uses `active_total` for all active agent kinds and `subagents_active` against the
subagent-only `capacity` of four:

| `kind` | Source | Important fields |
|---|---|---|
| `subagent` | `bridge.state.subagent_tasks` | label, model, prompt, result, start/end time, linked chat session |
| `browser` | `tools/browser_agent.py` run registry | goal/subgoal, step, confirmation state, result/error |
| `desktop` | `tools/desktop_agent.py` run registry | goal/subgoal, step, active run state, result/error |
| `background` | latest `BackgroundActivity` state | job type, status, notes, start/end time |

Each square card has a stable runtime ID and displays kind, state, elapsed time,
objective, and step where available. Selecting an unlinked card opens its live
session detail pane. If a subagent was spawned with a `session_id`, selecting it
loads that saved chat through the existing IndexedDB session manager.

Subagent detail includes a steering input. `POST /v1/subagent/steer` behaves as
follows:

- For a running task, the instruction is appended to `steer_queue`. The worker
  completes its current model turn, supplies the latest output and instruction
  to the next turn, and remains in `steering` state.
- For a completed or failed task, the same task record is reopened. Its prior
  result is included as context and a worker resumes under the original ID.
- `steer_history` retains timestamped directions in bridge memory for the life
  of the process. Subagent records are currently process-local and are not
  restored after bridge restart.

Eva launches batches through the registered Cognition capability
`agent.spawn_batch`, which accepts one to four `{label, prompt, model}` objects.
The browser calls `/v1/subagent/spawn-batch` once and reports only bridge-
accepted task IDs. The bridge validates every prompt and reserves the full batch
under one lock before starting workers. If insufficient slots are available,
it returns `429` and creates zero tasks. Each worker creates a dedicated ACP
client/session configured with the requested model, so same-model tasks remain
isolated and concurrent. If no model is supplied, the bridge uses the current
default Copilot model.

Explicit requests to launch, start, spawn, run, or kick off agents activate the
Cognition action path even if the general cognitive-layer toggle is off. Each
accepted task creates a dedicated Copilot ACP client and session. Same-model
tasks therefore run concurrently without sharing conversation context or the
main Eva session. ACP responses are normalized to clean text; structured ACP
errors produce `error` task states and can never be reported as successful
completion. Notification delivery occurs after task state is finalized and is
non-fatal if the notification channel is unavailable.

`spin up` is also an explicit launch trigger. After normal action parsing, the
frontend runs `Cognition.ensureAgentLaunch()`. If the model omitted the action
block, this deterministic guard discards any unsupported success narrative and
creates one to four real tasks through the same capability. The rendered reply
contains only bridge-accepted task IDs.

For requests containing collaboration language such as `work together`,
`relay`, or `handoff`, the last task becomes a synthesis agent. It is created
immediately in `waiting` state with `depends_on` references to the upstream
tasks. Once all prerequisites are `done`, the bridge injects their labeled
outputs into the synthesis prompt and transitions it to `running`. The topology
adds blue `agent` nodes and `feeds` edges for these dependencies.

When a collaborative request also asks for Signal delivery, only the synthesis
task receives `signal_on_complete`. The renderer suppresses its normal immediate
Signal fallback, and the bridge sends the finalized 1-2 line synthesis after
upstream work and synthesis complete. The task remains `finalizing` while
`signal-cli` runs, then changes to `done`; its card and detail pane show
`SIGNAL SENT` or `SIGNAL FAILED`. This option requires the existing Standalone
bridge capability token; browser-only callers cannot schedule Signal delivery.
Consent is derived only from the captured real user turn through
`canAuthorizeSignalDelivery()`; model-provided arguments cannot grant it, and
negated requests such as "do not notify me" never schedule delivery.

Profile creation and session renaming use the in-app `evaTextPrompt()` dialog in
`core/js/dialogs.js`. Native `window.prompt()` is not used because Electron does
not support it. Changed runtime scripts and styles carry version query strings
in `index.html` to prevent Chromium from executing cached pre-upgrade assets.

The dashboard reports the subagent concurrency capacity (`4`) separately from
browser, desktop, and background activity. Browser and desktop cards are
monitoring surfaces; their existing confirmation and cancellation APIs remain
the control path for sensitive actions.

#### Memory Topology

The bottom pane visualizes a live, bounded projection of Eva's existing
`Knowledge` table plus the current agent registry. The bridge selects the 30
newest rows with confidence at least `0.6`, excluding `mentioned`,
`candidate_mentioned`, and `recurring_topic`. Ignore/reserved-word entities are
also omitted so extraction artifacts such as command words do not become
topology labels. It emits:

```json
{
  "graph": {
    "nodes": [
      {"id": "eva-root", "label": "Eva", "type": "core"},
      {"id": "fact-...", "label": "AI assistant with persistent memory", "type": "fact"},
      {"id": "agent-sub-...", "label": "Alpha", "type": "agent", "status": "done"}
    ],
    "edges": [
      {"source": "eva-root", "target": "fact-...", "label": "role", "type": "memory"},
      {"source": "eva-root", "target": "agent-sub-...", "label": "orchestrates", "type": "orchestration"}
    ]
  }
}
```

`eva-root` is fixed at the canvas center and rendered as a gold double-ring with
the permanent label `EVA CORE`. Every agent has a blue `orchestrates` edge from
Eva. Collaborative prerequisites have violet `feeds` edges into the synthesis
agent. Running/waiting agents are blue squares; completed agents become teal
checked circles, and their graph status is merged from the two-second status
poll even when the memory graph remains cached.

Hover text explains the node instead of returning only its label: Eva lists the
agents she orchestrates; agents show status, model, result preview, upstream and
downstream relationships; facts show entity, relation, full value preview, and
confidence. IDs for memory nodes are deterministic SHA-1-derived identifiers
over normalized labels and do not expose database row IDs.

The browser retains force-layout positions across polls, applies attraction and
repulsion, clamps nodes using pixel-safe label margins, flips right-edge labels
to the left, and supports hover and drag. Agent and core nodes are prioritized
inside the 90-node client cap, so memory volume cannot push active sessions off
the canvas. Rendering runs only while the view is visible. Canvas resolution
follows device pixel ratio up to 2x.

This is intentionally a **lightweight GraphRAG complement**, not Microsoft
GraphRAG indexing. Eva already stores entity-relation-value facts, confidence,
source, timestamps, and decay. The topology exposes those relationships without
changing recall behavior. It does not yet perform entity alias resolution,
entity-to-entity edge extraction, multi-hop graph ranking, Leiden community
detection, community summaries, or global/DRIFT search.

The incremental path to fuller graph retrieval is:

1. Add canonical node and typed-edge records with source evidence and aliases.
2. Resolve entities during reflection while retaining `Knowledge` as the
   compatibility read model.
3. Add bounded one- and two-hop retrieval for relational questions.
4. Generate community summaries in background cognition jobs.
5. Evaluate graph retrieval against lexical/embedding recall before changing
   prompt injection.

Keeping this projection read-only means the dashboard can ship independently of
that indexing work and cannot corrupt existing memory.

## Coding Workspaces and Terminal

Coding workspaces are an experimental Eva Standalone feature enabled with
`EVA_WORKSPACE_TERMINAL_V1=1`, the Electron argument
`--eva-workspace-terminal-v1`, or:

```bash
cd standalone
npm run start:workspace
```

The workspace system is a local control plane layered on the existing bridge
and Agent Operations implementation. The renderer never receives arbitrary
Node process access or absolute workspace paths. The Python bridge owns durable
records and Git operations; Electron main owns path-bearing responses, project
selection, PTYs, and system file opening; preload exposes an allowlisted API of
opaque IDs and display metadata.

### Shipped architecture

```text
Renderer (opaque IDs only)
    |
    | workspaceList*, workspaceCreateRun, workspaceRunAction,
    | workspaceOpenAsset, terminal*
    v
Electron main
    |-- main-process-only EVA_WORKSPACE_CAPABILITY
    |-- filesystem picker and system file opener
    |-- TerminalBroker (node-pty + approved-root registry)
    |-- renderer-safe DTO and report redaction
    |
    | authenticated loopback HTTP
    v
Python bridge / tools/bridge/workspaces.py
    |-- workspaces.sqlite3
    |-- argument-array Git operations
    |-- managed runtime root and Eva Ready Workspace
    |-- CodingRun / AgentRun lifecycle and workspace Assets
    v
Git source repository -> isolated eva/run-<id> worktree
                              |
                              +-> dedicated ACPClient(cwd=worktree)
```

### Durable records and filesystem layout

The database is `${EVA_CONFIG_DIR}/workspaces.sqlite3`. Managed worktrees live
under `${EVA_CONFIG_DIR}/worktrees/`, and the automatically provisioned project
lives at `${EVA_CONFIG_DIR}/projects/eva-ready/`.

| Record | Current responsibility |
|---|---|
| `projects` | Canonical Git root and display metadata. |
| `checkouts` | Source checkout or managed worktree, branch, base revision, lifecycle, dirty count, owner references. |
| `coding_runs` | Objective, project/checkout link, linked chat ID, model policy, run state, final disposition. |
| `agent_runs` | Durable agent identity, conversation key, checkout, capability policy, status, bounded report, parent link. |
| `terminal_sessions` | Reserved durable terminal metadata/checkpoint schema; live PTYs are currently Electron-owned. |
| `run_attachments` | Reserved typed run evidence schema. |
| `approvals` | Reserved append-only run/agent/terminal decision schema. |

New records use UUIDs. Existing `sess_*` conversation IDs remain external
links and are not migrated into a new ID format.

### Project bootstrap and run creation

At startup, Electron calls `POST /v1/workspaces/eva-ready`. The bridge creates
and registers **Eva Ready Workspace**, including an initial README and a local
Git identity. This allows a coding run without a repository picker. The picker
remains available for a user-owned Git repository; non-Git folders are
rejected.

`POST /v1/workspaces/runs`:

1. Validates the project UUID, objective, base ref, chat ID, and model policy.
2. Resolves the base commit with argument-array Git; option-like refs are
   rejected.
3. Creates branch `eva/run-<short-id>` and a worktree below the managed runtime
   root, never inside the source working tree.
4. Persists `Checkout` and `CodingRun` records.
5. Reserves a workspace subagent slot, persists an `AgentRun`, and starts a
   dedicated ACP client with the worktree as its real `cwd`.

If agent capacity is full, the worktree/run remain durable and the response
contains a dispatch error. Standalone retries incomplete active runs on the
next startup. Tests may set `EVA_WORKSPACE_AGENT_AUTODISPATCH=0`; production
defaults to automatic dispatch.

### Workspace agent execution

Workspace agents reuse the observable subagent registry, so the same task is
visible in Agent Operations with `coding_run_id`, `checkout_id`, and
`capability_policy`. Unlike generic subagents, the worker creates its
`ACPClient` with the assigned worktree and uses prompt permission mode
`workspace_write`.

ACP `session/request_permission` requests are automatically allowed once only
for `read`, `search`, `fetch`, and `think` tool kinds when the active workspace
prompt offers an `allow_once` option. Execute, edit, delete, and unknown tool
kinds require an explicit permission decision because ACP does not provide a
path contract that can prove worktree confinement. Ordinary chats and generic
subagents retain interactive permission handling; passive recall continues to
reject tools. The automatic mode never accepts persistent `allow_always`
authority.

Live ACP chunks update the task and periodically persist a bounded report.
Plan/tool events update activity. Completion persists the final report and
marks the coding run `completed`; errors remain retryable. Known project and
worktree path prefixes are replaced with `<workspace>` before report text
crosses preload.

### Workspace Monitor and progress narration

`core/js/workspaces.js` implements the full main-window monitor:

- run list with project, objective, agent state, branch, and dirty count;
- selected run context, policy, linked session, terminal/chat actions, and
  bounded final report;
- activity history capped at 60 events;
- observation polling every 10 seconds;
- dispatch, state transition, bounded live report, completion, and failure
  narration;
- significant transition speech only when Auto Speak is enabled, with a
  two-minute voice rate limit and Local Voices readiness check;
- five-minute heartbeat summaries for active work.

Polling is observation-only. List IPC does not grant terminal roots or start an
agent. Terminal authority is resolved only after an explicit terminal-open
action.

### Electron terminal broker

The terminal uses `node-pty`, `@xterm/xterm`, fit, search, and web-links addons.
Preload exposes only:

```text
terminal-list | terminal-create | terminal-replay | terminal-write
terminal-resize | terminal-close | terminal-close-root
```

`TerminalBroker` owns approved opaque roots, shell selection, a secret-stripped
environment, PTY size, sequence/exit state, and a bounded 1 MiB UTF-8 replay
tail. Input writes are capped at 64 KiB.

Workspace roots are bridge-revalidated and re-registered on every terminal
creation. Immediately before spawn, the broker rejects a root or intermediate
component that became a symlink. The packaged app root alone may canonicalize
through AppImage links. Unix PTY session identity is captured at creation so
descendants can receive SIGTERM and bounded SIGKILL escalation even if the PTY
parent exits first; Windows uses `taskkill` process-tree termination.

Outside the monitor, Terminal is a horizontally resizable left surface with an
expand control. Inside the monitor it is a vertically resizable lower dock
(46% viewport height; 72% expanded). Opening Terminal from the run context or
sidebar keeps Workspace Monitor visible. xterm rows are explicitly left
aligned.

### Lifecycle and cleanup

Completed worktrees remain available for review. Archive retains the checkout.
Discard is explicit and:

1. Refreshes and verifies the run-to-checkout relationship.
2. Refuses while an agent is starting, running, or steering.
3. Terminates every PTY and descendant process for the checkout.
4. Refuses dirty cleanup without explicit confirmation.
5. Removes the Git worktree and generated run branch.
6. Marks the run/checkout disposed and revokes terminal-root authority.

If a worktree was removed outside Eva, cleanup prunes stale worktree metadata,
verifies it is gone, removes the branch, and completes the durable disposition.
Missing or replaced runtime/project/worktree components that are symlinks are
rejected rather than followed.

### Unified Assets

`core/js/assets.js` is a main-window library combining generated files from
`ARTIFACTS_DIR` and changed files from retained coding runs. Workspace files
include committed and uncommitted differences relative to the checkout's
recorded base revision.

Renderer metadata contains source, run/checkout IDs, project/objective labels,
relative path, size, modification time, and agent status, never an absolute
path. Opening a workspace file is an Electron IPC operation: main requests
capability-protected resolution of the run plus relative path, then calls
`shell.openPath()`.

Resolution rejects traversal, symlinked runtime/project/run components,
intermediate and leaf symlinks, missing/non-regular files, and disposed runs.
Terminal root registration applies an independent equivalent check.

### Main-view navigation

Durable collections and workflows use the main work surface; contextual tools
dock; small configuration/identity actions use existing settings or overlays.

| Destination | Current behavior |
|---|---|
| Eva / New Chat | Main conversation or voice view. |
| Agents | Full Agent Operations view. |
| Assets | Full generated/workspace Assets library. |
| Skills | Full searchable library/editor with Active/Draft/Disabled and provenance-aware source filters, tags/tools search, and sorting. |
| Workspaces | Full Workspace Monitor. |
| Terminal | Lower dock in Workspace Monitor; resizable side surface elsewhere. |
| Prompts / Models / Settings | Central Settings workspace. |
| Sessions | Legacy drawer pending main-view migration; old snapshots restore visibly. |
| Profile | Local identity overlay; planned move into Settings. |

Skills moves, rather than clones, its importer/editor nodes, retaining
paste/URL/GitHub/file import, Eva'rise preview, edit, enable/disable, delete,
and auto-learned Draft behavior. Source filters normalize persisted provenance
such as `github:owner/repo`, `file:name`, and `url:https://...` while preserving
the complete value for display and search.

### Workspace HTTP and IPC contracts

Every `/v1/workspaces/*` endpoint requires normal private bridge authorization
and the main-process-only `X-Eva-Workspace-Capability` header. The token is
generated at startup, passed only to the bridge process environment, and never
exposed in preload.

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/workspaces/eva-ready` | POST | Idempotently create/register Eva Ready Workspace. |
| `/v1/workspaces/projects` | GET/POST | List projects or register a picker-approved Git root. |
| `/v1/workspaces/runs` | GET/POST | List runs or create a worktree and dispatch its agent. |
| `/v1/workspaces/runs/<id>` | GET | Read one run with latest AgentRun. |
| `/v1/workspaces/runs/<id>/dispatch` | POST | Idempotently dispatch/recover an active run. |
| `/v1/workspaces/runs/<id>/archive` | POST | Archive while retaining checkout. |
| `/v1/workspaces/runs/<id>/discard` | POST | Explicit safe cleanup. |
| `/v1/workspaces/checkouts/<id>/status` | GET | Revalidate and summarize checkout. |
| `/v1/workspaces/assets` | GET | List changed files from retained runs. |
| `/v1/workspaces/assets/resolve` | POST | Resolve a run-relative regular file for main. |

Electron preload exposes `workspaceListProjects`, `workspaceSelectProject`,
`workspaceCreateRun`, `workspaceListRuns`, `workspaceRunAction`,
`workspaceListAssets`, and `workspaceOpenAsset`. These return renderer-safe
projections and do not accept absolute paths.

### Current limitations

- Linux AppImage is the proving ground; macOS and Windows require native
  `node-pty` package validation in platform CI.
- Live terminal metadata is not restored from `terminal_sessions` after an
  Electron process restart; renderer refresh reconnect is implemented.
- ACP is the first workspace runtime. A provider-neutral adapter interface is
  the intended boundary for future Cline/Gemini/Codex/Goose integrations.
- Multi-agent sibling worktrees, durable approval/audit UI, diff review, patch
  export, explicit commit/merge, browser evidence, and Eva Field remain later
  milestones.
- Sessions is the next sidebar destination planned for full main-view
  migration.

## Skills System

Skills are reusable instruction sets that Eva matches to user requests via
semantic similarity and injects into context.

### Skill Schema

```json
{
  "SkillId": "sk-a1b2c3d4e5f6",
  "Name": "Deploy to Kubernetes",
  "Description": "When the user asks to deploy an app to a Kubernetes cluster",
  "Instructions": "## Steps\n1. Check the deployment manifest...",
  "Tools": "browser, kusto",
  "Tags": "kubernetes, deploy, devops",
  "Source": "github:owner/repo",
  "Status": "active",
  "CreatedAt": "2026-06-14T12:00:00Z",
  "UpdatedAt": "2026-06-14T12:00:00Z"
}
```

### Import Sources

| Source | Input | Processing |
|---|---|---|
| Paste | Raw text | Direct to evarise |
| URL | HTTP(S) URL | SSRF-safe fetch (IP pinning, public-only) |
| GitHub | owner/repo or full URL | Try SKILL.md, skill.md, README.md |
| File | Upload (<= 200 KB) | Client-side FileReader |

### Evarise Normalization

Raw skill text is sent to an LLM with a strict prompt that treats the source as
untrusted data (prevents prompt injection). The model extracts name, description,
instructions, tools, and tags as a JSON object. Parsing handles `<think>` blocks
(Qwen, DeepSeek), code fences, and balanced-brace extraction. Falls back to
LM Studio when ACP is unavailable.

### Auto-Learn

After complex tasks, Eva can auto-extract a skill from the conversation
context via `/v1/skills/auto-learn`. The extracted skill is stored as a draft for
user review.

## Background System

### Memory Consolidation

When cognition and a memory backend are configured, the bridge starts an internal
background loop (default: every 2 hours, pauses within 120s of user activity).

**Job types (13 total):**

| Job | Description |
|---|---|
| `memory_consolidation` | Summarize recent conversations -> MemorySummaries |
| `goal_checkin` | Review active goals, update status (max 2/tick) |
| `daily_digest` | Compile day's activity summary |
| `knowledge_hygiene` | Revalidate old facts, trim Confidence < 0.3 |
| `reflection_synthesis` | Combine 3+ related reflections into new insights |
| `emotion_drift` | Detect significant mood changes (threshold 0.15) |
| `token_telemetry` | Aggregate token usage stats |
| `proactive_briefing` | Suggest upcoming relevant content |
| `market_snapshot` | Stock/crypto updates for watched symbols |
| `sec_filing_watch` | Check watched symbols for new SEC filings |
| `space_weather_alert` | Space weather alerts (Kp, G, R, S indices) |
| `research_deepdive` | Deep-dive on research topics |
| `alert_watch` | Check alert rules for triggers |

The bridge runs all 13 registered jobs. Settings exposes 12 direct job toggles;
`alert_watch` is controlled through the alert subsystem rather than a separate
background-job checkbox.

**Hands-off application:** Every job proposal is applied immediately through
`_apply_proposal_payload()`, regardless of its legacy `auto_apply` hint. The bridge
then stores an `applied` or `failed` row in `BackgroundProposals` and records the
tick in `BackgroundActivity`. The proposal approval/rejection endpoints remain
available for compatible handling of pending or historical records, but normal
background ticks do not wait for human review.

### Cron Scheduler

5-field cron expressions (minute, hour, day-of-month, month, day-of-week). Supports ranges (`1-5`), steps (`*/15`), and lists (`1,3,5`).

```json
{
  "id": "cron-abc12345",
  "enabled": true,
  "label": "Morning briefing",
  "prompt": "Prepare my morning briefing with weather and news",
  "schedule": "0 7 * * 1-5",
  "last_run": "2026-06-14T07:00:00Z",
  "next_run": "2026-06-15T07:00:00Z"
}
```

Tasks execute by sending the prompt through ACP and delivering results as notifications.

### Alert System

Alert rules trigger on conditions and deliver notifications:

| Type | Params | Description |
|---|---|---|
| `sec_filing` | symbols (max 12) | SEC filing watch |
| `weather` | location, condition | Weather alerts |
| `space_weather` | threshold | Kp, G, R, S index alerts |
| `keyword_watch` | topic | Topic monitoring |
| `research_question` | question | Recurring research probes |

Cooldown: 1-20160 minutes (default 1440/24 hours). Rate limit: 8 per hour.
Quiet hours configurable. Channels: `chat`, `voice`, or `signal`.

### Signal Messaging

Eva can send text messages via Signal using signal-cli (native binary, no Java).

**Setup:**
1. Install signal-cli (v0.14.5+ native binary at `~/.local/bin/signal-cli`, or let `install.sh` handle it)
2. Link to your Signal account: `signal-cli link -n "Eva"` and scan the QR code from Signal mobile
3. Enter sender and recipient numbers in Settings > Auth

**How it works:**
- The `[[EVA_SIGNAL]]` marker in the system prompt tells the model to emit `[[EVA_SIGNAL]]{"message":"..."}[[/EVA_SIGNAL]]`
- The final-response renderer accepts the marker only for an affirmative Signal request and posts it to the authenticated loopback bridge
- The bridge calls `signal-cli -u <sender> send -m <message> <recipient>` exactly once
- The frontend strips the marker and reports the real delivery success or failure

**Signal request examples:** `send me a Signal message`, `text my phone`, `notify me on Signal`, `Signal me the result`, `message me on Signal`

**Configuration persisted to:** `~/.config/eva-standalone/alerts.json` (signal_sender, signal_recipient fields)

**Camera/Signal conflict prevention:** When signal keywords match, camera REMINDER and camera fallback injection are suppressed to prevent spurious `[[EVA_LOOK]]` markers.

## Telemetry

Structured, privacy-safe event logging. Records durations, model names, and
routing decisions only. Never records message content, tokens, keys, or MCP env values.

**Events:** `acp_pool` (hit/warm/evict/miss), `acp_prompt` (model, ms, chars), `error` (category, message), `cognition_turn` (draft/review/revise timing)

**Storage:** JSONL file at `~/.config/eva-standalone/telemetry.jsonl` (rotates at 5 MB). In-memory ring buffer (300 events) for `/v1/telemetry`.

**Debug log:** `~/.config/eva-standalone/bridge_debug.log` (rotates at 10 MB). In-memory ring buffer (200 lines) for `/v1/logs`.

## Settings Panel

Eight tabs in a modal overlay:

| Tab | Contents |
|---|---|
| **General** | Theme, TTS engine/voice, auto-speak, camera presence, vision provider, data retrieval mode (cloud/local) with status |
| **Models** | Model selector (grouped by provider), temperature, max tokens, reasoning effort, AIG backend selector, ACP model selector, adaptive review toggle and reviewer model |
| **Auth** | API key inputs with show/hide toggles, ACP bridge URL, Signal sender/recipient numbers. Standalone encrypts provider keys with Electron safeStorage so they survive AppImage rebuilds. |
| **Prompts** | Personality presets (Default/Concise/Advanced/Terminal/Custom), editable system prompt textarea |
| **Goals** | Goals list with create/edit/delete |
| **Background** | Background loop status, enable/interval controls, run-once, proposal audit/history, approval/rejection controls for pending records, recent activity |
| **Cron** | Cron task list with create/edit/delete, schedule expression, prompt, last/next run timestamps |
| **MCP** | Azure MCP, GitHub MCP, Kusto MCP toggles with config fields. Apply/refresh buttons |

Skills are managed in a full main-window library/editor. It supports paste,
URL, GitHub, and file import; Eva'rise preview; edit/reimport;
enable/disable/delete; Active/Draft/Disabled filtering; normalized source
categories with full provenance; tag/tool search; name/status/update sorting;
and responsive organization controls.

The sidebar profile picker keeps sessions, prompts, model choices, voice preferences, and other browser-local settings separate per user. API credentials, MCP configuration, and the selected memory backend remain shared installation settings. Sessions open fresh on launch and support persistent custom titles. Saved skills can be edited and reimported through the existing skill ID, preserving database history.

Reasoning-capable models expose **Model default**, **None**, **Minimal**, **Low**, **Medium**, **High**, **Extra high**, and **Maximum**. The selection is saved locally and passed to OpenAI, GitHub Models, or Copilot CLI ACP when supported. Higher levels can increase response time and premium usage.

## Deployment

### Browser only

```bash
cp config.example.json config.json   # add your API keys
xdg-open index.html                  # or open in any browser
```

For `file://` usage without a JSON loader, copy `config.local.example.js` to `config.local.js`.

### Manual ACP bridge

```bash
python3 tools/acp_bridge.py --port 8888 \
  --enable-kusto-mcp \
  --kusto-cluster "https://<your-cluster>.region.kusto.windows.net" \
  --kusto-database Eva
```

### Standalone (Electron AppImage)

A bundled desktop build that ships the web UI and ACP bridge together. The
Electron shell allocates a free localhost port, starts the bridge, and injects
the URL into the renderer via `window.evaStandalone`.

```bash
cd standalone
npm install
npm run dist
./dist/'Eva Standalone-5.5.8.AppImage'

# Development/review launch with coding workspaces enabled
npm run start:workspace
```

**Electron lifecycle:**
1. `getFreeLocalPort()`: OS-allocated free port
2. Generate private bridge and workspace capability tokens.
3. `startBridge(port)`: Spawn `python3 tools/acp_bridge.py --bind 127.0.0.1 --port <port>` with both tokens in the child environment.
4. `waitForBridge(url, process, timeout)`: Poll `/health` every 500ms.
5. When workspaces are enabled, create Eva Ready Workspace and recover active runs before creating the window.
6. On `EADDRINUSE`: retry with a new port (max 3 attempts).
7. On bridge crash: show an error dialog and quit.

Host prerequisites: Node.js 24+, Python 3.12+, Copilot CLI authenticated (for cloud mode). LM Studio for local-only mode.

### ACP Infrastructure Roadmap (tracking)

Current state (2026-06-15):
- Static web tier can run on legacy 32-bit hosts.
- ACP Bridge currently runs on a separate compatible machine.
- Single-host deployment is blocked until new hardware is available.
- Local mode (LM Studio + direct MCP) works on any x86_64 machine without Copilot CLI.
- Signal messaging available via signal-cli (native binary, linked account required).

| Milestone | Status | Notes |
|---|---|---|
| Provision bridge-capable server | planned | 2+ vCPU, 4+ GB RAM |
| Install runtime baseline | planned | Node.js 24+, Python 3.12+ |
| Authenticate Copilot CLI | planned | `copilot auth login` on target |
| Deploy bridge as systemd service | planned | `tools/acp_setup.sh` |
| Single-host ACP deployment | planned | Keep localhost fallback until complete |
| Post-migration validation | planned | `/health` ok + AIG smoke + `test_eva.py` |
| macOS standalone build | planned | Needs Apple Developer ID + notarization |
| Windows standalone build | planned | Add `win` target to electron-builder |

## Attribution and Design Inspiration

Eva is independently implemented. This section records the projects and open
standards whose ideas inform its design, so credit and architectural intent
remain visible as the coding-workspace work evolves. Inclusion here does not
mean Eva embeds their code, is affiliated with them, or currently supports
their runtime.

| Project or standard | What Eva is studying or adapting | Current status |
|---|---|---|
| [Traycer](https://github.com/traycerai/traycer) | Durable agent identity separate from PTY scrollback, terminal stream replay/backpressure, worktree ownership facts, typed evidence, and capability-based A2A handoffs. | Durable run identity, bounded PTY replay, worktree ownership, and stable monitor IDs are implemented; collaboration canvas and cross-device sync remain deferred. |
| [OpenCode](https://github.com/anomalyco/opencode) | Explicit plan versus build modes, local coding-agent ergonomics, and subagent patterns. | Research candidate; evaluate a structured adapter surface before integration. |
| [Cline](https://github.com/cline/cline) | Plan/Act UX, checkpoints, approval-first execution, SDK extensibility, agent teams, and worktree-based task boards. | Priority adapter research spike. |
| [Gemini CLI](https://github.com/google-gemini/gemini-cli) | Structured headless event streams, checkpoints, trusted folders, sandbox concepts, MCP, and project context files. | Priority process-adapter research spike. |
| [Goose](https://github.com/aaif-goose/goose) | Provider-neutral agent profiles, extension catalog, MCP integration, and embeddable API design. | Research candidate for later runtime and automation adapters. |
| [OpenAI Codex CLI](https://github.com/openai/codex) | Local coding-agent and sandbox/protocol patterns. | Future optional adapter candidate; validate a documented control interface first. |
| [OpenHands Agent Canvas](https://github.com/OpenHands/OpenHands) | Backend catalog, self-hosted agent control-center patterns, and automation UX. | Inspiration only; Eva remains framework-free and local-first. |
| [Aider](https://github.com/Aider-AI/aider) | Repository-map context, test/lint feedback loops, diffs, and undo-oriented Git workflow. | Workflow inspiration; isolated worktrees and focused validation are implemented, while commits remain explicit future review actions. |
| [SWE-agent](https://github.com/SWE-agent/SWE-agent) | Issue-to-patch trajectories and coding-agent evaluation methodology. | Evaluation reference, not a desktop runtime. |
| [Model Context Protocol](https://modelcontextprotocol.io/specification/2026-07-28) | Tool/resource boundaries, capability negotiation, cancellation, consent, and stateless modern stdio protocol design. | Eva supports legacy and modern local stdio tool servers; remote HTTP, OAuth, MRTR continuations, Tasks, Resources, Prompts, and Apps remain separate work. |

### Runtime, Protocol, and Test Foundations

The following projects and standards are direct implementation dependencies,
interoperability targets, or runtime integrations. Eva retains ownership of its
security policy, local persistence, UI, and orchestration; credit here does not
mean it vendors upstream code or inherits an upstream trust boundary.

| Project or standard | Role in Eva | Current status |
|---|---|---|
| [Agent Client Protocol](https://agentclientprotocol.com/overview/introduction) | Structured bridge between Eva and the GitHub Copilot CLI for sessions, streaming, tool events, permissions, and cancellation. | Active integration. ACP terminal requests remain disabled; Eva's Electron PTY broker owns interactive terminals. |
| [GitHub Copilot CLI](https://github.com/github/copilot-cli) | Optional ACP runtime for Copilot-backed chat, AIG, and isolated workspace agents. | Active external runtime; users authenticate it locally with `copilot auth login`. |
| [MCP C# SDK v2](https://github.com/modelcontextprotocol/csharp-sdk) | Protocol reference for stateless HTTP, multi-round-trip input, Tasks, and enterprise service-hosting patterns. | Architecture and future interoperability reference; not bundled or exercised by Eva's current local stdio CI fixtures. |
| [MCP Python SDK v2](https://github.com/modelcontextprotocol/python-sdk) | Maintained reference implementation for native MCP `2026-07-28` stdio interoperability. | Test-only compatibility fixture; not an Eva runtime dependency. |
| [MCP TypeScript SDK v2](https://github.com/modelcontextprotocol/typescript-sdk) | Independent Tier 1 reference implementation for native modern stdio interoperability. | Test-only compatibility fixture; installed in an isolated CI path, not bundled with Eva. |
| [Electron](https://www.electronjs.org/) and [electron-builder](https://www.electron.build/) | Privileged main-process boundary, narrow preload IPC, AppImage packaging, and bundled local bridge lifecycle. | Shipped standalone foundation. |
| [xterm.js](https://github.com/xtermjs/xterm.js) and [node-pty](https://github.com/microsoft/node-pty) | Terminal rendering, measured fitting/search/link addons, native PTY ownership, and process-session cleanup. | Shipped Electron terminal foundation; renderer never receives arbitrary process access. |
| [Playwright](https://playwright.dev/) | Browser automation through CDP and packaged Electron end-to-end coverage. | Shipped browser-agent/test foundation; browser actions remain subject to Eva's approval and execution policy. |
| [Git](https://git-scm.com/) | Canonical repository inspection, isolated worktrees, branch lifecycle, diffs, and cleanup. | Shipped workspace foundation; Eva uses argument arrays and preserves explicit review/commit decisions. |
| [SQLite](https://www.sqlite.org/) | Local durable memory, workspace records, skills, and other bridge-owned state. | Shipped local persistence foundation; browser session display state remains separate. |
| [LM Studio](https://lmstudio.ai/) | Local OpenAI-compatible inference and local MCP tool-calling retrieval. | Supported optional runtime; no cloud model credential is required for local-only operation. |
| [Chatterbox TTS](https://github.com/resemble-ai/chatterbox) | Optional local English and Multilingual V3 synthesis behind Eva's maintained Local Voices adapter. | Installed only with `./install.sh --voice-deps`; Eva requires a configured, user-authorized reference recording for Local Voices synthesis, keeps audio on the loopback/local path, and preserves Chatterbox's upstream generated-audio watermarking behavior. |
| [Azure Data Explorer](https://azure.microsoft.com/products/data-explorer/) / Kusto | Optional cloud memory, analytics, and Kusto MCP data retrieval. | Supported configured integration; local SQLite remains the no-cloud alternative. |
| [PyAutoGUI](https://pyautogui.readthedocs.io/) and [OpenCV](https://opencv.org/) | Optional desktop automation and local camera/presence sensing. | Optional integrations; capability and consent controls remain in Eva. |
| [signal-cli](https://github.com/AsamK/signal-cli) | Optional linked-account Signal delivery. | Optional external integration; Eva reports delivery success or failure rather than assuming it. |

The implemented decision is **Eva control plane + explicit execution broker +
replaceable runtime adapters**. Eva owns run/worktree identity, safety policy,
monitoring, Assets, and review state rather than becoming a branded wrapper
around one CLI. External runtimes are eligible for later adapters only when
they provide a structured lifecycle or SDK, per-worktree `cwd`, interceptable
permissions, cancellation, and resumable session semantics. TUI paint alone is
not a supported agent-state protocol.

## Security

- Bridge binds to `127.0.0.1` by default (localhost only)
- ACP tool permissions are never globally bypassed. Standalone Eva requires an authenticated in-chat decision; hosted/file clients fail closed.
- Workspace routes require a second random capability held only by Electron main; the ordinary renderer-visible bridge token is insufficient.
- Workspace agent `workspace_write` prompts auto-select `allow_once` only for read/search/fetch/think; mutating and unknown tools remain interactive. Normal prompts remain interactive and passive recall remains deny-by-policy.
- Renderer workspace DTOs contain opaque IDs and relative paths only. Known project/worktree paths are redacted from agent reports.
- Managed worktree paths are revalidated under `EVA_CONFIG_DIR/worktrees` before status, Assets, terminal registration, and cleanup. Runtime-root, intermediate, leaf, and post-registration symlink swaps are rejected.
- PTY roots are allowlisted; the renderer cannot provide a cwd/environment. PTY shutdown retains Unix session identity and escalates descendants to SIGKILL before root revocation.
- Routine standing consent is limited to read, search, fetch, and think tool kinds. Execute, edit, move, delete, and unknown operations always require a fresh decision.
- ACP client-terminal capability is disabled because the protocol does not reliably correlate `terminal/create` with a permission decision. Authorized execution uses Eva's browser/desktop agent confirmation paths instead.
- MCP env vars (tokens) are redacted from `/v1/mcp` responses and persisted configs
- URL fetching uses SSRF protection: DNS resolution validated, all IPs must be public, IP pinning prevents DNS rebinding, redirect hops re-validated
- Skill import treats source text as untrusted data (explicit anti-injection prompt)
- LM Studio base URL restricted to localhost/private IPs on whitelisted ports (1234, 8000, 8080, 11434)
- Camera off by default, subprocess-isolated, state read-only from bridge
- Sensitive browser/desktop actions require user confirmation
- `pyautogui.FAILSAFE = True` (mouse to corner = emergency stop)
- Background writes use bounded proposal handlers and retain applied/failed audit records

## CI / Testing

### GitHub Actions (`eva-ci.yml`)

Runs on every PR to `main`:

| Job | Checks |
|---|---|
| **static-checks** | Secret scanning, HTML structure, JS syntax, Python syntax, model routing, config templates, .gitignore |
| **python-tests** | `tools/test_static.py`: file integrity, config safety, CSV logic, model selector, seed validation |

### Test Files

| File | Needs Bridge? | Description |
|---|---|---|
| `tools/test_static.py` | No | CI-safe static tests |
| `tools/test_eva.py` | Yes | 64-check integration suite |
| `tools/test_latency.py` | Yes | Production-shaped AIG latency probe with NDJSON TTFT/total timings, cold/warm repetitions, and optional thresholds |
| `tools/test_latency_fake_server.py` | No | Fake HTTP-server coverage for fast, approval, and revision call paths |
| `tools/test_skills_e2e.py` | Starts local test bridge | Skill import/edit/injection/delete lifecycle with stubbed ACP/Kusto |
| `tools/test_workspaces.py` | No | Workspace schema, worktree isolation/recovery, dirty cleanup, symlink confinement |
| `tools/test_workspaces_e2e.py` | No live model | Real HTTP workspace/AgentRun/Assets lifecycle with deterministic worker |
| `tools/test_terminal_broker.js` | No | PTY root confinement, replay, resize, descendant cancellation, root swaps |
| `tools/test_workspace_projection.js` | No | Report path redaction |
| `tools/test_terminal_e2e.js` | Starts packaged app | Terminal, reconnect, mobile layout, Sessions, Skills main view |
| `tools/test_workspace_electron_e2e.js` | Starts packaged app | Workspaces, terminal dock, Assets, process cleanup, root revocation |
| `tools/eval/run.py --mode mock` | No | Behavioral eval with synthetic responses |
| `tools/eval/run.py --mode live` | Yes | Behavioral eval against live bridge |

```bash
python3 tools/test_static.py                          # CI-safe
python3 tools/test_skills_e2e.py                      # Skills HTTP lifecycle
python3 tools/test_workspaces.py                      # Workspace/Git safety
python3 tools/test_workspaces_e2e.py                  # Workspace HTTP + agent/Assets
python3 tools/test_streaming.py                       # Streaming and ACP permission policy
node tools/test_terminal_broker.js                    # PTY contract
node tools/test_workspace_projection.js               # Report redaction
node tools/test_terminal_e2e.js                       # Packaged terminal/session/Skills UI
node tools/test_workspace_electron_e2e.js             # Packaged workspace/Assets UI
python3 tools/test_latency_fake_server.py              # latency harness without a bridge
python3 tools/test_latency.py --bridge http://localhost:8888 --mode both --repetitions 2
python3 tools/test_eva.py --verbose                   # full integration
python3 tools/eval/run.py --mode mock                 # synthetic eval
python3 tools/eval/run.py --mode live --bridge http://localhost:8888  # live eval
```

### Behavioral Evaluation

Fixtures in `tools/eval/fixtures/` (one JSON per category): identity, style,
refusal, recall, routing, capability, injection_resistance. Mock mode reads
`tools/eval/mock_responses.json`. Results to `tools/eval/results/<timestamp>.json`.

## Session Explorer

`core/js/sessions.js` + `core/js/idb-store.js`:

- **Storage:** IndexedDB (`eva_sessions_db`) with `sessions` + `blobs` object stores
- **Auto-save** after every response, auto-restore on page load
- **Session index** in localStorage (lightweight), full snapshots in IndexedDB
- **Migration** from localStorage on first load, plus per-session fallback when a
  legacy `session_<id>` snapshot remains after migration
- **Persistent storage** via `navigator.storage.persist()`
- **Transactional switching:** the current session save finishes before the
  target IndexedDB record is read; successful loads restore chat/model state,
  close Session Explorer, and exit Agent Operations/Workspace Monitor
- **Legacy visible restore:** snapshots without `_htmlSnapshot` reconstruct a
  safe text transcript from provider messages or `masterOutput`. An index entry
  whose snapshot is unavailable renders an explicit notice instead of leaving
  the welcome screen unchanged.
- **Row activation:** delegated mouse and Enter/Space activation works from the
  title, timestamp, or empty row space; rename/pin/delete buttons remain isolated
- **Sidebar coordination:** Agents, Assets, Skills, and Workspaces are primary
  main views. Prompts/Models/Settings use the Settings workspace. Terminal is a
  contextual dock in Workspace Monitor and a resizable side surface elsewhere.
  Sessions remains a legacy drawer pending main-view migration.

## LCARS Theme

Star Trek-inspired interface (Lower Decks palette):

- Barlow Condensed font (Google Fonts)
- LCARS elbows (curved connectors via CSS pseudo-elements)
- Flat colored sidebar chips with black gaps
- Accent-border chat bubbles (cyan=Eva, blue=User)
- Monitor dock with 4 tabs (Tokens, Network, Session, System)

---
Based on [CodeProject](https://www.codeproject.com/Articles/5350454/Chat-GPT-in-JavaScript). Heavily extended.
