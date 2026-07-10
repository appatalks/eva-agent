# Eva Standalone

This directory contains the Electron scaffold for the Linux AppImage build. The Electron files stay under `standalone/`; electron-builder copies the parent web UI and bridge into `resources/app` with `extraResources`. In development, `main.js` loads the parent repo directly.

## Prerequisites

- Node.js >= 24
- Python >= 3.12
- GitHub Copilot CLI installed and authenticated only when using `EVA_EGRESS_MODE=cloud`
- LM Studio for `offline` or `local-network` operation

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

The AppImage build is configured in `package.json` but this scaffold does not include generated output or a lockfile.

## Launch The AppImage

```sh
cd standalone/dist
chmod +x "Eva Standalone-5.3.0.AppImage"
"./Eva Standalone-5.3.0.AppImage"
```

If the host is missing FUSE (common on minimal containers and some distros), launch with extraction instead:

```sh
"./Eva Standalone-5.3.0.AppImage" --appimage-extract-and-run
```

The AppImage spawns the bundled bridge on a random localhost port at startup.
Cloud mode needs Copilot CLI authenticated once via `copilot auth login`.
`EVA_EGRESS_MODE=offline` or `local-network` starts without ACP and routes Eva
through the configured LM Studio model.

## Runtime Notes

- Electron starts `tools/acp_bridge.py` with `python3` on `127.0.0.1` using a free dynamic port.
- The renderer receives the bridge URL through `window.evaStandalone.acpBaseUrl`.
- Standalone exposes Eva (AIG) only. All routing, cognition, AIG backend selection, and Settings sub-controls remain available.
- The Kusto database field is intentionally blank on first run. Configure it in Settings > MCP.
- TTS engines: standalone defaults to OpenAI TTS when an OpenAI API key is set in Settings > Auth, otherwise falls back to browser SpeechSynthesis. Polly engines (Standard, Neural, Generative) require AWS credentials and are not configured through the standalone Auth tab. Bark is hidden in standalone mode.
