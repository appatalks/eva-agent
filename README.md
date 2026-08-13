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
./dist/'Eva Standalone-5.5.11.AppImage' --eva-workspace-terminal-v1
```

Prereqs: Node.js 24+, Python 3.12+, GitHub Copilot CLI (`copilot auth login`).

### Windows (experimental)

The standalone launcher can be packaged as a Windows x64 installer from a
Windows checkout:

```powershell
cd standalone
npm install
npm run dist:win
```

The installer is written to `standalone/dist/`. It provisions Python 3.12,
Node.js 24+, and a private GitHub Copilot CLI runtime through Windows Package
Manager, then opens a terminal for the account owner to complete the
interactive GitHub sign-in. Linux-specific desktop automation and camera discovery are not yet
supported on Windows.

## Features

| | |
|---|---|
| **Camera vision** | Webcam presence sensing, face-detection auto-wake, on-demand "look" with gpt-4o |
| **Browser agent** | Playwright-based DOM control, persistent Chrome login, hybrid vision fallback |
| **Desktop agent** | PyAutoGUI mouse/keyboard control, optional AT-SPI via computer-use-linux MCP |
| **Voice interface** | Full-screen voice orb, wake/barge-in, profile TTS for normal replies, fast native-speech Live Translation, persisted microphone/speaker selection, cached Local Voices acknowledgement clips, and durable user/Eva turn records in Sessions |
| **Native harness control** | Allowlisted in-app API for direct navigation, voice controls, owned GitHub repository selection, and terminal tasks; direct questions and requests are considered by a tool-free CLI planner, non-CLI questions fall back to chat, safe inspection plans auto-run, and other commands are typed for review |
| **Signal messaging** | Send-only text notifications via signal-cli, keyword-triggered or on-demand |
| **Persistent memory** | Kusto/ADX or local SQLite: conversations, emotion tracking, semantic recall |
| **User profiles** | Switch local profiles with separate sessions, prompts, model choices, and UI preferences |
| **Settings workspace** | Full-view grouped navigation for models, accounts, goals, background jobs, schedules, tools, memory, and learning controls |
| **Self-improving skills** | Full searchable skills library and editor with source/status filters, tags, tools, imports, and reusable capability drafts |
| **Cron scheduler** | Standard cron expressions, recurring prompts, startup-prepared morning briefings with explicit incomplete-source status, alerts |
| **Subagent parallelism** | Spawn up to 4 concurrent ACP tasks, results via notifications |
| **Agent operations** | Eva has a permanent, navigable primary-agent entry alongside live subagent output and sanitized activity updates, browser/desktop runs, steering, and animated memory topology |
| **Coding workspaces (experimental)** | Eva Standalone imports local Git projects or authenticated owned GitHub repositories into protected project boundaries, discovers project MCP modules from multiple `mcp.json` and `.mcp.json` locations, applies enabled modules only to that project’s coding agents, opens real PTY terminals, and monitors coding runs; ordinary requests to run smoke tests, tests, builds, checks, or diagnostics use an isolated agent run for the selected workspace with chat and Workspaces progress; private GitHub imports require repository Contents: Read access and can use native GitHub CLI device-code authorization; workspace removal cleans Eva-managed run worktrees and records while preserving the source repository |
| **Multi-provider** | Eva AIG via direct OpenAI API, GitHub Copilot ACP, or LM Studio; direct Gemini remains available only as a deprecated browser compatibility route |
| **Prompt budgets** | Bounded provider payloads with pinned instructions, recent turns, rolling summaries, and privacy-safe estimates |
| **Reasoning controls** | Model-specific effort levels for OpenAI, GitHub Models, and Copilot CLI ACP |
| **Streaming responses** | AIG and direct Copilot ACP show safe incremental text with TTFT telemetry; final actions run once |
| **Structured learning controls** | Explicit response feedback, routine action outcomes, and optional voice diagnostics use consent-gated, expiring metadata only; prompts, transcripts, audio, credentials, and private content are excluded |
| **Application audit log** | Owner-only rotating JSONL records sanitized routing, provider, native-action, terminal-planning, cancellation, and latency outcomes without prompts, responses, commands, credentials, or tokens |
| **Doctor diagnostics** | Structured readiness probe for every subsystem with actionable fixes |
| **MCP ecosystem** | Azure, GitHub, Kusto, computer-use-linux desktop control |
| **Adaptive review** | Fast direct Eva responses with GPT-5.6 Terra review for consequential turns |
| **Dual data mode** | Cloud (Copilot CLI + MCP) or Local (LM Studio + direct MCP, fully offline) |

## Get started

Select **Eva (AIG)** in the model dropdown for the full experience.

In Settings > Models, **Eva Backend Model** includes both Copilot ACP models and
**OpenAI API (direct)** models. Direct OpenAI keeps Eva's memory, persona,
adaptive review, response rendering, and browser/desktop/camera action markers;
it requires an OpenAI key but not a Copilot license. ACP-specific MCP retrieval
and ACP subagents still require Copilot, while LM Studio local mode provides the
no-cloud tool path.

| Direct OpenAI model | Best Eva role | Standard short-context input / output per 1M tokens |
|---|---|---:|
| GPT-5.6 Luna | Fast, cost-sensitive conversation and review | $0.20 / $1.20 |
| GPT-5.6 Terra | Balanced intelligence and cost | $2.00 / $12.00 |
| GPT-5.6 Sol | Premium complex reasoning | $5.00 / $30.00 |
| GPT-4.1 Nano | Lightweight routing and classification | $0.10 / $0.40 |

Eva defaults to a 16,384-token completion ceiling, accepts explicit limits up
to 128,000, and caps adaptive reviewer calls at 8,192 tokens. The ceiling does
not force longer responses; provider `length` finishes are surfaced as a
truncation warning.

For persistent memory, point Settings > MCP at an Azure Data Explorer cluster, or use the default local SQLite backend (zero setup). For semantic recall, add an OpenAI key in Settings > Auth (falls back to keyword matching without one).

For Signal notifications, install [signal-cli](https://github.com/AsamK/signal-cli) and link it to your Signal account (`signal-cli link -n "Eva"`). Enter sender and recipient numbers in Settings > Auth.

### Learning and consent

Settings > Learning controls local feedback, routine outcomes, voice diagnostics, and optional standing consent for routine read/search tools. Records are bounded, expire according to the configured retention period, and can be revoked or deleted from the same panel. Explicit response feedback produces short, fixed adaptive guidance only for the originating chat session while its retained source signal remains active; deleting the signal or revoking feedback consent removes that guidance from future prompts. Inferred outcomes never alter adaptive guidance or explicit memory. Read/search/fetch/think and a narrow allowlist of inspection commands proceed autonomously. Interpreters, opaque execute requests, writes, remote mutations, edit, and delete require a fresh in-chat Allow once decision. Eva controls active native forms through typed field schemas rather than screen automation.

### Local Voices

Eva's **Local Voices** engine defaults to **Eva English** and supports **Automatic**, English, and Korean speech. Automatic uses deterministic Hangul detection, preserves mixed English/Korean playback order, and selects Eva's bundled Korean profile for Korean spans while keeping English as the default profile. Eva Korean and AppaTalks English are also available as explicit profile choices. Imported recordings are intentionally unclassified, so Automatic uses a bundled language-matched profile rather than guessing an imported recording's language.

```bash
./install.sh --voice-deps
```

This creates a separate Python 3.11 environment at `~/.local/share/eva/local-voices/.venv` using Chatterbox English plus Multilingual V3, Eva's maintained `tools/voice_clone_module` adapter, and Faster Whisper with Silero VAD. Korean and Automatic local transcription use a multilingual Whisper model; the first use downloads its selected model and Chatterbox Multilingual V3 (about 3.2 GB), while later cached use remains local. Chatterbox and Perth remain external dependencies: Chatterbox is MIT-licensed, and Perth is pinned to its source commit by the installer. Eva intentionally uses six newer tested dependency versions than Chatterbox 0.1.7 declares; the installer verifies that no other dependency conflict is present. The standalone app creates a token-protected loopback speech service automatically and does not upload microphone audio to a cloud transcription service.

Import skills from text, URLs, GitHub repos, or files in Settings. Eva normalizes them into her format, stores in ADX, and applies matching skills automatically.

## Documentation

- [README-2.md](README-2.md): architecture, MCP, ACP, browser-only setup, roadmap
- [standalone/README.md](standalone/README.md): AppImage build and runtime
- [Website](https://appatalks.github.io/eva-agent/): features, comparison, install guide

