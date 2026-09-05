# Eva Standalone

This directory contains the Electron scaffold for the standalone builds. The Electron files stay under `standalone/`; electron-builder copies the parent web UI and bridge into `resources/app` with `extraResources`. In development, `main.js` loads the parent repo directly.

## Prerequisites

- Node.js >= 24
- Python >= 3.12
- GitHub Copilot CLI installed on the host
- Copilot CLI authenticated on the host with `copilot auth login`

## Run In Development

```sh
cd standalone
npm install
npm run start
```

## Build AppImage

```sh
cd standalone
npm install
npm run dist
```

Output lands in `standalone/dist/`, named like `Eva Standalone-<version>.AppImage` (the version comes from `package.json`).

The AppImage build is configured in `package.json`; `package-lock.json` is
tracked, while generated `dist/` output is ignored.

## Build Windows Installer (Experimental)

The installer provisions Python 3.12, Node.js 24+, and a private GitHub
Copilot CLI runtime through Windows Package Manager. It opens a terminal for
the account owner to complete the interactive GitHub sign-in; this cannot be
automated or bundled. Build the installer with:

```powershell
cd standalone
npm install
npm run dist:win
```

The NSIS installer is written to `standalone/dist/` as
`Eva Standalone Setup <version>.exe`. The launcher starts the bundled bridge
with `py -3.12` and stores bridge data plus its private Copilot CLI runtime
under the Windows application-data folder.

Windows packaging is an initial compatibility path. Linux-specific desktop
automation and camera discovery remain unsupported on Windows.

## Launch The AppImage

```sh
cd standalone/dist
chmod +x "Eva Standalone-5.6.7.AppImage"
"./Eva Standalone-5.6.7.AppImage" --eva-workspace-terminal-v1
```

If the host is missing FUSE (common on minimal containers and some distros), launch with extraction instead:

```sh
"./Eva Standalone-5.6.7.AppImage" --appimage-extract-and-run --eva-workspace-terminal-v1
```

The AppImage is self-contained: it spawns the bundled ACP bridge on a random
localhost port at startup. Copilot-backed cloud features require Copilot CLI to
be authenticated once via `copilot auth login`; local-only LM Studio mode does
not.

The package includes `tools/skills/**` and the default-skills catalog as active
runtime resources. Office-format Python packages are host dependencies, not
vendored into the AppImage; run `./install.sh --check` to see their status and
`./install.sh --skill-deps` to install missing optional packages. The bridge
never installs a package during a user action. A trusted workspace root may be
configured with `EVA_SKILLS_WORKSPACE_ROOTS` (paths separated by the platform
path separator); otherwise bounded operations use Eva artifacts.

## Runtime Notes

- Electron starts `tools/acp_bridge.py` with `python3` on `127.0.0.1` using a free dynamic port.
- The renderer receives the bridge URL through `window.evaStandalone.acpBaseUrl`.
- Standalone exposes Eva (AIG) only. All routing, cognition, AIG backend selection, and Settings sub-controls remain available.
- Eva backend models can use Copilot ACP, the OpenAI API directly, or LM Studio. Direct OpenAI preserves Eva's memory, adaptive review, and action-marker pipeline without requiring Copilot; ACP-specific MCP retrieval and subagents still require Copilot CLI.
- The Kusto database field is intentionally blank on first run. Configure it in Settings > MCP.
- TTS engines: standalone defaults to OpenAI TTS when an OpenAI API key is set in Settings > Auth, otherwise falls back to browser SpeechSynthesis. Optional Local Voices uses an authorized imported PCM WAV profile plus `./install.sh --voice-deps`; its token-protected loopback service also provides local Faster Whisper transcription with Silero VAD for Voice View. Opening Voice View starts warming a small acknowledgement set in the active Local Voices profile and stores clips only in local app data for instant spoken feedback; a requested clip takes priority over background warming. Voice View's Live Translation toggle sends each detected utterance through a short, tool-free translation request and speaks the result with the browser's fast native voice; normal replies continue to use the selected profile. Polly engines (Standard, Neural, Generative) require AWS credentials and are not configured through the standalone Auth tab. Settings > General can select a microphone for Voice View and a supported media playback output; browser wake-word recognition and browser SpeechSynthesis continue to use the operating system default device.
