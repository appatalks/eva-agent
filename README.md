# Eva AI Assistant

![screenshot](core/img/Eva-splash.png)

[Website](https://appatalks.github.io/eva-agent/) | [Documentation](README-2.md) | [Issues](https://github.com/appatalks/eva-agent/issues) | License: MIT

A voice-first AI assistant that sees through your camera, controls your browser and desktop, remembers everything, learns from experience, and runs tasks on a schedule. No build step. No framework. Open source.

## Quick install

```bash
curl -fsSL https://appatalks.github.io/eva-agent/get-eva.sh | bash
```

Then launch:

```bash
eva
```

Eva is also added to your system application menu (GNOME, KDE, etc.), so you can search for "Eva" in your app launcher.

Or clone and run manually:

```bash
git clone https://github.com/appatalks/eva-agent.git
cd eva-agent
./install.sh            # install dependencies
cd standalone && npm install && npm run dist
./dist/'Eva Standalone-5.3.0.AppImage'
```

Prereqs: Node.js 24+ and Python 3.12+. GitHub Copilot CLI plus
`copilot auth login` is required only for `cloud` egress mode; LM Studio local
operation does not require Copilot CLI.

## Features

| | |
|---|---|
| **Camera vision** | Webcam presence sensing, face-detection auto-wake, on-demand "look" with gpt-4o |
| **Browser agent** | Isolated Playwright DOM/vision loop with DNS-pinned public egress, native launch authorization, per-action approval, and verified outcomes |
| **Desktop agent** | Launch-only containment for curated root-owned GUI binaries; pointer, keyboard, shell, arguments, and window helpers remain broker-disabled |
| **Voice interface** | Full-screen voice orb, wake/barge-in, TTS (OpenAI, Polly, Bark, browser) |
| **Signal messaging** | Send-only text notifications via signal-cli, keyword-triggered or on-demand |
| **Persistent memory** | Kusto/ADX or local SQLite, default-off shadow/hybrid claim recall, and human-approved evidence-linked consolidation proposals |
| **Safe skill learning** | Default-off local shadow pipeline records verified outcomes, proposes restricted immutable candidates, and evaluates them deterministically without activation |
| **Legacy skill drafts** | Existing provider-backed draft suggestion is separately gated by strict default-off `EVA_LEGACY_SKILL_AUTO_LEARN`; it grants no activation authority |
| **Cron scheduler** | Standard cron expressions, recurring prompts, morning briefings, alerts |
| **Subagent parallelism** | Spawn up to 4 concurrent ACP tasks, results via notifications |
| **Multi-provider** | OpenAI, Google Gemini, GitHub Copilot, lm-studio (local) |
| **Doctor diagnostics** | Structured readiness probe for every subsystem with actionable fixes |
| **MCP ecosystem** | Azure, GitHub, and Kusto integrations; desktop-control MCP servers are release-disabled |
| **Cognitive layer** | Eva + Reviewer dual-agent pipeline with configurable models |
| **Explicit egress policy** | Cloud, local-network, or air-gapped offline operation with fail-closed routing |

## Get started

Select **Eva (AIG)** in the model dropdown for the full experience.

For persistent memory, point Settings > MCP at an Azure Data Explorer cluster, or use the default local SQLite backend (zero setup). For semantic recall, add an OpenAI key in Settings > Auth (falls back to keyword matching without one).

For Signal notifications, install [signal-cli](https://github.com/AsamK/signal-cli) and link it to your Signal account (`signal-cli link -n "Eva"`). Enter sender and recipient numbers in Settings > Auth.

Import skills from text, URLs, GitHub repos, or files in Settings. Eva normalizes them into her format, stores in ADX, and applies matching skills automatically.

## Security and egress modes

Eva Standalone creates a random bridge bearer token for every launch. Electron's
main process injects it only into requests sent from Eva's trusted window to the
exact local `/v1/*` bridge origin. The token is not exposed to renderer scripts,
preload APIs, model prompts, ACP, MCP children, or process arguments.

Set `EVA_EGRESS_MODE` before launch:

- `cloud` (default): configured providers, ACP, and approved MCP servers are available.
- `local-network`: SQLite plus LM Studio on a validated loopback/private address; public providers, ACP, ADX, web imports, and cloud vision are blocked.
- `offline`: SQLite plus loopback LM Studio only; public and LAN network requests are blocked.

Invalid non-empty values fail startup. In restricted modes, select **Eva (AIG)**
or **LM Studio**; AIG is forced through the configured local model. ACP terminal
execution is disabled until it is mediated by the capability broker.

Manual bridge clients must supply `EVA_BRIDGE_TOKEN` and send it as a bearer
token. `EVA_ALLOW_UNAUTHENTICATED_LOOPBACK=1` is a development-only escape hatch
and is refused on non-loopback binds. Never expose an unauthenticated bridge to a
LAN or the internet.

## Documentation

- [README-2.md](README-2.md): architecture, MCP, ACP, browser-only setup, roadmap
- [standalone/README.md](standalone/README.md): AppImage build and runtime
- [Website](https://appatalks.github.io/eva-agent/): features, comparison, install guide

