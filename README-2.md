# Technical Documentation

Detailed architecture, dependencies, and implementation notes for Eva AI Assistant.

> **Recommended experience:** Select **Eva (AIG)** from the model dropdown for the full
> Eva experience: persistent memory, emotion tracking, proactive data retrieval, and
> intelligent cross-model orchestration. All other models work standalone, but AIG is the
> way Eva was designed to be used.

## Providers

| Provider | Models |
|---|---|
| Eva (AIG) | Multi-agent orchestration over GitHub Models, ACP, and LM Studio |
| OpenAI | GPT-4o, GPT-4o Mini, o1, o1-preview, o1-mini, o3-mini, latest |
| GitHub Copilot (PAT) | GPT-4o, GPT-4o Mini, o3-mini, GPT-5, o4-mini, DeepSeek-R1, Llama 4 Maverick |
| GitHub Copilot (ACP) | Claude, GPT-5.x, GPT-4.1 via Copilot CLI |
| Google Gemini | Gemini 2.0 Flash (Thinking Exp) |
| LM Studio | Any local OpenAI-compatible model (fully offline) |
| gpt-image-1 | Image generation |

## Highlights

- Multi-agent AIG with eva and reviewer cognitive pipeline
- Dual-mode data retrieval: cloud (Copilot CLI + MCP) or local (LM Studio + direct MCP)
- MCP tool access (Kusto, GitHub, Azure, web search) hot-reloadable at runtime
- Persistent memory via Azure Data Explorer (Kusto) or local SQLite
- Autonomous browser control (Playwright + CDP) and desktop control (pyautogui)
- Webcam presence detection (OpenCV face + motion)
- Inline image search (Wikimedia) and generation (gpt-image-1)
- Downloadable artifact creation (PDF, text, CSV, markdown) with auto-open
- Skill import/normalization from paste, URL, GitHub, or file upload
- Background memory consolidation with human-in-the-loop proposals
- Cron scheduler for recurring tasks (briefings, checks, reminders)
- Alert system (SEC filings, weather, space weather, keyword watch)
- TTS: OpenAI (default), browser, Bark, Amazon Polly
- LCARS and Eva themes
- Standalone Electron AppImage with bundled bridge
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

### Request Flow

**Direct models (OpenAI, Copilot PAT, Gemini):**
Browser -> XHR/fetch -> Provider API -> JSON response -> `renderEvaResponse()`

**ACP models (Copilot CLI):**
1. Browser -> `POST /v1/chat/completions` -> ACP Bridge (HTTP)
2. Bridge -> `session/prompt` -> Copilot CLI (JSON-RPC over NDJSON/stdio)
3. Copilot may invoke MCP tools (bridge auto-grants permissions)
4. Copilot streams `session/update` notifications with text chunks
5. Bridge accumulates chunks -> returns OpenAI-compatible JSON response

**LM Studio (local):**
1. Browser fetches `/v1/memory/context` + `/v1/data/retrieve` in parallel from bridge
2. Bridge injects memory context from SQLite/Kusto
3. Bridge runs data retrieval via local MCP tool-calling loop (see below)
4. Browser prepends memory + data to system prompt
5. Browser sends directly to `http://localhost:1234/v1/chat/completions`
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
mcp.json                   MCP server configuration (gitignored)

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
    dalle3.js              Image generation via gpt-image-1 (dalle3Send)
    idb-store.js           IndexedDB storage backend (sessions + blobs)
    sessions.js            Session persistence and management
    voice.js               Wake-word "Eva" via Web Speech API
    camera.js              Webcam capture for [[EVA_LOOK]] vision
    browser-agent.js       Frontend integration for browser agent runs
    pandora.js             Pandora box / Easter egg system
    skills.js              Skill import UI (paste/URL/GitHub/file upload)
    external.js            External data fetching at page load

tools/
  acp_bridge.py            Entry point (imports bridge/ package)
  bridge/
    __init__.py
    __main__.py            Allows `python -m bridge`
    core.py                Main HTTP server, AIG pipeline, all endpoints (~3800 lines)
    acp_client.py          ACPClient: Copilot CLI subprocess, JSON-RPC, model pool
    cognition.py           Memory context builder, entity extraction, emotion computation
    memory.py              Backend switching (Kusto/SQLite), embeddings, synonyms
    skills.py              Skill import, evarise normalization, SSRF-safe URL fetching
    kusto.py               Azure Data Explorer queries, ingest, token management
    config.py              All constants, paths, thresholds, table schemas
    state.py               Mutable runtime state (thread-safe)
    local_mcp.py           Local MCP client, tool-calling agent loop
    background.py          Background job system (12 job types, proposals)
    cron.py                Cron scheduler (5-field expressions)
    alerts.py              Alert/notification system (SEC, weather, space weather)
    telemetry.py           Structured event logging (latency, routing decisions)
    utils.py               URL validation, LM Studio validation, config persistence
  web_search_mcp.py        MCP server: DuckDuckGo + Google fallback (no API key)
  sqlite_memory.py         SQLite memory backend (SqliteMemory class)
  kusto_mcp.py             MCP server for Azure Data Explorer (10 tools)
  browser_agent.py         Autonomous web browsing (Playwright + CDP)
  desktop_agent.py         Autonomous desktop control (pyautogui + vision)
  camera_sense.py          Webcam presence detection (OpenCV face + motion)
  barkTTS_server.py        Suno Bark TTS engine server (GPU)
  eva_seed.kql             Sanitized database seed (public-safe)
  acp_bridge.service       Systemd unit file
  acp_setup.sh             One-command installer
  test_static.py           CI-safe static tests
  test_eva.py              Integration tests (64 checks)
  test_latency.py          Latency benchmarks
  test_skills_e2e.py       Skill import end-to-end tests
  eval/                    Behavioral eval harness

standalone/
  main.js                  Electron shell: port allocation, bridge spawn, health polling
  preload.js               Context bridge (exposes evaStandalone API to renderer)
  package.json             Electron + electron-builder config (v5.3.0)
```

## Dependencies

### Browser-side (no install needed)
- Barlow Condensed font (loaded from Google Fonts CDN)
- AWS SDK v2.1304.0 (bundled, for Polly TTS)

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

### API Keys
| Key | Used by | Get it from |
|---|---|---|
| `OPENAI_API_KEY` | OpenAI models, DALL-E 3, embeddings | [platform.openai.com](https://platform.openai.com/api-keys) |
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
| `session/request_permission` | Agent -> Client | Request tool execution permission (auto-granted) |
| `session/cancel` | Client -> Agent | Cancel ongoing operation |
| `terminal/create` | Agent -> Client | Execute shell command |
| `terminal/output` | Agent -> Client | Get command output |
| `terminal/release` | Agent -> Client | Release terminal |

### ACP Client Pool

The bridge maintains a pool of up to 4 `ACPClient` instances (one per model). Each client is a separate `copilot` subprocess with its own conversation session.

```python
acp_pool: dict[model_key -> ACPClient]   # keyed by model name
acp_pool_order: list[model_key]          # LRU eviction order
acp_pool_lock: threading.RLock()         # thread-safe access
ACP_POOL_MAX = 4
```

When a request arrives for a model not in the pool, the bridge spawns a new Copilot CLI process (`copilot --acp --stdio`), runs the ACP `initialize` + `session/new` handshake, and registers it in the pool. If the pool is full, the least-recently-used client is evicted and its subprocess terminated.

### Available ACP Models

Models available through the Copilot CLI (requires a GitHub Copilot license). The
catalog evolves; this list reflects a recent `copilot --list-models` output.

| Provider | Model ID | Notes |
|---|---|---|
| **Anthropic** | `claude-opus-4.8` | AIG backend only |
| | `claude-opus-4.7` | Variants: `-high`, `-xhigh` |
| | `claude-opus-4.6` | Variant: `-1m` (1M context) |
| | `claude-opus-4.5` | |
| | `claude-sonnet-4.6` | Default AIG backend |
| | `claude-sonnet-4.5`, `claude-sonnet-4` | |
| | `claude-haiku-4.5` | Fastest Claude |
| **OpenAI** | `gpt-5.5` | |
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
| `/v1/memory/seed` | POST | Seed database tables |

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

**Background:**

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/background/status` | GET | Loop status, interval, last tick |
| `/v1/background/control` | POST | Enable/disable, change interval, run now |
| `/v1/background/proposals` | GET | Pending memory consolidation proposals |
| `/v1/background/proposals/<id>/approve` | POST | Apply a proposal |
| `/v1/background/proposals/<id>/reject` | POST | Reject a proposal |
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
| `/v1/notifications` | GET | Unseen notifications |

**MCP:**

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/mcp` | GET | Active MCP servers (secrets redacted) |
| `/v1/mcp/configure` | POST | Restart copilot with new MCP config |

**Browser/Desktop Agents:**

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/browser/launch` | POST | Start autonomous browser task |
| `/v1/browser/<id>/status` | GET | Run status + latest screenshot |
| `/v1/browser/<id>/confirm` | POST | Answer confirmation prompt |
| `/v1/desktop/launch` | POST | Start autonomous desktop task |
| `/v1/desktop/<id>/status` | GET | Run status + latest screenshot |

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
```

**Lifecycle:**
1. `start()`: Spawn process, send `initialize` handshake (protocol 2024-11-05), send `notifications/initialized`, discover tools via `tools/list`
2. `call_tool(name, arguments, timeout)`: Send `tools/call` JSON-RPC, parse content response
3. `stop()`: Terminate process

**Threading:** Background reader thread per server matches JSON-RPC responses by ID. Stderr is logged to bridge debug log.

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
| `BackgroundProposals` | ProposalId, JobType, TargetTable, Payload, Status, ... | Human-reviewed memory proposals |
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
| `[User Profile]` | Always | Knowledge where Entity="User", Confidence >= 0.5 |
| `[Morning Reflection]` | First msg of day | MemorySummaries (latest 3) |
| `[Memory: Core Facts]` | Always | Knowledge where Confidence >= 0.6 (top 15) |
| `[Active Goals]` | When present | Goals where Status="active" (top 10) |
| `[Active Skill: ...]` | On semantic match | Skills matched by embedding similarity or keyword |
| `[Init: First Conversation]` | Empty Knowledge | Introduction prompts |
| `[Emotion State]` | Always | Latest EmotionState row |
| `[Memory: Relevant]` | On keyword match | Lexical + semantic recall against user message |

**Skills manifest (always injected):**
- data-retrieval, weather-news, web-search
- browser-control ([[EVA_BROWSER]]), desktop-control ([[EVA_DESKTOP]]), camera-vision ([[EVA_LOOK]])
- file-creation ([[EVA_ACTION]] file.download)
- image-search, image-generation
- persistent-memory (table list)
- cron-scheduling, skill-learning

**Skill matching:** When a user message arrives, all active skills are compared by
embedding cosine similarity (threshold 0.30, OpenAI `text-embedding-3-small`).
Up to 2 matching skills have their instructions injected (capped at 1500 chars each).
Falls back to lexical keyword matching if embeddings are unavailable.

### Entity Extraction

Post-response reflection extracts facts using strict regex patterns:

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
| greeting/trivial | "hi", "thanks", etc. (<=4 words) | Skip data retrieval |
| meta-question | "what can you do", "who are you" (<=6 words) | Skip data retrieval |
| news-search | "news", "headlines", "breaking" | Web search prompt |
| weather-search | "weather", "forecast", "temperature" | Web search prompt |
| financial-data | "stock", "price", "$TICKER" | Web search prompt |
| kusto-query | KQL keywords, table names | Kusto tool prompt |
| web-search | "search", "look up", "find" | Web search prompt |
| general | Everything else | General-purpose prompt |

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

### Browser Cognitive Layer (`core/js/cognition.js`)

Opt-in via Settings > Models > **Enable Cognitive Layer**. When active, every Eva
(AIG) turn is routed through two role-specific agents:

```
User turn
  -> Eva (plans, drafts answer, may emit action blocks)
     -> Reviewer (verdict: APPROVE | REQUEST_CHANGES)
        -> Eva (revises against feedback) ... up to cogMaxCycles
  -> executeActions(): runs any [[EVA_ACTION]] blocks
  -> renderEvaResponse(): renders the final approved draft
```

Each agent calls `/v1/aig/chat` independently with its own model and editable
system prompt, so users can mix providers (Claude for planning, GPT for drafting,
a smaller model for review).

**Activation:**

| Trigger | Behavior |
|---|---|
| Settings toggle on | Layer runs for every AIG turn |
| Phrase in user message | Force-enabled for that single turn |
| Neither | Single-shot AIG path; system note prevents fabricated phase narration |

Trigger phrases: `trigger the chain`, `use cognition`, `use the cognitive layer`,
`run eva`, `run the reviewer`, `engage cognition`, `cognition: on`.

**Configuration (localStorage):**
- `cogEnabled`: "0" or "1"
- `cogEvaModel`: model name for draft agent
- `cogReviewerModel`: model name for review agent
- `cogMaxCycles`: review iterations (0-3, default 1)
- `cogEvaPrompt` / `cogReviewerPrompt`: editable system prompts
- `cogShowTrace`: show draft/review trace in output

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

**Safety:** Sensitive actions (buy, purchase, payment, checkout) require user confirmation before execution. The run parks and waits for approval via `/v1/browser/<id>/confirm`.

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

**Job types (12 total):**

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
| `space_weather` | Space weather alerts (Kp, G, R, S indices) |
| `research_deepdive` | Deep-dive on research topics |
| `alert_watch` | Check alert rules for triggers |

**Human-in-the-loop:** The loop never writes directly to memory tables. It creates
proposals in `BackgroundProposals` with status `pending`. A human reviews them in
Settings > Background. Approval writes the payload; rejection marks it rejected.

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
Quiet hours configurable. Channels: `chat` or `voice`.

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
| **General** | Theme, TTS engine/voice, auto-speak, data retrieval mode (cloud/local) with status |
| **Models** | Model selector (grouped by provider), temperature, max tokens, reasoning effort, AIG backend selector, ACP model selector, cognitive layer controls (toggle, per-agent model selectors, max cycles, editable prompts, debug trace) |
| **Auth** | API key inputs with show/hide toggles, ACP bridge URL, LM Studio base URL and model name |
| **Prompts** | Personality presets (Default/Concise/Advanced/Terminal/Custom), editable system prompt textarea |
| **Goals** | Goals list with create/edit/delete. Skills list with import (paste/URL/GitHub/file), evarise preview, enable/disable |
| **Background** | Background loop status, enable/interval controls, run-once, proposal approval/rejection, recent activity |
| **Cron** | Cron task list with create/edit/delete, schedule expression, prompt, last/next run timestamps |
| **MCP** | Azure MCP, GitHub MCP, Kusto MCP toggles with config fields. Apply/refresh buttons |

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
./dist/'Eva Standalone-5.3.0.AppImage'
```

**Electron lifecycle:**
1. `getFreeLocalPort()`: OS-allocated free port
2. `startBridge(port)`: Spawn `python3 tools/acp_bridge.py --bind 127.0.0.1 --port <port>`
3. `waitForBridge(url, process, timeout)`: Poll `/health` every 500ms
4. On `EADDRINUSE`: retry with new port (max 3 attempts)
5. On bridge crash: show error dialog and quit

Host prerequisites: Node.js 24+, Python 3.12+, Copilot CLI authenticated (for cloud mode). LM Studio for local-only mode.

### ACP Infrastructure Roadmap (tracking)

Current state (2026-06-14):
- Static web tier can run on legacy 32-bit hosts.
- ACP Bridge currently runs on a separate compatible machine.
- Single-host deployment is blocked until new hardware is available.
- Local mode (LM Studio + direct MCP) works on any x86_64 machine without Copilot CLI.

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

## Security

- Bridge binds to `127.0.0.1` by default (localhost only)
- `--allow-all-tools` bypasses ACP permission prompts (required for non-interactive MCP)
- Terminal commands execute with bridge process's user permissions
- MCP env vars (tokens) are redacted from `/v1/mcp` responses and persisted configs
- URL fetching uses SSRF protection: DNS resolution validated, all IPs must be public, IP pinning prevents DNS rebinding, redirect hops re-validated
- Skill import treats source text as untrusted data (explicit anti-injection prompt)
- LM Studio base URL restricted to localhost/private IPs on whitelisted ports (1234, 8000, 8080, 11434)
- Camera off by default, subprocess-isolated, state read-only from bridge
- Sensitive browser/desktop actions require user confirmation
- `pyautogui.FAILSAFE = True` (mouse to corner = emergency stop)
- Background proposals require human approval before writing to memory

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
| `tools/test_latency.py` | Yes | Latency benchmarks |
| `tools/test_skills_e2e.py` | Yes | Skill import end-to-end |
| `tools/eval/run.py --mode mock` | No | Behavioral eval with synthetic responses |
| `tools/eval/run.py --mode live` | Yes | Behavioral eval against live bridge |

```bash
python3 tools/test_static.py                          # CI-safe
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
- **Migration** from localStorage on first load
- **Persistent storage** via `navigator.storage.persist()`

## LCARS Theme

Star Trek-inspired interface (Lower Decks palette):

- Barlow Condensed font (Google Fonts)
- LCARS elbows (curved connectors via CSS pseudo-elements)
- Flat colored sidebar chips with black gaps
- Accent-border chat bubbles (cyan=Eva, blue=User)
- Monitor dock with 4 tabs (Tokens, Network, Session, System)

---
Based on [CodeProject](https://www.codeproject.com/Articles/5350454/Chat-GPT-in-JavaScript). Heavily extended.
