#!/usr/bin/env bash
# ============================================================================
#  Eva Installer
#  Detects and installs every dependency Eva needs on this machine, then
#  (optionally) self-updates the checkout and rebuilds the standalone AppImage.
#
#  Usage:
#    ./install.sh                 Check dependencies and install missing ones
#                                 (asks before running anything with sudo)
#    ./install.sh --yes           Install everything missing without prompting
#    ./install.sh --check         Report only; install nothing (doctor mode)
#    ./install.sh --no-skill-deps Skip the optional skill toolchains (PDF/OCR)
#    ./install.sh --build         Rebuild the standalone AppImage at the end
#    ./install.sh --no-update     Do not git-pull / self-update before running
#    ./install.sh --help          Show this help
#
#  Supported platforms: Linux (apt, dnf/yum, pacman, zypper) and macOS (brew).
#
#  To add a NEW dependency later, add one line to the relevant section in
#  register_dependencies() below. That function is the single source of truth.
# ============================================================================

set -u

# ── Colors ──────────────────────────────────────────────────────────────────
if [ -t 1 ]; then
  RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'
  CYAN=$'\033[0;36m'; BOLD=$'\033[1m'; NC=$'\033[0m'
else
  RED=""; GREEN=""; YELLOW=""; CYAN=""; BOLD=""; NC=""
fi
info() { printf '%s[INFO]%s %s\n' "$CYAN" "$NC" "$*"; }
ok()   { printf '%s[ OK ]%s %s\n' "$GREEN" "$NC" "$*"; }
warn() { printf '%s[WARN]%s %s\n' "$YELLOW" "$NC" "$*"; }
err()  { printf '%s[FAIL]%s %s\n' "$RED" "$NC" "$*"; }
hr()   { printf '%s\n' "------------------------------------------------------------"; }

# ── Flags ───────────────────────────────────────────────────────────────────
ASSUME_YES=0
CHECK_ONLY=0
DO_UPDATE=1
DO_BUILD=0
WANT_SKILL_DEPS=1

for arg in "$@"; do
  case "$arg" in
    --yes|-y)        ASSUME_YES=1 ;;
    --check|--dry-run) CHECK_ONLY=1 ;;
    --no-update)     DO_UPDATE=0 ;;
    --build)         DO_BUILD=1 ;;
    --skill-deps)    WANT_SKILL_DEPS=1 ;;
    --no-skill-deps) WANT_SKILL_DEPS=0 ;;
    --help|-h)
      sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *)
      err "Unknown option: $arg (try --help)"; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ── Parallel arrays acting as the dependency queue (bash 3.2 compatible) ─────
MISSING_DESC=""    # newline-separated descriptions
MISSING_CMD=""     # newline-separated install commands (same order)
NEED_SUDO=0
PRESENT_COUNT=0

queue() {  # queue "<description>" "<install command>"
  MISSING_DESC="${MISSING_DESC}${1}"$'\n'
  MISSING_CMD="${MISSING_CMD}${2}"$'\n'
  case "$2" in *"sudo "*) NEED_SUDO=1 ;; esac
}

# ── Detection helpers ───────────────────────────────────────────────────────
have_cmd()   { command -v "$1" >/dev/null 2>&1; }

# Pick a Python interpreter to probe modules with (matches what the bridge runs).
PYTHON="python3"; have_cmd python3 || PYTHON="python"
have_pymod() { "$PYTHON" -c "import $1" >/dev/null 2>&1; }

# version_ge "3.12.1" "3.12" -> 0 (true) when first >= second
version_ge() {
  [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]
}

# Detect a downloaded Playwright Chromium build. Playwright stores browsers in a
# per-user cache (override: PLAYWRIGHT_BROWSERS_PATH). A chromium-* directory
# there means the browser is installed, so we do not re-offer it every run.
playwright_chromium_present() {
  local base
  if [ -n "${PLAYWRIGHT_BROWSERS_PATH:-}" ]; then
    base="$PLAYWRIGHT_BROWSERS_PATH"
  elif [ "$OS" = "macos" ]; then
    base="$HOME/Library/Caches/ms-playwright"
  else
    base="$HOME/.cache/ms-playwright"
  fi
  [ -d "$base" ] || return 1
  ls -d "$base"/chromium-* >/dev/null 2>&1
}

# ── OS + package manager detection ──────────────────────────────────────────
OS="unknown"; PKG=""; PKG_INSTALL=""; PKG_UPDATE=""
detect_platform() {
  local uname_s; uname_s="$(uname -s)"
  case "$uname_s" in
    Linux)  OS="linux" ;;
    Darwin) OS="macos" ;;
    *)      OS="unknown" ;;
  esac

  if [ "$OS" = "macos" ]; then
    PKG="brew"
    if ! have_cmd brew; then
      warn "Homebrew not found. Install it from https://brew.sh then re-run."
    fi
    PKG_INSTALL="brew install"
    PKG_UPDATE="brew update"
  elif [ "$OS" = "linux" ]; then
    if   have_cmd apt-get; then PKG="apt";    PKG_INSTALL="sudo apt-get install -y"; PKG_UPDATE="sudo apt-get update"
    elif have_cmd dnf;     then PKG="dnf";    PKG_INSTALL="sudo dnf install -y";     PKG_UPDATE=":"
    elif have_cmd yum;     then PKG="yum";    PKG_INSTALL="sudo yum install -y";     PKG_UPDATE=":"
    elif have_cmd pacman;  then PKG="pacman"; PKG_INSTALL="sudo pacman -S --noconfirm"; PKG_UPDATE="sudo pacman -Sy"
    elif have_cmd zypper;  then PKG="zypper"; PKG_INSTALL="sudo zypper install -y";   PKG_UPDATE=":"
    else PKG=""; fi
  fi
}

# pip install command (user scope so the bare-python3 bridge can import them).
pip_install_cmd() {  # echoes a command that installs the given pip package(s)
  printf '%s -m pip install --user %s' "$PYTHON" "$*"
}

# Resolve a per-manager system package name. Empty field => not available there.
# Usage: sys_pkg_name <apt> <dnf/yum> <pacman> <zypper> <brew>
sys_pkg_name() {
  case "$PKG" in
    apt)            printf '%s' "$1" ;;
    dnf|yum)        printf '%s' "$2" ;;
    pacman)         printf '%s' "$3" ;;
    zypper)         printf '%s' "$4" ;;
    brew)           printf '%s' "$5" ;;
    *)              printf '' ;;
  esac
}

# Queue a system package by command name, mapping the package per manager.
need_sys() {  # need_sys <checkcmd> <desc> <apt> <dnf> <pacman> <zypper> <brew>
  local checkcmd="$1" desc="$2"
  if have_cmd "$checkcmd"; then ok "$desc ($checkcmd present)"; PRESENT_COUNT=$((PRESENT_COUNT+1)); return; fi
  local pkg; pkg="$(sys_pkg_name "$3" "$4" "$5" "$6" "$7")"
  if [ -z "$PKG" ] || [ -z "$pkg" ]; then
    warn "$desc missing and no known install recipe for this platform ($checkcmd)"
    queue "$desc [manual]" "echo 'Install $checkcmd manually'"
    return
  fi
  queue "$desc" "$PKG_INSTALL $pkg"
}

# Queue a pip package by import name.
need_pymod() {  # need_pymod <import_name> <pip_name> <desc>
  if have_pymod "$1"; then ok "$3 (python: $1)"; PRESENT_COUNT=$((PRESENT_COUNT+1)); return; fi
  queue "$3" "$(pip_install_cmd "$2")"
}

# ── Dependency registry — the single source of truth ────────────────────────
# Add new dependencies here. Each block reports present items and queues
# missing ones with a platform-correct install command.
register_dependencies() {
  hr; info "Core runtime"; hr

  # Architecture (report only; cannot install a CPU).
  local arch; arch="$(uname -m)"
  case "$arch" in
    x86_64|amd64|arm64|aarch64) ok "CPU architecture: $arch (Copilot CLI supported)"; PRESENT_COUNT=$((PRESENT_COUNT+1)) ;;
    *) warn "CPU architecture: $arch (Copilot CLI needs x86_64 or arm64; run the bridge on a 64-bit host)" ;;
  esac

  need_sys git  "git"  git  git  git  git  git
  need_sys curl "curl" curl curl curl curl curl

  # Python >= 3.12
  if have_cmd "$PYTHON"; then
    local pyver; pyver="$("$PYTHON" -c 'import platform;print(platform.python_version())' 2>/dev/null || echo 0)"
    if version_ge "$pyver" "3.12"; then
      ok "Python $pyver (>= 3.12)"; PRESENT_COUNT=$((PRESENT_COUNT+1))
    else
      warn "Python $pyver found; Eva wants >= 3.12"
      local p; p="$(sys_pkg_name python3 python3 python python3 python@3.12)"
      [ -n "$PKG" ] && [ -n "$p" ] && queue "Python >= 3.12" "$PKG_INSTALL $p" || warn "Upgrade Python 3.12+ manually"
    fi
  else
    local p; p="$(sys_pkg_name python3 python3 python python3 python@3.12)"
    queue "Python 3 (>= 3.12)" "$PKG_INSTALL $p"
  fi

  # pip for the chosen interpreter
  if "$PYTHON" -m pip --version >/dev/null 2>&1; then
    ok "pip (for $PYTHON)"; PRESENT_COUNT=$((PRESENT_COUNT+1))
  else
    need_sys pip3 "pip" python3-pip python3-pip python-pip python3-pip ""
  fi

  # Node.js >= 24
  if have_cmd node; then
    local nodever; nodever="$(node --version 2>/dev/null | sed 's/^v//')"
    if version_ge "$nodever" "24.0.0"; then
      ok "Node.js v$nodever (>= 24)"; PRESENT_COUNT=$((PRESENT_COUNT+1))
    else
      warn "Node.js v$nodever found; Copilot CLI needs >= 24"
      queue_node_install
    fi
  else
    warn "Node.js not found (Copilot CLI needs >= 24)"
    queue_node_install
  fi

  # Copilot CLI
  if have_cmd copilot; then
    ok "Copilot CLI present"; PRESENT_COUNT=$((PRESENT_COUNT+1))
  else
    queue "GitHub Copilot CLI" "npm install -g @github/copilot"
  fi

  # FUSE (needed to run the AppImage); skip on macOS.
  if [ "$OS" = "linux" ]; then
    if ldconfig -p 2>/dev/null | grep -qi 'libfuse\.so\.2\|libfuse2'; then
      ok "FUSE (libfuse2) present"; PRESENT_COUNT=$((PRESENT_COUNT+1))
    else
      need_sys fusermount "FUSE (for AppImage)" libfuse2 fuse fuse2 libfuse2 ""
    fi
  fi

  hr; info "Bridge Python packages"; hr
  need_pymod requests        requests        "requests"
  need_pymod azure.identity  azure-identity  "azure-identity"
  need_pymod msal            msal            "msal"

  hr; info "Browser agent"; hr
  if have_pymod playwright; then
    ok "playwright (python) present"; PRESENT_COUNT=$((PRESENT_COUNT+1))
    if playwright_chromium_present; then
      ok "Playwright Chromium browser present"; PRESENT_COUNT=$((PRESENT_COUNT+1))
    else
      queue "Playwright Chromium browser" "$PYTHON -m playwright install chromium"
    fi
  else
    queue "playwright (python)" "$(pip_install_cmd playwright)"
    queue "Playwright Chromium browser" "$PYTHON -m playwright install chromium"
  fi

  hr; info "Desktop agent (computer use)"; hr
  if have_pymod pyautogui; then
    ok "pyautogui present"; PRESENT_COUNT=$((PRESENT_COUNT+1))
  else
    queue "pyautogui (desktop control)" "$(pip_install_cmd pyautogui)"
  fi

  hr; info "Camera sense (Eva's eyes)"; hr
  if have_pymod cv2; then
    ok "OpenCV (cv2) present"; PRESENT_COUNT=$((PRESENT_COUNT+1))
  else
    queue "opencv-python (camera presence)" "$(pip_install_cmd opencv-python)"
  fi

  if [ "$WANT_SKILL_DEPS" = "1" ]; then
    hr; info "Skill toolchains (PDF / office / OCR)"; hr
    need_pymod pypdf       pypdf       "pypdf (PDF read/merge/split)"
    need_pymod reportlab   reportlab   "reportlab (PDF creation)"
    need_pymod pdfplumber  pdfplumber  "pdfplumber (PDF tables/text)"
    need_pymod pytesseract pytesseract "pytesseract (OCR binding)"
    need_sys pdftotext "poppler-utils (pdftotext/pdfinfo)" poppler-utils poppler-utils poppler poppler-tools poppler
    need_sys qpdf      "qpdf (PDF transform)"               qpdf          qpdf          qpdf    qpdf          qpdf
    need_sys tesseract "tesseract OCR engine"               tesseract-ocr tesseract     tesseract tesseract-ocr tesseract
  else
    info "Skipping skill toolchains (--no-skill-deps)"
  fi

  hr; info "Signal notifications (optional)"; hr
  if have_cmd signal-cli; then
    local scver; scver="$(signal-cli --version 2>/dev/null | awk '{print $NF}')"
    ok "signal-cli ${scver:-present}"; PRESENT_COUNT=$((PRESENT_COUNT+1))
  else
    local sc_arch; sc_arch="$(uname -m)"
    if [ "$sc_arch" = "x86_64" ] || [ "$sc_arch" = "amd64" ]; then
      queue "signal-cli (native binary)" \
        "curl -fsSL -o /tmp/signal-cli-native.tar.gz https://github.com/AsamK/signal-cli/releases/latest/download/signal-cli-\$(curl -fsSL https://api.github.com/repos/AsamK/signal-cli/releases/latest | grep -oP '\"tag_name\": \"v\\K[^\"]+')}-Linux-native.tar.gz && tar xzf /tmp/signal-cli-native.tar.gz -C /tmp && install -m 755 /tmp/signal-cli \"\${HOME}/.local/bin/signal-cli\" && rm -f /tmp/signal-cli /tmp/signal-cli-native.tar.gz"
    else
      warn "signal-cli: no prebuilt native binary for $sc_arch. Install manually from https://github.com/AsamK/signal-cli"
    fi
  fi
}

# Node 24 install differs sharply per platform; keep it in one place.
queue_node_install() {
  case "$PKG" in
    brew)   queue "Node.js >= 24" "brew install node" ;;
    pacman) queue "Node.js >= 24" "sudo pacman -S --noconfirm nodejs npm" ;;
    apt)    queue "Node.js >= 24 (NodeSource)" "curl -fsSL https://deb.nodesource.com/setup_24.x | sudo -E bash - && sudo apt-get install -y nodejs" ;;
    dnf|yum) queue "Node.js >= 24 (NodeSource)" "curl -fsSL https://rpm.nodesource.com/setup_24.x | sudo bash - && sudo $PKG install -y nodejs" ;;
    zypper) queue "Node.js >= 24" "sudo zypper install -y nodejs24 || sudo zypper install -y nodejs" ;;
    *)      queue "Node.js >= 24 [manual]" "echo 'Install Node.js 24+ from https://nodejs.org or nvm'" ;;
  esac
}

# ── Self-update (git pull + re-exec if the installer changed) ───────────────
self_update() {
  [ "$DO_UPDATE" = "1" ] || { info "Skipping self-update (--no-update)"; return; }
  [ "${EVA_INSTALLER_REEXEC:-0}" = "1" ] && return   # already re-execed once
  have_cmd git || return
  git rev-parse --is-inside-work-tree >/dev/null 2>&1 || return
  # Only auto-update a clean checkout that tracks a remote branch.
  if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    info "Working tree has local changes; skipping auto-update."
    return
  fi
  local upstream; upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
  [ -n "$upstream" ] || return
  info "Checking for Eva updates ($upstream)..."
  git fetch --quiet 2>/dev/null || { warn "git fetch failed; continuing offline."; return; }
  local before after
  before="$(git rev-parse HEAD 2>/dev/null)"
  if git merge-base --is-ancestor HEAD "$upstream" 2>/dev/null && [ "$before" != "$(git rev-parse "$upstream")" ]; then
    local sh_before sh_after
    sh_before="$(cksum "$0" 2>/dev/null)"
    if git pull --ff-only --quiet 2>/dev/null; then
      after="$(git rev-parse HEAD 2>/dev/null)"
      ok "Updated Eva: ${before:0:8} -> ${after:0:8}"
      sh_after="$(cksum "$0" 2>/dev/null)"
      if [ "$sh_before" != "$sh_after" ]; then
        info "Installer changed; re-running the updated version..."
        EVA_INSTALLER_REEXEC=1 exec "$0" "$@"
      fi
    else
      warn "Auto-update skipped (could not fast-forward)."
    fi
  else
    ok "Eva is up to date."
  fi
}

# ── Build the standalone AppImage ───────────────────────────────────────────
build_appimage() {
  [ -d "$SCRIPT_DIR/standalone" ] || { warn "No standalone/ directory; skipping build."; return; }
  have_cmd npm || { warn "npm not available; cannot build the AppImage."; return; }
  info "Building the standalone AppImage..."
  ( cd "$SCRIPT_DIR/standalone" \
      && { [ -d node_modules ] || npm install; } \
      && npm run dist ) \
    && ok "AppImage rebuilt under standalone/dist/" \
    || err "AppImage build failed (see output above)."
}

# ── Run the queued installs ─────────────────────────────────────────────────
run_installs() {
  local count; count="$(printf '%s' "$MISSING_DESC" | grep -c . || true)"
  if [ "${count:-0}" -eq 0 ]; then
    ok "All required dependencies are present. Nothing to install."
    return 0
  fi

  hr
  printf '%sThe following %s item(s) are missing:%s\n' "$BOLD" "$count" "$NC"
  printf '%s' "$MISSING_DESC" | grep . | while IFS= read -r d; do printf '  - %s\n' "$d"; done
  hr

  if [ "$CHECK_ONLY" = "1" ]; then
    info "Doctor mode (--check): install commands that would run:"
    printf '%s' "$MISSING_CMD" | grep . | while IFS= read -r c; do printf '  $ %s\n' "$c"; done
    return 0
  fi

  if [ "$NEED_SUDO" = "1" ]; then
    warn "Some installs need sudo (system packages)."
  fi

  if [ "$ASSUME_YES" != "1" ]; then
    printf '%sProceed with installation? [y/N] %s' "$BOLD" "$NC"
    read -r reply
    case "$reply" in y|Y|yes|YES) ;; *) info "Aborted. No changes made."; return 1 ;; esac
  fi

  [ -n "$PKG_UPDATE" ] && [ "$PKG_UPDATE" != ":" ] && { info "Refreshing package index..."; eval "$PKG_UPDATE" || warn "Package index refresh failed; continuing."; }

  local fails=0 idx=0
  # Iterate the two parallel lists in lockstep.
  local descs cmds; descs="$MISSING_DESC"; cmds="$MISSING_CMD"
  while IFS= read -r desc && IFS= read -r cmd <&3; do
    [ -z "$desc" ] && continue
    info "Installing: $desc"
    if eval "$cmd"; then
      ok "Installed: $desc"
    else
      # pip externally-managed fallback (PEP 668 on Debian/Ubuntu).
      case "$cmd" in
        *"-m pip install --user"*)
          warn "Retrying with --break-system-packages..."
          if eval "${cmd/--user/--user --break-system-packages}"; then ok "Installed: $desc"; else err "Failed: $desc"; fails=$((fails+1)); fi ;;
        *)
          err "Failed: $desc"; fails=$((fails+1)) ;;
      esac
    fi
    idx=$((idx+1))
  done < <(printf '%s' "$descs") 3< <(printf '%s' "$cmds")

  hr
  if [ "$fails" -eq 0 ]; then ok "All installs completed."; else warn "$fails install(s) failed; review the output above."; fi
  return 0
}

# ── Main ────────────────────────────────────────────────────────────────────
main() {
  printf '\n%s==== Eva Installer ====%s\n\n' "$BOLD" "$NC"
  self_update "$@"
  detect_platform
  info "Platform: $OS  Package manager: ${PKG:-none}  Python: $PYTHON"
  register_dependencies
  hr
  info "$PRESENT_COUNT dependency check(s) already satisfied."
  run_installs

  if [ "$CHECK_ONLY" != "1" ]; then
    if [ "$DO_BUILD" = "1" ]; then
      build_appimage
    elif [ "$ASSUME_YES" != "1" ] && [ -d "$SCRIPT_DIR/standalone" ] && have_cmd npm; then
      printf '%sRebuild the standalone AppImage now? [y/N] %s' "$BOLD" "$NC"
      read -r breply
      case "$breply" in y|Y|yes|YES) build_appimage ;; *) info "Skipped AppImage build." ;; esac
    fi
  fi

  hr
  ok "Done. Next steps:"
  printf '   1. Authenticate Copilot if you have not:  %scopilot auth login%s\n' "$BOLD" "$NC"
  printf '   2. Launch Eva:  %sstandalone/dist/Eva Standalone-*.AppImage%s\n' "$BOLD" "$NC"
  printf '\n'
}

main "$@"
