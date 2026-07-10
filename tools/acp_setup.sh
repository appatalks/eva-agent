#!/bin/bash
# ============================================================
#  Eva ACP Bridge — Setup Script
#  Installs Copilot CLI + configures the ACP bridge service
# ============================================================
#
#  Usage:
#    sudo ./tools/acp_setup.sh              # Full install + service
#    sudo ./tools/acp_setup.sh --local      # Local-only (no systemd)
#    sudo ./tools/acp_setup.sh --status     # Check service status
#    sudo ./tools/acp_setup.sh --uninstall  # Remove service
#
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BRIDGE_SCRIPT="$SCRIPT_DIR/acp_bridge.py"
SERVICE_FILE="$SCRIPT_DIR/acp_bridge.service"
SERVICE_NAME="acp-bridge"
BRIDGE_PORT=8888

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
fail()  { echo -e "${RED}[FAIL]${NC} $*"; }

# --- Check architecture ---
check_arch() {
    local arch
    arch=$(uname -m)
    if [[ "$arch" == "x86_64" || "$arch" == "aarch64" || "$arch" == "arm64" ]]; then
        ok "Architecture: $arch (supported)"
        return 0
    else
        fail "Architecture: $arch (not supported by Copilot CLI)"
        echo "    Copilot CLI requires 64-bit (x86_64 or arm64/aarch64)."
        echo "    You can still run the ACP bridge on a 64-bit machine"
        echo "    and point Eva's Settings → Auth → ACP Bridge URL to it."
        return 1
    fi
}

# --- Check/install Node.js ---
check_node() {
    if command -v node &>/dev/null; then
        local ver
        ver=$(node --version | sed 's/v//')
        local major
        major=$(echo "$ver" | cut -d. -f1)
        if (( major >= 24 )); then
            ok "Node.js: v$ver"
            return 0
        else
            fail "Node.js v$ver found; Eva requires v24+"
            return 1
        fi
    else
        fail "Node.js not found"
        echo "    Install Node.js v24+: https://nodejs.org/"
        return 1
    fi
}

# --- Check/install Copilot CLI ---
check_copilot() {
    if command -v copilot &>/dev/null; then
        local ver
        ver=$(copilot --version 2>&1 | head -1)
        if echo "$ver" | grep -qi "requires Node.js"; then
            fail "Copilot CLI installed but Node.js version too old"
            echo "    $ver"
            return 1
        fi
        ok "Copilot CLI: $ver"
        # Check ACP support
        if copilot --help 2>&1 | grep -q "\-\-acp"; then
            ok "ACP support: available"
        else
            warn "ACP support: not found (upgrade with: npm install -g @github/copilot)"
        fi
        return 0
    else
        info "Installing Copilot CLI..."
        npm install -g @github/copilot
        if command -v copilot &>/dev/null; then
            ok "Copilot CLI installed: $(copilot --version 2>&1 | head -1)"
            return 0
        else
            fail "Failed to install Copilot CLI"
            return 1
        fi
    fi
}

# --- Check Copilot authentication ---
check_auth() {
    info "Testing Copilot authentication..."
    local result
    if ! result=$(timeout 45 copilot -p \
        "Reply with exactly EVA_AUTH_OK and nothing else." \
        --silent --no-color 2>&1); then
        warn "Copilot authentication probe failed or timed out"
        echo "    Run: copilot auth login"
        return 1
    fi
    if [[ "$(echo "$result" | tr -d '\r' | xargs)" != "EVA_AUTH_OK" ]]; then
        warn "Copilot authentication probe returned no affirmative result"
        echo "    Run: copilot auth login"
        return 1
    fi
    ok "Copilot authentication: looks good"
    return 0
}

# --- Check Python ---
check_python() {
    if command -v python3 &>/dev/null; then
        local py_major py_minor
        py_major=$(python3 -c 'import sys; print(sys.version_info.major)')
        py_minor=$(python3 -c 'import sys; print(sys.version_info.minor)')
        if (( py_major > 3 || (py_major == 3 && py_minor >= 12) )); then
            ok "Python3: $(python3 --version)"
            return 0
        fi
        fail "$(python3 --version) found; Eva requires Python 3.12+"
        return 1
    else
        fail "Python3 not found"
        return 1
    fi
}

# --- Install systemd service ---
install_service() {
    if [[ ! -f "$SERVICE_FILE" ]]; then
        fail "Service file not found: $SERVICE_FILE"
        return 1
    fi

    info "Installing systemd service..."
    cp "$SERVICE_FILE" /etc/systemd/system/${SERVICE_NAME}.service
    systemctl daemon-reload
    systemctl enable ${SERVICE_NAME}
    systemctl start ${SERVICE_NAME}

    sleep 2
    if systemctl is-active --quiet ${SERVICE_NAME}; then
        ok "Service ${SERVICE_NAME} is running"
        echo ""
        echo "    Bridge URL: http://$(hostname -I | awk '{print $1}'):${BRIDGE_PORT}"
        echo "    Health:     curl http://localhost:${BRIDGE_PORT}/health"
        echo ""
        echo "    Set this URL in Eva → Settings → Auth → ACP Bridge URL"
    else
        fail "Service failed to start"
        echo "    Check logs: journalctl -u ${SERVICE_NAME} -n 20"
        return 1
    fi
}

# --- Status ---
show_status() {
    echo ""
    echo "=== ACP Bridge Status ==="
    echo ""

    # Check if running as systemd service
    if systemctl is-active --quiet ${SERVICE_NAME} 2>/dev/null; then
        ok "Systemd service: active"
        systemctl status ${SERVICE_NAME} --no-pager -l | head -10
    else
        warn "Systemd service: not running"
    fi

    # Check if bridge port is listening
    if command -v ss &>/dev/null; then
        if ss -tlnp 2>/dev/null | grep -q ":${BRIDGE_PORT}"; then
            ok "Port ${BRIDGE_PORT}: listening"
        else
            warn "Port ${BRIDGE_PORT}: not listening"
        fi
    fi

    # Health check
    if command -v curl &>/dev/null; then
        local health
        health=$(curl -s --connect-timeout 3 http://localhost:${BRIDGE_PORT}/health 2>/dev/null || echo "unreachable")
        if echo "$health" | grep -q '"ok"'; then
            ok "Health: $health"
        else
            warn "Health: $health"
        fi
    fi

    echo ""
}

# --- Uninstall ---
uninstall_service() {
    info "Removing systemd service..."
    systemctl stop ${SERVICE_NAME} 2>/dev/null || true
    systemctl disable ${SERVICE_NAME} 2>/dev/null || true
    rm -f /etc/systemd/system/${SERVICE_NAME}.service
    systemctl daemon-reload
    ok "Service removed"
}

# --- Main ---
main() {
    echo ""
    echo "=========================================="
    echo "  Eva ACP Bridge — Setup"
    echo "=========================================="
    echo ""

    case "${1:-}" in
        --status)
            show_status
            exit 0
            ;;
        --uninstall)
            uninstall_service
            exit 0
            ;;
        --local)
            info "Local-only mode (no systemd service)"
            check_python || exit 1
            check_arch || exit 1
            check_node || exit 1
            check_copilot || exit 1
            check_auth || exit 1
            echo ""
            ok "Ready! Start the bridge with:"
            echo "    python3 $BRIDGE_SCRIPT --port $BRIDGE_PORT"
            echo ""
            echo "    Then set ACP Bridge URL in Eva → Settings → Auth"
            echo "    to: http://$(hostname -I | awk '{print $1}'):${BRIDGE_PORT}"
            exit 0
            ;;
    esac

    # Full install
    check_python || exit 1
    check_arch || exit 1
    check_node || exit 1
    check_copilot || exit 1
    check_auth || {
        fail "Copilot authentication is required for cloud ACP setup"
        echo "    Run: copilot auth login"
        exit 1
    }
    echo ""
    install_service
    echo ""
    ok "Setup complete!"
    echo ""
    echo "  Useful commands:"
    echo "    sudo systemctl status ${SERVICE_NAME}    # Check status"
    echo "    sudo systemctl restart ${SERVICE_NAME}   # Restart"
    echo "    sudo journalctl -u ${SERVICE_NAME} -f    # Follow logs"
    echo "    sudo $0 --status                         # Quick status"
    echo "    sudo $0 --uninstall                      # Remove"
    echo ""
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
