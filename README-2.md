# Technical Documentation

Detailed architecture, dependencies, and implementation notes for Eva AI Assistant.

> **Recommended experience:** Select **Eva (AIG)** from the model dropdown for the full
> Eva experience: persistent memory, emotion tracking, proactive data retrieval, and
> intelligent cross-model orchestration. This containment release requires Eva Standalone;
> all direct cloud models use its trusted main-process provider broker.

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
- Canonical persistent memory via local SQLite with optional consent-aware Azure Data Explorer/Kusto projection
- Signal delivery for configured trusted alerts (send-only via signal-cli; model text has no dispatch authority)
- Bounded browser control (isolated Playwright + DNS-pinned egress) and launch-only desktop containment
- Webcam presence detection (OpenCV face + motion)
- Inline image search (Wikimedia) and generation (gpt-image-1)
- Downloadable artifact creation (PDF, text, CSV, markdown) with manual trusted Download controls
- Skill import/normalization from paste, URL, GitHub, or file upload
- Background memory consolidation with human-in-the-loop proposals
- Cron scheduler for recurring tasks (briefings, checks, reminders)
- Alert system (SEC filings, weather, space weather, keyword watch) with Signal delivery
- Atomic mode/MCP persistence across restarts (bridge-side runtime_state.json)
- TTS: OpenAI (default), browser, Bark, Amazon Polly
- LCARS and Eva themes (7 Eva variants)
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
|  | Direct  | | PAT API  | | Direct | | ACP/AIG   | | via Bridge  |       |
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
- Optional web search via `web_search_mcp.py` when the `cloud` egress policy permits it
- No cloud model inference; network access is governed independently by `EVA_EGRESS_MODE`

**Mode persistence:** The selected mode and canonical secret-free MCP selection are
persisted atomically to `~/.config/eva-standalone/runtime_state.json` by the bridge.
On startup, the bridge accepts only the complete versioned document; malformed or
partial state fails closed rather than combining legacy mode and MCP files.
The frontend seeds its selector from the bridge via `GET /v1/mode` after init, and
skips auto-switch logic during startup to avoid overriding the persisted choice.

### Security Boundary and Egress Policy

The local/cloud data-retrieval selector is separate from the process-wide egress
policy. `EVA_EGRESS_MODE` accepts exactly `cloud`, `local-network`, or `offline`:

| Policy | Allowed |
|---|---|
| `cloud` | Configured cloud providers, ACP, ADX, web access, and approved MCP servers |
| `local-network` | SQLite and LM Studio on loopback or private IP literals; no public-cloud calls |
| `offline` | SQLite and loopback LM Studio only; no public or LAN calls |

Eva Standalone generates a per-launch bearer token in Electron main and injects
it only for the exact local bridge `/v1/*` origin. All `/v1/*` methods authenticate
before dispatch; `/health` is a redacted unauthenticated readiness probe and
`OPTIONS` is side-effect-free. The token is removed before model/tool subprocesses
start. Manual clients must provide `EVA_BRIDGE_TOKEN`; the only unauthenticated
escape hatch is `EVA_ALLOW_UNAUTHENTICATED_LOOPBACK=1`, which is restricted to a
loopback bind.

ACP permissions are default-deny and ACP terminal execution is not advertised.
Background jobs honor each proposal's `auto_apply` policy; inferred facts,
reflections, and summaries marked for review remain pending until approved.

### Request Flow

**Direct models (OpenAI, Copilot PAT, Gemini):**
1. Renderer submits a closed request object through the preload IPC facade; raw provider transports are blocked at Electron's renderer network layer
2. Electron main validates the fixed provider host/path, method, headers, and request size, then acquires an authenticated process-global bridge lease
3. Main performs direct TLS with redirects/proxy inheritance disabled and streams the response into a bounded buffer while the lease remains active
4. Main releases the lease only after transport settlement; local mode cannot commit while any lease exists
5. Renderer canonicalizes one-turn intents away from inert history text; cleaned text and closed server-attested action receipts are finalized before rendering

If the atomic runtime-state document is malformed, the bridge starts in a
provider-blocked `unknown` repair state. Electron accepts that authenticated,
bound process as UI-ready, but no model runtime starts. An explicit authenticated
Cloud or Local selection rewrites the complete document and exits repair mode.

**ACP models (Copilot CLI):**
1. Browser -> `POST /v1/chat/completions` -> ACP Bridge (HTTP)
2. Bridge -> `session/prompt` -> Copilot CLI (JSON-RPC over NDJSON/stdio)
3. Copilot may request MCP tools; the bridge applies its default-deny permission policy
4. Copilot streams `session/update` notifications with text chunks
5. Bridge accumulates chunks -> returns OpenAI-compatible JSON response

**LM Studio (local):**
1. Browser fetches `/v1/memory/context` + `/v1/data/retrieve` in parallel from bridge
2. Bridge injects memory context from SQLite/Kusto
3. Bridge runs data retrieval through a fixed read-only local MCP profile (see below)
4. Browser submits bounded user/assistant history and immutable artifact identities to authenticated `POST /v1/lmstudio/chat`
5. Bridge validates the local/private LM Studio origin, injects the one system-owned artifact registry, disables inherited proxy/redirect behavior, and sends the provider request
6. Response processed by `Cognition.executeActions()` for any action blocks
7. Rendered via `renderEvaResponse()`

**Eva (AIG) with cognition layer:**
1. Browser calls `Cognition.run()` which drives the draft/review/revise pipeline
2. Each agent call goes to `POST /v1/aig/chat` on the bridge
3. Bridge runs Step 1 (memory), Step 2 (data retrieval), Step 3 (persona), Step 4 (LLM call)
4. LLM call routes to GitHub Models API (PAT), ACP (Copilot CLI), or LM Studio based on model
5. The canonical assistant response and closed action receipts finalize exactly once before rendering; governed background proposals run separately

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
mcp.json                   Optional local MCP presets (gitignored; never auto-loaded)

core/
  style.css                All styling: base theme, settings panel,
                           monitors, chat bubbles, responsive
  themes/
    eva.css                Eva dark theme overrides
    lcars.css              LCARS (Star Trek) theme overrides
  js/
    agent-markers.js       Strict closed-marker parser for nested browser/desktop launch specs
    action-outcomes.js     Exact public eva.action-run/1 proof validator shared by UI consumers
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
                           - Immutable, digest-verified artifact download links
                           - Server-side artifact opening is disabled
                           - AWS Polly TTS (speakText)
                           - Speech recognition, print, clear memory
    gpt-core.js            OpenAI Chat Completions API (trboSend)
                           - Closed preload request to Electron main's leased, bounded TLS broker
                           - Model-specific params (o3-mini reasoning, gpt-5 top_p)
                           - External data augmentation (weather, news, markets, solar)
    gl-google.js           Google Gemini API (geminiSend)
                           - Thinking mode (extracts thoughts vs non-thoughts)
    lm-studio.js           Local LLM via LM Studio (lmsSend)
                           - OpenAI-compatible endpoint on localhost:1234
                           - Parallel memory context + data retrieval from bridge
                           - Action block execution (Cognition.executeActions)
                           - File capability documentation in system prompt
                           - Synchronous exactly-once durable finalization via bridge
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
                             [[EVA_LOOK]]; artifact authority is structured state
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
                           Signal messaging via signal-cli
    telemetry.py           Structured event logging (latency, routing decisions)
    action_runs.py         Typed outcomes, causal proof, launch/gate capabilities, execution leases
    public_egress_proxy.py Per-run public-unicast DNS-pinning HTTP CONNECT proxy
    utils.py               URL validation, LM Studio validation, config persistence
  web_search_mcp.py        MCP server: DuckDuckGo + Google fallback (no API key)
  sqlite_memory.py         SQLite memory backend (SqliteMemory class)
  kusto_mcp.py             MCP server for Azure Data Explorer (10 tools)
  browser_agent.py         Bounded isolated browsing (Playwright + DNS-pinned proxy)
  desktop_agent.py         Launch-only GUI containment (pyautogui screenshot + process proof)
  camera_sense.py          Webcam presence detection (OpenCV face + motion)
  barkTTS_server.py        Suno Bark TTS engine server (GPU)
  eva_seed.kql             Sanitized database seed (public-safe)
  acp_bridge.service       Same-user systemd unit for the canonical ~/.eva installation
  acp_setup.sh             Same-user installer (run without sudo from ~/.eva)
  test_static.py           CI-safe static tests
  test_action_plane.py     No-network action containment, proof, gate, proxy, and UI tests
  test_eva.py              Integration tests (64 checks)
  test_latency.py          Latency benchmarks
  test_skills_e2e.py       Skill import end-to-end tests
  eval/                    Behavioral eval harness

standalone/
  main.js                  Electron shell: port allocation, bridge spawn, health polling
  preload.js               Context bridge (exposes evaStandalone API to renderer)
  launch-capability.js     Native-dialog one-use launch capability issuer
  package.json             Electron + electron-builder config (v5.4.0)
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
| `@github/copilot` | Copilot CLI in cloud mode | `npm install -g @github/copilot`, then `copilot auth login` |
| `azure-identity` | Kusto MCP auth | `pip install azure-identity` |
| `requests` | AIG HTTP calls | `pip install requests` |
| Docker | GitHub MCP server | [docker.com](https://docker.com) |
| Playwright | Browser agent | `pip install playwright && playwright install` |
| pyautogui | Desktop agent | `pip install pyautogui` |
| opencv-python | Camera presence | `pip install opencv-python` |
| signal-cli | Signal messaging | Native binary from [GitHub releases](https://github.com/AsamK/signal-cli/releases), or `install.sh` auto-installs |

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
| `session/request_permission` | Agent -> Client | Request tool execution permission (default-deny) |
| `session/cancel` | Client -> Agent | Cancel ongoing operation |

ACP terminal methods are not advertised and are rejected. Host command execution
will only return after the capability broker can mediate the complete ACP terminal
contract with typed policy and one-use approval receipts.

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
  --mcp-config PATH        Explicit approved-preset MCP config JSON file
```

MCP process startup is fail-closed. Only the exact Azure, GitHub, Kusto,
bundled SQLite, and bundled web-search release shapes are accepted. Unknown,
wrapped, Playwright, pointer/keyboard, or aliased servers are rejected, and a
project `mcp.json` is never auto-discovered.

### HTTP Endpoints

**Core:**

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/chat/completions` | POST | OpenAI-compatible chat (routes to ACP) |
| `/v1/aig/chat` | POST | AIG pipeline: memory + data + persona + LLM |
| `/v1/lmstudio/chat` | POST | Validate private/local LM Studio egress and inject bridge-owned trusted context |
| `/v1/models` | GET | Available models list |
| `/health` | GET | Status, session ID, model, MCP servers |

**Memory:**

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/memory/context` | GET | Build and return memory context for injection |
| `/v1/memory/reflect` | POST | Legacy-named endpoint for synchronous exactly-once turn finalization before render |
| `/v1/memory/backend` | GET/POST | Get or switch memory backend (kusto/sqlite) |
| `/v1/memory/seed` | POST | Seed database tables |

**Data Retrieval:**

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/data/retrieve` | GET | Retrieve live data for any model path |
| `/v1/mode` | GET/POST | Get or switch data retrieval mode (cloud/local) |
| `/v1/provider/admit` | POST | Acquire a process-global direct-cloud-provider lease; denied unless committed cloud mode is ready |
| `/v1/provider/release` | POST | Consume one provider lease after Electron main's transport has settled; local mode cannot commit while any lease exists |

**Skills:**

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/skills` | GET | List all active skills |
| `/v1/skills` | POST | Create a new skill |
| `/v1/skills/evarise` | POST | Normalize raw skill text into Eva schema |
| `/v1/skills/auto-learn` | POST | Default-off legacy draft extraction; never activates behavior |
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
| `/v1/files/generation` | GET | Read the current write-authority epoch required by new artifact writes |
| `/v1/files/write` | POST | Write an immutable artifact plus fsynced identity metadata containing creation epoch, MIME, byte size, and digest |
| `/v1/files/<session-id>/<artifact-id>/<name>?digest=<sha256>&generation=<epoch>` | GET | Stream one immutable artifact only after generation, digest, MIME, and byte-size verification; server-side Open is disabled |
| `/v1/files/purge` | POST | Revoke active/saved artifact registries and delete all artifacts |
| `/v1/files/session/<session-id>/purge` | POST | Delete one session's artifact namespace during checked session deletion |

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
| `/v1/browser/run` | POST | Consume a native one-use capability and start a bounded browser run |
| `/v1/browser/status?run_id=<id>` | GET | Typed lifecycle/outcome status |
| `/v1/browser/screenshot?run_id=<id>` | GET | Latest private screenshot while retained |
| `/v1/browser/confirm` | POST | Resolve one exact approval/input gate |
| `/v1/browser/cancel` | POST | Request run cancellation; reports pending in-flight effects |
| `/v1/desktop/run` | POST | Consume a native one-use capability and start a bounded desktop run |
| `/v1/desktop/status?run_id=<id>` | GET | Typed lifecycle/outcome status |
| `/v1/desktop/screenshot?run_id=<id>` | GET | Latest private screenshot while retained |
| `/v1/desktop/confirm` | POST | Resolve one exact approval/input gate |
| `/v1/desktop/cancel` | POST | Request run cancellation; reports pending in-flight effects |

**Camera:**

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/camera/start` | POST | Start explicit presence mode, or consume an Electron-signed one-use capability for a bound camera question/device |
| `/v1/camera/stop` | POST | Stop webcam |
| `/v1/camera/status` | GET | Presence state (faces, motion) |
| `/v1/camera/frame?capture_id=<id>` | GET | Consume the capture authority and return one fresh JPEG with bridge receipt headers |

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
1. `start()`: Spawn an exact bundled server, strictly validate bounded JSON-RPC `initialize` and `tools/list`, and abort the complete staged set if any server fails
2. `call_tool(name, arguments, timeout)`: Revalidate the fixed read-only tool ID and bounded arguments, then parse an exact bounded content response
3. `stop()`: Terminate process

**Threading:** Background reader threads match bounded JSON-RPC responses by ID. Child stderr content is suppressed; only fixed metadata is logged. Mode/config stage-persist-swap transitions share one lock.

### LocalMCPManager

Manages multiple MCP servers with a unified tool catalog:

```python
class LocalMCPManager:
    servers: dict[name -> MCPServer]
    _tool_map: dict[tool_name -> server_name]  # routes calls
```

Direct local-model execution permits only fixed read-only bundled profiles:
SQLite/Kusto memory reads and fixed-host web search. Generic KQL, Azure/GitHub
MCP tools, writes, callouts, unknown tools, and duplicate tool IDs are denied.
The SQLite process is bound to the operator-configured canonical memory path;
request configuration cannot select another filesystem target.

### Tool-Calling Agent Loop

`local_agent_query()` implements an iterative tool-calling agent using LM Studio:

1. Send the user message plus only the fixed read-only tool schemas to validated LM Studio `/chat/completions`
2. If the model returns a closed, bounded `tool_calls` batch, revalidate each tool ID and argument object before `mcp_manager.call_tool()`
3. Inject tool results back as assistant messages
4. Repeat until model produces a text answer (max 5 iterations, 90s timeout)
5. Return `(data_text, model_used)` matching the ACP retrieval signature

### Web Search MCP Server

`tools/web_search_mcp.py` provides web search without any API key:

| Tool | Args | Description |
|---|---|---|
| `web_search` | query, max_results (8) | DuckDuckGo HTML scraping + Google fallback |
| `web_search_news` | query, max_results (8) | DDG with news-biased queries |
| `web_fetch` | disabled | Arbitrary URL fetching is disabled until it uses brokered DNS-pinned egress |

**Search cascade:** DuckDuckGo HTML -> DuckDuckGo Lite -> Google HTML scraping. User-Agent spoofs Chrome 131.

### Auto-Configuration

When switching to local mode, the bridge:
1. Loads the exact versioned `runtime_state.json` document that atomically binds mode and secret-free MCP selection
2. Rejects malformed/partial state as a whole; legacy split mode/MCP files are not combined
3. Filters the canonical selection to the fixed direct-local read-only process/tool policy
4. Auto-adds `eva-web-search` only when the active egress policy permits its fixed-host search transport
5. Stages the complete local manager transactionally before publishing Local mode

Direct-provider leases intentionally have no bridge-side timeout. Electron main
owns transport settlement and release; if the broker process crashes, restarting
Eva clears the bridge's in-memory leases.

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
- trusted alert Signal delivery (not invokable from model text)
- file-creation ([[EVA_ACTION]] file.download)
- image-search, image-generation
- persistent-memory (table list)
- cron-scheduling, skill-learning

**Skill matching:** When a user message arrives, all active skills are compared by
embedding cosine similarity (threshold 0.30, OpenAI `text-embedding-3-small`).
Up to 2 matching skills have their instructions injected (capped at 1500 chars each).
Falls back to lexical keyword matching if embeddings are unavailable.

### Entity Extraction

Synchronous durable finalization projects explicit facts using strict regex patterns:

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
    +-- Step 5: Exactly-once durable finalization (before render)
      +-- Append immutable user/assistant events and closed action receipts
      +-- Project the conversation and explicit evidence-linked facts atomically
      +-- Commit or roll back the complete turn
    |
    +-- Separate governed background loop
      +-- Build consolidation/reflection proposals from durable evidence
      +-- Require policy or human approval before applying inferred memory
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
server-side and adds persistent intelligence. SQLite is canonical; ADX/Kusto is
an optional consent-aware projection. Each turn is durably finalized exactly
once before rendering, while inferred consolidation remains proposal-based. The
**browser cognitive layer** runs in the page and adds an optional multi-agent
draft/review loop.

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
  effectful: true,
  validate: function (args) {
    // Pure validation/normalization; no I/O or mutation.
    return normalizedArgs;
  },
  run: async function (args) {
    // Execute only after the complete batch passes validation.
    return structuredResult;
  }
});
```

**Built-in capabilities:**

| Capability | Args | Description |
|---|---|---|
| `file.download` | filename, content, mime? | Create downloadable artifact. Genuine PDF for `.pdf` or `application/pdf`. Writes to bridge ARTIFACTS_DIR via `/v1/files/write`. |
| `file.open` | filename | Surface a Download control for an existing registry-authorized artifact. Server-side Open is disabled. |

**Action protocol:**
```
[[EVA_ACTION]]{"id":"file.download","args":{"filename":"report.pdf","content":"..."}}[[/EVA_ACTION]]
```

Action blocks must be standalone and closed. Fenced code is inert. The complete
batch is count-, byte-, shape-, and capability-argument-validated before the
first execution. At most one action/control surface is accepted per turn; malformed,
unclosed, conflicting, or multi-effect batches execute nothing.

**File behavior defaults:**
- Inline answers by default. Eva only creates file artifacts when the user
  explicitly asks for a file format ("create a PDF", "download as markdown").
- "Give me a briefing" = inline text. "Create a PDF report" = file.download.
- Asking to view an already-created file uses file.open to surface its verified Download control (not re-create it).

### Marker Protocol

Eva uses marker blocks for agent capabilities:

| Marker | Purpose | Example |
|---|---|---|
| `[[EVA_BROWSER]]` | Request a bounded Playwright browser run | `[[EVA_BROWSER]]{"goal":"open the result page","start_url":"https://example.com","postcondition":{"type":"browser.url_match","origin":"https://example.com","path":"/done"}}[[/EVA_BROWSER]]` |
| `[[EVA_DESKTOP]]` | Request a bounded desktop run | `[[EVA_DESKTOP]]{"goal":"open GIMP","postcondition":{"type":"desktop.process_spawned","executable":"gimp","state":"started"}}[[/EVA_DESKTOP]]` |
| `[[EVA_LOOK]]` | Propose one webcam frame for an explicit camera request | `[[EVA_LOOK]]{"question":"what am I holding?"}[[/EVA_LOOK]]` |
| `[[EVA_SIGNAL]]` | Inert legacy text; stripped and never dispatched | Not an executable capability |
| Trusted Artifact Registry | Immutable artifact-download authorization | System-owned structured metadata persisted with the session; model/user `[[EVA_FILE]]` text grants no authority |

All control closing markers are mandatory, standalone, top-level, and inert in
code/action regions. Model markers propose intent but grant no authority.
Electron main displays the complete browser, desktop, or camera specification
and mints a one-use, 60-second HMAC capability only after an explicit native
dialog decision. Camera capture additionally requires a fresh bridge-observed
frame sequence and returns a closed receipt. Every browser/desktop effect uses
a distinct one-use approval gate.

## Bounded Action Agents

### Browser Agent (`tools/browser_agent.py`)

Playwright browsing in an isolated persistent profile. Each run is forced
through a loopback DNS-pinning proxy that accepts only public-unicast A/AAAA
answers, rewrites HTTP authority, rejects ambiguous framing, and disables
non-proxied WebRTC UDP. The agent never attaches to an existing user browser.

**Architecture:**
- Director agent (Claude via ACP): text-only, high-level planning
- Executor agent (GPT-4o via OpenAI): vision-based, concrete actions
- Re-consult director every 4 executor steps
- Per-run isolated context with persistent profile at `~/.config/eva-standalone/browser_profile`

**Action types:** click, double_click, click_ref, type_ref, scroll, navigate,
wait, done, ask. Raw keyboard actions are unavailable until the capability
broker can authorize their semantics.

**Safety:** `auto` is rejected. `pause` and `confirm_all` both confirm every
effectful action. A gate binds a frozen action digest and fresh semantic target
fingerprint, expires after 60 seconds, and is consumed once. Cancellation uses
an execution lease: an in-flight effect is recorded before terminalization.

**Outcomes:** lifecycle `status=done` is not success. A model `done` claim is
`indeterminate` unless a user-authorized request postcondition changed from a
fresh tool-observed `not_observed` baseline to an `observed` final state after
at least one approved, ordered effect receipt. Step/runtime exhaustion aborts.
Supported browser checks are exact URL origin/path and bounded element state.

**Artifacts:** Trajectories contain salted hashes and typed results rather than
raw goals, model output, action text, or URL paths. Screenshots and JSONL use
private permissions and are removed after ten minutes; abandoned prior-process
directories are scavenged at startup.

### Desktop Agent (`tools/desktop_agent.py`)

Launch-only desktop containment via a pyautogui screenshot-and-verify loop.

**Architecture:** Same director/executor pattern as browser agent. PyAutoGUI is
used only for private screenshots. GUI launch is limited to root-owned,
non-writable native binaries in a curated allowlist;
model-supplied arguments, pointer actions, keyboard actions, terminal helpers,
and arbitrary window helpers are structurally unavailable until the capability
broker is implemented.

**Verified outcome:** the run must have no prior spawn receipt; an approved
launch records the exact canonical binary and PID; success requires that same
live `/proc/<pid>/exe` to be observed after the effect. This is explicitly a
run-scoped spawn attestation, not a claim that no matching process existed
elsewhere on the system.

### Camera Presence (`tools/camera_sense.py`)

Local webcam face and motion detection.

**Architecture:** Subprocess worker (avoids V4L2 GIL wedge). State exposed via JSON file (`~/.config/eva-standalone/camera/state.json`).

**Detection:** OpenCV Haar cascade for faces, frame-difference for motion. Hysteresis: 2 frames to detect presence, 8 to lose it.

**Privacy:** Camera is off by default. Presence mode is an explicit UI setting.
Each model-proposed one-shot look must match an explicit user camera request,
pass strict top-level parsing, receive a native Electron decision, and consume a
question/device-bound HMAC capability. The bridge releases only one frame newer
than its captured baseline and returns a closed `eva.camera-capture/1` receipt.

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

### Governed Learning

The Phase 3 learning pipeline is default-off, local, shadow-only, and cannot
activate behavior. It records verified outcomes, proposes restricted immutable
candidates, and evaluates them deterministically. The older provider-backed
`/v1/skills/auto-learn` draft path is separately default-off behind
`EVA_LEGACY_SKILL_AUTO_LEARN`; drafts require explicit review and grant no
activation authority.

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

**Proposal governance:** Every result is recorded in `BackgroundProposals`. Jobs
marked `auto_apply: false` remain `pending` for review in Settings > Background.
Narrow deterministic maintenance outputs explicitly marked `auto_apply: true`
may be applied immediately and are recorded as reviewed by `auto`.

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

### Signal Alert Delivery

Eva can deliver configured trusted alerts through Signal using signal-cli
(native binary, no Java).

**Setup:**
1. Install signal-cli (v0.14.5+ native binary at `~/.local/bin/signal-cli`, or let `install.sh` handle it)
2. Link to your Signal account: `signal-cli link -n "Eva"` and scan the QR code from Signal mobile
3. Enter sender and recipient numbers in Settings > Auth

**How it works:** Trusted alert records pass the configured quiet-hour,
salience, cooldown, and rate-limit policy before the alert subsystem invokes
signal-cli. `[[EVA_SIGNAL]]` text is always inert and stripped by the canonical
renderer. Local/provider model output, keywords, prompts, and response parsing
cannot invoke signal-cli. General on-demand model-authored Signal messages are
not part of this containment release.

**Configuration persisted to:** `~/.config/eva-standalone/alerts.json` (signal_sender, signal_recipient fields)

## Telemetry

Structured, privacy-safe event logging. Records numeric counts/durations and a
closed set of bounded enum labels only. Never records message content, queries,
tool arguments, child stderr, tokens, keys, or MCP environment values.

**Events:** `acp_pool` (hit/warm/evict/miss), `acp_prompt` (model, ms, chars), `cognition_turn` (draft/review/revise timing)

**Storage:** JSONL file at `~/.config/eva-standalone/telemetry.jsonl` (rotates at 5 MB). In-memory ring buffer (300 events) for `/v1/telemetry`.

**Debug log:** Free-form stdout/stderr persistence and the legacy `/v1/logs`
ring are disabled. The retired `bridge_debug.log` is truncated to an empty file;
Electron does not mirror bridge stream content.

## Settings Panel

Eight tabs in a modal overlay:

| Tab | Contents |
|---|---|
| **General** | Theme, TTS engine/voice, auto-speak, camera presence, vision provider, data retrieval mode (cloud/local) with status |
| **Models** | Model selector (grouped by provider), temperature, max tokens, reasoning effort, AIG backend selector, ACP model selector, cognitive layer controls (toggle, per-agent model selectors, max cycles, editable prompts, debug trace) |
| **Auth** | API key inputs with show/hide toggles, ACP bridge URL, Signal sender/recipient numbers |
| **Prompts** | Personality presets (Default/Concise/Advanced/Terminal/Custom), editable system prompt textarea |
| **Goals** | Goals list with create/edit/delete. Skills list with import (paste/URL/GitHub/file), evarise preview, enable/disable |
| **Background** | Background loop status, enable/interval controls, run-once, proposal approval/rejection, recent activity |
| **Cron** | Cron task list with create/edit/delete, schedule expression, prompt, last/next run timestamps |
| **MCP** | Azure MCP, GitHub MCP, Kusto MCP toggles with config fields. Apply/refresh buttons |

## Deployment

### Browser-only deployment

Standalone browser/file mode is not supported by this containment release.
Direct provider requests require authenticated process-global leases issued by
the local bridge, and the bearer remains outside renderer memory. Use Eva
Standalone. A future hosted deployment must provide the equivalent trusted
network broker and authenticated bridge identity; renderer-stored bearer tokens
and unbrokered direct-provider transport are intentionally unsupported.

### Manual ACP bridge

```bash
python3 tools/acp_bridge.py --port 8888 \
  --enable-kusto-mcp \
  --kusto-cluster "https://<your-cluster>.region.kusto.windows.net" \
  --kusto-database Eva
```

Manual API clients must set `EVA_BRIDGE_TOKEN` and include the matching bearer
header on every `/v1/*` request. The unauthenticated loopback escape hatch is for
isolated development only and is not a browser authentication mechanism.

### Standalone (Electron AppImage)

A bundled desktop build that ships the web UI and ACP bridge together. The
Electron shell allocates a free localhost port, starts the bridge, and injects
the URL into the renderer via `window.evaStandalone`.

```bash
cd standalone
npm install
npm run dist
./dist/'Eva Standalone-5.4.0.AppImage'
```

**Electron lifecycle:**
1. `getFreeLocalPort()`: OS-allocated free port
2. `startBridge(port)`: generate a one-time readiness nonce and spawn
  `python3 tools/acp_bridge.py --bind 127.0.0.1 --port <port>` with the nonce
  and per-launch bearer available only to that child.
3. After `ThreadingHTTPServer` successfully binds, the child emits one stdout
  proof: an HMAC over the nonce, exact child PID, loopback host, and selected
  port. The proof contains neither the nonce nor bearer.
4. `waitForBridge(url, process, timeout)`: accept only the exact spawned child's
  buffered, authenticated bind proof; make no `/health` request before it.
5. After proof, poll `/health` every 500ms. Only proof plus health may create the
  renderer window and install main-process `/v1/*` bearer injection.
6. On `EADDRINUSE`: retry with a new nonce and port (max 3 attempts).
7. On malformed proof, child exit, timeout, or post-ready bridge crash: show an
  error dialog and quit without creating a trusted renderer.

Host prerequisites: Node.js 24+, Python 3.12+, Copilot CLI authenticated (for cloud mode). LM Studio for local-only mode.

### Phase 2 Memory, Runtime Recall, and Proposal Consolidation (tracking)

Phase 2 introduces a sidecar schema for semantic claims, retrieval scoring, and embedding cache alongside the existing Phase 1 event kernel. All Phase 2 runtime behavior is **default-off** via startup-immutable environment flags. The additive sidecar migration still runs at ordinary SQLite startup while runtime behavior is off; it uses independent metadata and does not alter Phase 1 kernel tables or the legacy recall path.

**Feature flags** (set before bridge startup, frozen at import):
| Variable | Values | Default | Purpose |
|---|---|---|---|
| `EVA_PHASE2_MEMORY` | `1`/`true`/`yes`/`0`/`false`/`no` | `0` (off) | Master kill switch |
| `EVA_MEMORY_RECALL_MODE` | `legacy`/`shadow`/`hybrid` | `legacy` | Recall pipeline mode |
| `EVA_MEMORY_SEMANTIC_MODE` | `off`/`cache`/`openai` | `off` | Semantic scoring source |
| `EVA_MEMORY_SEMANTIC_QUERY_CONSENT` | `1`/`true`/`yes`/`0`/`false`/`no` | `0` (no) | Explicit cloud egress consent |
| `EVA_MEMORY_CONSOLIDATION` | `off`/`proposals` | `off` | Local proposal-only journal consolidation |
| `EVA_MEMORY_ANALYTICS` | `off`/`local` | `off` | Local low-cardinality retrieval metrics |

**Invalid flag handling:** Boolean flags use a strict parser that accepts only `1`/`true`/`yes` and `0`/`false`/`no`. Unrecognized values (e.g. `on`, `maybe`, `2`) record the flag name (not value) in `PHASE2_INVALID_FLAGS`. Enum flags produce an `"INVALID"` sentinel on unrecognized values. At startup, an invalid master value or an enabled master with any invalid dependent flag prints a redacted error and exits with code 2. If the master is explicitly off, invalid dependent flags produce a warning and Phase 2 remains disabled. `phase2_effective_modes()` returns all-legacy/off defaults when the master is off or configuration is invalid.

**Scoring formula:** Composite weighted score = `(0.35*lexical + 0.30*semantic + 0.15*temporal + 0.15*confidence + 0.05*provenance) / sum(available weights)`. When a component is unavailable (`None`), its weight is omitted. Explicit measured `0.0` remains available and is not skipped. Missing, non-finite, or out-of-range per-candidate semantic values are unavailable rather than silently clamped.

**Timestamp and ordering contract:** Input must be an ISO 8601 string. Naive timestamps are treated as UTC; aware timestamps are normalized to UTC; numeric epochs are rejected. A malformed candidate observation rejects that candidate, and malformed `now_iso` fails the ranking operation with `ValueError`. Future timestamps clamp age to zero. Ranking is deterministic by score descending, effective confidence descending, exact integer-microsecond UTC observation instant descending, then normalized `ClaimId` ascending; adjacent instants remain ordered across the full accepted Python datetime range.

**Candidate cap:** Input exceeding 200 candidates raises ValueError (fail closed, not silently truncated). Results capped at 6.

**Runtime recall modes:** Runtime integration remains behind `EVA_PHASE2_MEMORY` and preserves the exact Phase 1 context path when disabled or set to `legacy`. `shadow` reads and ranks eligible local sidecar claims but returns the legacy context byte-for-byte. `hybrid` appends at most six ranked claims as bounded untrusted JSON lines; claim IDs are never rendered and claims are never copied into legacy `Knowledge`. The same local sidecar augments either the SQLite or ADX legacy read path. Direct ACP prompt composition inserts an explicit blank-line boundary so the untrusted-data footer remains a standalone line.

**Claim eligibility:** Terminal `deny`, `supersede`, `retract`, or `merge` resolutions exclude a claim; `confirm` retains it. `deleted` claims are excluded. `session` claims remain excluded until claims carry a session identity that can be matched safely. Offline/local-network hybrid recall may use local and cloud-allowed claims, including locally held secret claims. Cloud-mode prompt augmentation requires explicit query consent, `cloud_allowed` item consent, and non-secret sensitivity. Shadow evaluation remains local and never injects its result.

**No-network semantics:** `off` uses lexical/temporal/confidence/provenance scoring. `cache` may add semantic scores only when a fully matching query and claim vector already exists in `MemoryEmbeddingCache`; misses fall back to lexical scoring. Shadow always uses a `local_only` query-cache consent namespace, even when the bridge process permits cloud egress. Expired or corrupt query rows do not consume the cache work cap; at most the first eight fully valid namespaces in canonical provider/model/version/dimension/encoding order are considered. Iteration 2 performs no embedding writes or provider calls. The reserved `openai` mode is therefore cache-only in this iteration and records zero semantic egress.

**Embedding cache identity:** Cache keys hash sorted canonical JSON containing `ObjectType`, `ObjectId`, `Provider`, `Model`, `ModelVersion`, `Dimensions`, `Encoding`, `ContentHash`, and `ConsentFingerprint`; delimiter-bearing fields cannot collide. Encoding is fixed to `f32le`. Blob length must equal `dimensions * 4`, and every unpacked float must be finite. `lookup_embedding_cache()` requires and verifies the full identity rather than accepting a cache key alone. Invalidation by object or exact SHA-256 consent fingerprint is supported.

**Cache expiry:** Non-null `ExpiresAt` values use canonical UTC `YYYY-MM-DDTHH:MM:SS[.ffffff]Z`. Write and lookup boundaries reject rather than normalize offsets, alternate separators, date-only values, whitespace, and non-six-digit fractions. The schema also rejects invalid calendar dates, embedded NULs, empty values, and out-of-range time fields. Invalid or expired values return no cache hit. Clock injection supports deterministic boundary testing.

**Rollback safety:** Phase 2 tables use a separate `_phase2_schema_migrations` metadata table. Old binaries that only know Phase 1 see no drift. Rolling back to a pre-Phase2 binary leaves the sidecar tables dormant but does not break Phase 1 `verify_schema()`.

**Proposal-only consolidation:** With both the Phase 2 master and `EVA_MEMORY_CONSOLIDATION=proposals` enabled, an authenticated loopback request may scan a bounded journal batch. Scanning uses monotonic `MemoryEvents.JournalSequence`, never timestamps. Every visited event receives an immutable receipt (`ignored`, `invalid`, or `proposed`) in the same transaction as proposal/conflict inserts and checkpoint advancement, so crashes cannot skip an unreceipted event. Proposed receipts have a composite foreign key to the exact proposal source event, extractor, sequence, and payload hash; replay also recomputes normalized claim/proposal identity and digest before trusting the receipt. Source claim fields must be valid UTF-8; escaped lone-surrogate payloads receive deterministic `invalid_payload` receipts rather than stalling the cursor. Iteration 3 deterministically proposes only existing `memory.fact_candidate_extracted` events; it invokes no model and stores no raw model output.

**Contradiction classification:** A proposal is classified as `new`, `confirmation`, `contradiction`, or allowlisted `temporal_change`. Exact source event ID, journal sequence, source payload hash, extractor version, normalized claim, and the deterministic active-conflict set are covered by the proposal digest. Terminally denied/superseded/retracted/merged or deleted-scope claims are not conflicts. Creating the proposed receipt seals conflict membership at the schema boundary. Before any canonical mutation, approval reloads the source event and complete conflict set, recomputes proposal ID/digest, and then rechecks current active conflicts; stale or integrity-mismatched proposals cannot mutate canonical claims but may be explicitly rejected.

**Explicit decision boundary:** Proposal state is derived from immutable `MemoryClaimProposalDecisions`; proposal rows are never updated. Decisions require the exact proposal digest and a UUID request/operation identity. Replaying the same operation and command returns the same result; reusing it with different content or deciding an already-terminal proposal is rejected. Claim targets are binary identities and are validated without Unicode or whitespace normalization. Scoped proposal-conflict and decision-command hashing encodes identity UTF-8 bytes as hexadecimal before Phase 1 canonical JSON is applied, so canonically equivalent but binary-distinct IDs remain distinct. Decision journal receipts persist the same reversible encoding, and evidence/resolution IDs use it too. Before any claim/evidence/resolution write, the complete redacted audit payload is canonicalized and checked against the immutable journal byte limit; oversized multibyte target sets fail with a validation response and zero writes. Supported decisions are `reject`, `approve_new`, `confirm_existing`, `keep_both`, and `supersede_existing`; the last requires the exact sorted conflict set. Before approval, the source must still be an eligible `memory.fact_candidate_extracted` event. Only a successful approval transaction may append `MemorySemanticClaims`, `MemoryClaimEvidence`, and applicable confirm/supersede resolutions. It never writes legacy `Knowledge`.

**Transaction lock order:** Memory-backed repository mutations (event append, outbox ensure/claim/complete/fail, and legacy receipt) acquire the caller/SQLite memory transaction before repository serialization. Caller-owned decision transactions, ordinary appends, and projection workers therefore share one memory-to-repository order. Bare connection-factory repositories use a stable lock per exact connection object: one shared `check_same_thread=False` connection is fully serialized, while distinct per-thread connections never hold a global repository lock while waiting for SQLite write ownership. An implicit factory operation rejects an already-active transaction rather than joining unknown caller-owned state; callers that intentionally need transaction-local participation must pass `connection=`. Implicit factory reads serialize on their connection and reject unknown active transactions, while explicit-connection reads remain transaction-local. Idempotency fallback uses the same ownership protocol and cannot accept an uncommitted row that later rolls back.

**Proposal API:** All routes require a configured bridge bearer even when the unauthenticated loopback development escape is enabled, plus loopback bind and proposal mode. `POST /v1/memory/claim-proposals/scan` advances one bounded batch; scan request IDs are correlation IDs, not replay receipts, so a later retry may process the next batch. Durable proposal or receipt primary/alternate identity conflicts return HTTP 409. `GET /v1/memory/claim-proposals` lists derived status; `GET /v1/memory/claim-proposals/<id>` returns evidence/conflict metadata; and `POST /v1/memory/claim-proposals/<id>/decide` performs one operation-identified terminal decision. No automatic/background decision path or review UI exists in this iteration.

**Schema design:** Claims are pure immutable records (no Active/SupersededBy lifecycle fields). Status derives from `MemoryClaimResolutions`: a claim without a retract/supersede resolution is active. Claims, evidence, resolutions, proposals, conflicts, scan receipts, and decisions reject updates, deletes, replacement, and UPSERT mutation. Proposal-side `BEFORE INSERT` guards cover every primary and alternate unique key (source/extractor, journal sequence, proposal, operation, and decision event), preventing `INSERT OR REPLACE` bypass even when SQLite recursive triggers are disabled. Runtime schema probes use a unique verifier-only extractor identity and cannot collide with legitimate extractor data. Every sidecar and migration-metadata text field requires SQLite `typeof(...)='text'` and rejects embedded NUL, preventing affinity/BLOB and NUL-truncated length bypasses. Text identities are explicitly `NOT NULL`, nonempty, and bounded; SHA-256 identities require exactly 64 lowercase hexadecimal text characters. Metrics use SQLite integer-type and cross-field constraints.

**Untrusted recall rendering:** Recalled fields are emitted as bounded canonical JSON lines between explicit untrusted-data markers. IDs are omitted; quotes and controls cannot escape JSON strings; role headers and every case variant of `[[EVA_` are neutralized; source bidi/invisible formatting controls (including deprecated controls), line separators, and paragraph separators are removed before intentional marker neutralizers are inserted. Carriage returns are canonicalized before structural scans, and marker/role scans run again after line flattening so character removal cannot reconstruct control syntax. Recalled text alone never authorizes an action.

**Metrics contract:** Foundation metrics contain only fixed recall/semantic/fallback categories, bounded integer counts, binary `0`/`1` egress/cache flags, and bounded integer latency. They never contain query text, facts, entity values, event/message IDs, or URLs. Metric records are immutable and all slots are revalidated at the write boundary. Application validation and SQLite constraints both enforce result/candidate and semantic-mode cross-field rules. Aggregation skips catalog-corrupted categories/types and parses UTC instants rather than lexically comparing equivalent ISO spellings, including exact fractional boundaries and offsets.

**Sidecar tables:** `MemorySemanticClaims`, `MemoryClaimEvidence`, `MemoryClaimResolutions`, `MemoryEmbeddingCache`, `MemoryRetrievalMetrics`, `MemoryConsolidationCheckpoints`, `MemoryClaimProposals`, `MemoryClaimProposalConflicts`, `MemoryConsolidationReceipts`, `MemoryClaimProposalDecisions`.

**Runtime metrics:** When `EVA_MEMORY_ANALYTICS=local`, shadow/hybrid attempts append only mode, bounded counts, binary cache/egress flags, fallback category, and bounded latency. Query text, claim IDs, entities, values, URLs, and provider payloads are never stored. Metric failure cannot alter recall output. Timer failure returns the exact legacy context; when analytics is off, the runtime does not access a timer. The default remains `off`.

**Iteration scope:** Iterations 1–3 establish and attest the sidecar, pure retrieval/cache/rendering helpers, local shadow/hybrid reads, cache-only semantic scoring, privacy-safe metrics, journal-sequence claim proposals, contradiction metadata, and explicit terminal decisions. Autonomous approval, background consolidation, review UI, provider/model extraction, semantic egress, and procedural self-improvement remain disabled until later independently approved iterations.

**Runtime prerequisite:** Phase 2 exact column attestation uses `PRAGMA table_xinfo` and therefore requires SQLite 3.26 or newer (in addition to the project Python 3.12+ baseline). Startup checks this version before creating Phase 2 migration metadata and fails with a clear prerequisite error on older runtimes.

**Tests:** `python3 tools/test_phase2.py` (deterministic, no external network, temporary SQLite, 566 assertions).

**Runtime tests:** `python3 tools/test_phase2_runtime.py` (33 deterministic tests, temporary SQLite, no providers or external network).

**Consolidation tests:** `python3 tools/test_phase2_consolidation.py` (61 deterministic tests, temporary SQLite, no models/providers/external network).

### Phase 3 Safe Continual Learning (iteration 1)

Phase 3 adds a dormant, local shadow-learning pipeline behind startup-frozen
`EVA_PHASE3_LEARNING=shadow`; the default is `off`, and any other non-empty
value fails startup. The additive SQLite migration runs while behavior is off
and uses independent `_phase3_schema_migrations` metadata. Schema v2 upgrades
the brief rowid-backed v1 layout atomically to `WITHOUT ROWID`; concurrent
first-start migrations serialize, preserve rows, and converge idempotently.

Authenticated loopback clients with SQLite selected as the active memory
authority may record explicitly confirmed (`user_confirmed: true`),
user-attested execution
outcomes, propose one of three restricted candidate kinds
(`skill_instructions`, `skill_prompt_template`, or `skill_routing_rule`), and
run a frozen deterministic evaluator. Outcomes, candidates, evidence links,
evaluation plans, and results are immutable and event-linked. Every mutation
requires a UUID operation identity with collision-safe replay semantics.
Support evidence must attest the exact local skill version and a succeeded,
observed postcondition; failed or unobserved outcomes can be linked only as
failure evidence. Missing, disabled, stale, cross-skill, or cross-version
targets fail closed.

This iteration deliberately has no candidate execution, activation, promotion,
canary, rollback, background worker, provider/model call, or legacy `Skills`
write. Evaluation reads the exact current baseline hash, rejects stale
candidates, detects a versioned fixed policy set and baseline regressions, and
records detailed fixture results plus a hash of every data-driven evaluator
rule. `Passed` means only that this frozen local policy found no listed issue;
it is not a general safety proof. Passing evaluation grants no capability and
never changes runtime skill selection or prompt context.

The pre-existing browser-triggered skill-draft suggestion flow is legacy and
separate: it can call the configured agent/model after a successful task. It is
now blocked before request-body processing or provider dispatch unless the
startup-frozen strict boolean `EVA_LEGACY_SKILL_AUTO_LEARN` is explicitly set
to `1`, `true`, or `yes`; the default is off, `0`/`false`/`no` are explicit off
values, and every other non-empty value fails startup. Its output is not Phase
3 evidence, evaluation, approval, or activation.

Routes (all require configured bearer auth, loopback bind, SQLite memory
authority, and shadow mode):
- `POST /v1/learning/executions/report`
- `POST /v1/learning/candidates`
- `GET /v1/learning/candidates`
- `GET /v1/learning/candidates/<candidate-id>`
- `POST /v1/learning/candidates/<candidate-id>/evaluate`

**Tests:** `python3 tools/test_phase3.py` (deterministic, temporary SQLite, no
models/providers/external network, and explicit non-activation checks).

### ACP Infrastructure Roadmap (tracking)

Current state (2026-06-15):
- A legacy static web tier may remain deployed on 32-bit hosts, but it cannot use
  this release's direct providers without an equivalent trusted network broker.
- ACP Bridge currently runs on a separate compatible machine.
- Single-host deployment is blocked until new hardware is available.
- Local mode (LM Studio + direct MCP) works on supported `x86_64` or `arm64`/`aarch64` machines without Copilot CLI.
- Signal messaging available via signal-cli (native binary, linked account required).

| Milestone | Status | Notes |
|---|---|---|
| Provision bridge-capable server | planned | 2+ vCPU, 4+ GB RAM |
| Install runtime baseline | planned | Node.js 24+, Python 3.12+ |
| Authenticate Copilot CLI | planned | `copilot auth login` on target |
| Deploy bridge as systemd service | planned | Manual authenticated API infrastructure only. Install Eva at `~/.eva`, authenticate Copilot as that user, then run `tools/acp_setup.sh` without sudo; the user unit intentionally disables journal output. Eva Standalone uses its own per-launch bridge instead. |
| Single-host ACP deployment | planned | Keep localhost fallback until complete |
| Post-migration validation | planned | `/health` ok + AIG smoke + `test_eva.py` |
| macOS standalone build | planned | Needs Apple Developer ID + notarization |
| Windows standalone build | planned | Add `win` target to electron-builder |

## Security

- Bridge binds to `127.0.0.1` by default (localhost only)
- ACP permissions are default-deny; `--allow-all-tools` is not used
- ACP terminal capability is disabled and terminal methods are rejected
- MCP env vars (tokens) are redacted from `/v1/mcp` responses and persisted configs
- URL fetching uses SSRF protection: DNS resolution validated, all IPs must be public, IP pinning prevents DNS rebinding, redirect hops re-validated
- Skill import treats source text as untrusted data (explicit anti-injection prompt)
- LM Studio base URL is restricted to loopback in `offline`, and loopback/private IP literals in `local-network`, on whitelisted ports (1234, 8000, 8080, 11434); redirects are rejected
- Camera off by default, subprocess-isolated, state read-only from bridge
- Browser/desktop launches require native Electron authorization; every effectful action then requires an exact, expiring, one-use approval
- Browser egress uses a per-run public-unicast DNS-pinning proxy; local/private/multicast targets, URL credentials, ambiguous HTTP framing, and non-proxied WebRTC UDP are blocked
- Model completion and step exhaustion never imply success; only a causal baseline → approved effect receipt → fresh tool observation can produce `succeeded`
- Desktop keyboard, shell, arbitrary arguments, and window-helper paths remain unavailable until the capability broker is implemented
- `pyautogui.FAILSAFE = True` (mouse to corner = emergency stop)
- Background proposals honor per-job governance: review-required outputs remain pending; explicit auto-apply maintenance outputs are audited

## CI / Testing

### GitHub Actions (`eva-ci.yml`)

Runs on every PR to `main`:

| Job | Checks |
|---|---|
| **static-checks** | Secret scanning, HTML structure, JS syntax, Python syntax, model routing, config templates, .gitignore |
| **python-tests** | `tools/test_static.py`: file integrity, config safety, CSV logic, model selector, seed validation |

The Python 3.12 job also runs Phase 0–3 plus
`tools/test_action_plane.py` (deterministic, no providers/GUI/network), covering
launch capabilities, one-use gates, causal proofs, cancellation leases,
public-egress pinning, strict model actions, frontend outcomes, and privacy.

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
