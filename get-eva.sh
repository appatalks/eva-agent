#!/usr/bin/env bash
# ============================================================================
#  Eva AI Assistant — remote installer
#
#  curl -fsSL https://appatalks.github.io/eva-agent/install.sh | bash
#
#  What it does:
#    1. Checks for git, python3, node (installs if missing and user confirms)
#    2. Clones the repo to ~/.eva (or updates if it exists)
#    3. Runs the local install.sh to set up all dependencies
#    4. Builds the standalone AppImage
#    5. Creates a symlink so `eva` works from anywhere
#
#  Options (passed as env vars before the pipe):
#    EVA_DIR=~/my-eva    — install location (default: ~/.eva)
#    EVA_YES=1           — skip all prompts
#    EVA_CHECK=1         — report only, don't install
#    EVA_NO_BUILD=1      — skip AppImage build
# ============================================================================

set -euo pipefail

EVA_DIR="${EVA_DIR:-$HOME/.eva}"
EVA_YES="${EVA_YES:-0}"
EVA_CHECK="${EVA_CHECK:-0}"
EVA_NO_BUILD="${EVA_NO_BUILD:-0}"
EVA_REPO="https://github.com/appatalks/eva-agent.git"
EVA_BIN="$HOME/.local/bin"

# Colors
if [ -t 1 ]; then
  R=$'\033[0;31m'; G=$'\033[0;32m'; Y=$'\033[1;33m'
  C=$'\033[0;36m'; B=$'\033[1m'; N=$'\033[0m'
else
  R=""; G=""; Y=""; C=""; B=""; N=""
fi

info() { printf '%s[eva]%s %s\n' "$C" "$N" "$*"; }
ok()   { printf '%s[eva]%s %s\n' "$G" "$N" "$*"; }
warn() { printf '%s[eva]%s %s\n' "$Y" "$N" "$*"; }
fail() { printf '%s[eva]%s %s\n' "$R" "$N" "$*"; exit 1; }

confirm() {
  [ "$EVA_YES" = "1" ] && return 0
  printf '%s[eva]%s %s [Y/n] ' "$Y" "$N" "$1"
  read -r ans
  case "$ans" in
    [Nn]*) return 1 ;;
    *) return 0 ;;
  esac
}

# ── Platform detection ──────────────────────────────────────────────────────
OS="$(uname -s)"
ARCH="$(uname -m)"

case "$OS" in
  Linux)  PLATFORM="linux" ;;
  Darwin) PLATFORM="macos" ;;
  *)      fail "Unsupported OS: $OS. Eva supports Linux and macOS." ;;
esac

info "Eva AI Assistant installer"
info "Platform: $PLATFORM ($ARCH)"
info "Install directory: $EVA_DIR"
echo

# ── Check / install prerequisites ──────────────────────────────────────────
check_cmd() {
  command -v "$1" >/dev/null 2>&1
}

install_prereq() {
  local cmd="$1" pkg="$2"
  if check_cmd "$cmd"; then
    ok "$cmd found: $(command -v "$cmd")"
    return 0
  fi
  warn "$cmd not found."
  if [ "$EVA_CHECK" = "1" ]; then return 1; fi

  if [ "$PLATFORM" = "linux" ]; then
    if check_cmd apt-get; then
      confirm "Install $pkg via apt?" && sudo apt-get update -qq && sudo apt-get install -y -qq "$pkg"
    elif check_cmd dnf; then
      confirm "Install $pkg via dnf?" && sudo dnf install -y "$pkg"
    elif check_cmd pacman; then
      confirm "Install $pkg via pacman?" && sudo pacman -S --noconfirm "$pkg"
    else
      fail "No supported package manager found. Install $cmd manually."
    fi
  elif [ "$PLATFORM" = "macos" ]; then
    if ! check_cmd brew; then
      confirm "Install Homebrew first?" && /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    fi
    confirm "Install $pkg via brew?" && brew install "$pkg"
  fi

  check_cmd "$cmd" || fail "Failed to install $cmd."
  ok "$cmd installed."
}

install_prereq git git
install_prereq python3 python3
install_prereq node nodejs

# Node version check
NODE_MAJOR="$(node --version 2>/dev/null | sed 's/v//' | cut -d. -f1)"
if [ -n "$NODE_MAJOR" ] && [ "$NODE_MAJOR" -lt 24 ]; then
  warn "Node.js $NODE_MAJOR found, but Eva needs 24+."
  if [ "$PLATFORM" = "linux" ] && [ "$EVA_CHECK" != "1" ]; then
    if confirm "Install Node.js 24 via NodeSource?"; then
      curl -fsSL https://deb.nodesource.com/setup_24.x | sudo -E bash -
      sudo apt-get install -y nodejs
      ok "Node.js $(node --version) installed."
    fi
  elif [ "$PLATFORM" = "macos" ] && [ "$EVA_CHECK" != "1" ]; then
    confirm "Upgrade node via brew?" && brew install node@24
  fi
fi

echo
if [ "$EVA_CHECK" = "1" ]; then
  info "Check mode: no changes made."
  exit 0
fi

# ── Clone or update ────────────────────────────────────────────────────────
if [ -d "$EVA_DIR/.git" ]; then
  info "Updating existing installation..."
  cd "$EVA_DIR"
  git pull --ff-only origin main || warn "Pull failed; continuing with current version."
else
  info "Cloning Eva..."
  git clone --depth 1 "$EVA_REPO" "$EVA_DIR"
  cd "$EVA_DIR"
fi

# ── Run local installer ───────────────────────────────────────────────────
if [ -f install.sh ]; then
  info "Running dependency installer..."
  INSTALL_ARGS="--yes"
  if [ "$EVA_NO_BUILD" != "1" ]; then
    INSTALL_ARGS="$INSTALL_ARGS --build"
  fi
  bash install.sh $INSTALL_ARGS
fi

# ── Create launcher symlink ───────────────────────────────────────────────
mkdir -p "$EVA_BIN"

APPIMAGE="$(find "$EVA_DIR/standalone/dist" -name '*.AppImage' -type f 2>/dev/null | sort -V | tail -1)"

if [ -n "$APPIMAGE" ]; then
  ln -sf "$APPIMAGE" "$EVA_BIN/eva"
  ok "Symlinked: eva -> $APPIMAGE"
else
  # Fallback: create a launcher script that starts the bridge + opens browser
  cat > "$EVA_BIN/eva" <<'LAUNCHER'
#!/usr/bin/env bash
EVA_HOME="${EVA_HOME:-$HOME/.eva}"
cd "$EVA_HOME" || { echo "Eva not found at $EVA_HOME"; exit 1; }
echo "Starting Eva bridge on http://localhost:8888..."
python3 tools/acp_bridge.py &
BRIDGE_PID=$!
sleep 2
if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "http://localhost:8888" 2>/dev/null || true
elif command -v open >/dev/null 2>&1; then
  open "http://localhost:8888" || true
fi
echo "Eva bridge running (PID $BRIDGE_PID). Press Ctrl+C to stop."
wait $BRIDGE_PID
LAUNCHER
  chmod +x "$EVA_BIN/eva"
  ok "Created launcher: $EVA_BIN/eva"
fi

# ── Create .desktop entry (Linux app launcher icon) ─────────────────────────
DESKTOP_DIR="$HOME/.local/share/applications"
mkdir -p "$DESKTOP_DIR"
EVA_ICON="$EVA_DIR/core/img/eva-icon.svg"
EVA_EXEC="$EVA_BIN/eva"

cat > "$DESKTOP_DIR/eva.desktop" <<EOF
[Desktop Entry]
Name=Eva
Comment=Eva AI Assistant
Exec=$EVA_EXEC
Icon=$EVA_ICON
Terminal=false
Type=Application
Categories=Utility;ArtificialIntelligence;
StartupWMClass=eva-standalone
EOF
chmod 644 "$DESKTOP_DIR/eva.desktop"
ok "Desktop entry: $DESKTOP_DIR/eva.desktop"

# Ensure ~/.local/bin is on PATH
case ":$PATH:" in
  *":$EVA_BIN:"*) ;;
  *)
    SHELL_RC=""
    if [ -f "$HOME/.bashrc" ]; then SHELL_RC="$HOME/.bashrc"
    elif [ -f "$HOME/.zshrc" ]; then SHELL_RC="$HOME/.zshrc"
    fi
    if [ -n "$SHELL_RC" ]; then
      if ! grep -q 'local/bin' "$SHELL_RC" 2>/dev/null; then
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$SHELL_RC"
        info "Added ~/.local/bin to PATH in $SHELL_RC"
      fi
    fi
    export PATH="$EVA_BIN:$PATH"
    ;;
esac

echo
echo "${B}Eva is installed.${N}"
echo
echo "  Run:  ${G}eva${N}"
echo
echo "  Update:  ${C}cd $EVA_DIR && git pull && ./install.sh --build${N}"
echo "  Docs:    ${C}https://appatalks.github.io/eva-agent/${N}"
echo
