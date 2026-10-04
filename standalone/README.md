# Eva Standalone

This directory contains the Electron scaffold for the standalone builds. The Electron files stay under `standalone/`; electron-builder copies the parent web UI and bridge into `resources/app` with `extraResources`. In development, `main.js` loads the parent repo directly.

## Prerequisites

- Core chat: Python >= 3.12 with working `requests` and `cryptography` packages.
- Copilot ACP: Node.js >= 24, GitHub Copilot CLI, and completed `copilot auth login`.
  Copilot is not required for direct OpenAI or LM Studio chat.
- Development/build: Node.js >= 24.
- Linux builds: `readelf` (binutils) and `zsyncmake` (zsync). The dependency
  installer checks these when invoked with `--build` or `--check`.

## Run In Development

```sh
cd standalone
npm install
npm run start
```

## Download and First Launch

Download the AppImage for your architecture from
[GitHub Releases](https://github.com/appatalks/eva-agent/releases), make it
executable, and open it. The currently published Linux release is x86_64.
Building on ARM is a separate source-build path, not a published ARM release.

```sh
chmod +x "Eva.Standalone-<version>.AppImage"
./"Eva.Standalone-<version>.AppImage"
```

New AppImages enable coding workspaces without an extra launch flag. Use
`--eva-no-workspaces` to run chat without the native terminal. Source and
unpacked development launches retain their explicit opt-in flag.

Before starting the bridge, Eva checks the selected Python version and core
packages. Missing prerequisites open a setup dialog with **Retry**, **Copy setup
instructions**, **Open setup guide**, and **Quit**. Eva never installs packages
from this dialog. It prefers the managed Eva interpreter, or an explicitly
configured `EVA_PYTHON`, before falling back to `python3`. Linux GUI launches
also search the user's standard `.local/bin` and `.npm-global/bin` directories.

On the first successful launch, Eva explains provider setup and optional
Copilot requirements. Choose your backend in **Settings > Models**, configure
credentials in **Settings > Auth**, and use **Settings > General > Run
diagnostics** for optional capabilities. Installing a CLI is not authentication;
Copilot still needs an interactive `copilot auth login`.

Download-only users do not have `install.sh` beside their AppImage. For the
managed setup path, download and review the official helper, then run it:

```sh
setup_file="$(mktemp)"
curl -fsSL https://appatalks.github.io/eva-agent/get-eva.sh -o "$setup_file"
# Review the downloaded script before running it.
bash "$setup_file"
rm -f -- "$setup_file"
```

The helper provisions an installed source copy and builds its AppImage; it is
not a hidden action performed by the downloaded app. Alternatively, create a
Python 3.12+ environment containing `requests` and `cryptography` and set
`EVA_PYTHON` to that environment's interpreter. Optional tools have their own
dependencies and must be set up separately.

For desktop-menu integration without the Eva installer, an external tool such
as AppImageLauncher can integrate the downloaded file. Moving or deleting a
manually integrated AppImage can break its shortcut.

## Build AppImage

```sh
cd standalone
npm install
npm run dist
```

Output lands in `standalone/dist/`, named like `Eva Standalone-<version>.AppImage` (the version comes from `package.json`).

The AppImage build is configured in `package.json`; `package-lock.json` is
tracked, while generated `dist/` output is ignored.

### Runtime, Compatibility, and Updates

Linux packaging pins electron-builder's `appimage` toolset to `1.0.3`, which
uses a static type-2 runtime. The AppImage launcher does not require the host's
glibc or `libfuse2`. It still needs a usable kernel FUSE interface to mount;
`--appimage-extract-and-run` remains available when mounting is unavailable.
This does not make the application payload fully self-contained: Electron
still uses host glibc and desktop libraries, and Eva's bridge and ACP features
retain the Python, Node.js, and Copilot CLI prerequisites above. No older
Linux/glibc support is implied by changing the AppImage runtime.
The inspected x86_64 payload still references `GLIBC_2.34` in the bundled
`node-pty` native terminal module; this change does not lower that requirement.
Tagged Linux builds run on Ubuntu 22.04 and reject native payloads requiring
glibc newer than 2.35. The supported baseline is an x86_64 desktop with glibc
2.35 or newer and Electron-compatible desktop libraries (for example Ubuntu
22.04+). This is a build compatibility ceiling, not a claim of testing every
distribution or of supporting musl-based distributions.

The embedded desktop entry is named **Eva**, matching the UI and installed
launcher. Release filenames retain `Eva Standalone-<version>.AppImage`
(GitHub release uploads replace spaces with dots).

`appimage-update.js` runs only for AppImage artifacts. It embeds
`gh-releases-zsync|appatalks|eva-agent|latest|Eva.Standalone-*.AppImage.zsync`
in the runtime's reserved update-information section, rebuilds the embedded
Electron blockmap after that modification, and generates
`Eva.Standalone-<version>.AppImage.zsync` with `zsyncmake`. A missing tool,
invalid runtime section, or failed verification fails the build.

The tag-triggered release workflow publishes the AppImage and its `.zsync`
sidecar together. AppImageUpdate-compatible tools can then discover the latest
stable GitHub release and perform differential updates. This is external
updater support, not an in-app automatic updater. Existing AppImages without
update information need a one-time download of a new release. Locally built
unreleased versions have metadata, but their remote assets do not exist until
the matching `v<version>` release is published.

Release checks enforce matching package/tag versions, static-runtime linkage,
desktop identity and workspace arguments, the exact sidecar URL/length/checksum,
and every embedded blockmap checksum. Publication normalizes AppImage filenames
to dots explicitly and includes `SHA256SUMS` for the AppImage and sidecar.
Checksums detect mismatched downloads; they are not a signing or publisher
authentication mechanism.

Installer-managed launchers discover the highest stable semantic-version AppImage in
their installed distribution directory on each launch, accepting both spaces
and dots in filenames. External updaters should write the replacement into that
same directory. Installer pruning preserves one locally built rollback and
does not delete dotted filenames managed by external updaters. Keep the
previous AppImage until the new version has started successfully; externally
managed old versions can then be removed manually. Updates do not move personal state into the AppImage or reset the
existing configuration and credential storage.

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
chmod +x "Eva Standalone-5.6.10.AppImage"
"./Eva Standalone-5.6.10.AppImage" --eva-workspace-terminal-v1
```

If the host is missing FUSE (common on minimal containers and some distros), launch with extraction instead:

```sh
"./Eva Standalone-5.6.10.AppImage" --appimage-extract-and-run --eva-workspace-terminal-v1
```

The AppImage bundles the UI and bridge source and spawns the ACP bridge using
host Python on a random localhost port at startup. Copilot-backed cloud features
require Copilot CLI to be authenticated once via `copilot auth login`; local-only
LM Studio mode does not.

The package includes `tools/skills/**` and the default-skills catalog as active
runtime resources. Office-format Python packages are host dependencies, not
vendored into the AppImage. In an installed source checkout, run
`./install.sh --check` to see their status and `./install.sh --skill-deps` to
install missing optional packages; download-only users can use the setup path
above. The bridge
never installs a package during a user action. A trusted workspace root may be
configured with `EVA_SKILLS_WORKSPACE_ROOTS` (paths separated by the platform
path separator); otherwise bounded operations use Eva artifacts.

## Runtime Notes

- Electron starts `tools/acp_bridge.py` with `python3` on `127.0.0.1` using a free dynamic port.
- The renderer receives the bridge URL through `window.evaStandalone.acpBaseUrl`.
- Standalone exposes Eva (AIG) only. All routing, cognition, AIG backend selection, and Settings sub-controls remain available.
- GPT-6 Luna is the default AIG model preference, with GPT-6.1 Sol also selectable
  through Copilot ACP or OpenAI direct and GPT-6 Astra through Copilot ACP. Models
  can also use LM Studio. Direct OpenAI preserves Eva's memory, adaptive review,
  and action-marker pipeline without requiring Copilot; ACP-specific MCP retrieval
  and subagents still require Copilot CLI.
- The Kusto database field is intentionally blank on first run. Configure it in Settings > MCP.
- TTS engines: standalone defaults to OpenAI TTS when an OpenAI API key is set in Settings > Auth, otherwise falls back to browser SpeechSynthesis. Optional Local Voices uses an authorized imported PCM WAV profile plus `./install.sh --voice-deps`; its token-protected loopback service also provides local Faster Whisper transcription with Silero VAD for Voice View. Opening Voice View starts warming a small acknowledgement set in the active Local Voices profile and stores clips only in local app data for instant spoken feedback; a requested clip takes priority over background warming. Voice View's Live Translation toggle sends each detected utterance through a short, tool-free translation request and speaks the result with the browser's fast native voice; normal replies continue to use the selected profile. Polly engines (Standard, Neural, Generative) require AWS credentials and are not configured through the standalone Auth tab. Settings > General can select a microphone for Voice View and a supported media playback output; browser wake-word recognition and browser SpeechSynthesis continue to use the operating system default device.
