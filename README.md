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
./dist/'Eva Standalone-5.5.0.AppImage'
```

Prereqs: Node.js 24+, Python 3.12+, GitHub Copilot CLI (`copilot auth login`).

## Features

| | |
|---|---|
| **Camera vision** | Webcam presence sensing, face-detection auto-wake, on-demand "look" with gpt-4o |
| **Browser agent** | Playwright-based DOM control, persistent Chrome login, hybrid vision fallback |
| **Desktop agent** | PyAutoGUI mouse/keyboard control, optional AT-SPI via computer-use-linux MCP |
| **Voice interface** | Full-screen voice orb, wake/barge-in, TTS (OpenAI, Polly, Local Voices, browser) |
| **Signal messaging** | Send-only text notifications via signal-cli, keyword-triggered or on-demand |
| **Persistent memory** | Kusto/ADX or local SQLite: conversations, emotion tracking, semantic recall |
| **User profiles** | Switch local profiles with separate sessions, prompts, model choices, and UI preferences |
| **Self-improving skills** | Auto-extracts reusable skills from successful tasks, stored as drafts |
| **Cron scheduler** | Standard cron expressions, recurring prompts, morning briefings, alerts |
| **Subagent parallelism** | Spawn up to 4 concurrent ACP tasks, results via notifications |
| **Agent operations** | Live scorecard for subagents, browser/desktop runs, steering, and animated memory topology |
| **Multi-provider** | OpenAI, Google Gemini, GitHub Copilot, lm-studio (local) |
| **Reasoning controls** | Model-specific effort levels for OpenAI, GitHub Models, and Copilot CLI ACP |
| **Doctor diagnostics** | Structured readiness probe for every subsystem with actionable fixes |
| **MCP ecosystem** | Azure, GitHub, Kusto, computer-use-linux desktop control |
| **Adaptive review** | Fast direct Eva responses with GPT-5.6 Terra review for consequential turns |
| **Dual data mode** | Cloud (Copilot CLI + MCP) or Local (LM Studio + direct MCP, fully offline) |

## Get started

Select **Eva (AIG)** in the model dropdown for the full experience.

For persistent memory, point Settings > MCP at an Azure Data Explorer cluster, or use the default local SQLite backend (zero setup). For semantic recall, add an OpenAI key in Settings > Auth (falls back to keyword matching without one).

For Signal notifications, install [signal-cli](https://github.com/AsamK/signal-cli) and link it to your Signal account (`signal-cli link -n "Eva"`). Enter sender and recipient numbers in Settings > Auth.

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

