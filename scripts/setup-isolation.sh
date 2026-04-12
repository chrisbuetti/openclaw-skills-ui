#!/usr/bin/env bash
# setup-isolation.sh — One-shot setup for OpenClaw per-agent exec isolation
#
# This script does everything:
#   1. Applies the isolation patch to the OCPlatform bundle
#   2. Runs the interactive exec-approvals configuration
#   3. Installs a launchd watchdog to auto-reapply the patch after npm updates
#   4. Restarts the gateway
#
# Usage:
#   cd ocplatform-skills-ui
#   bash scripts/setup-isolation.sh
#
#   # Or with options:
#   bash scripts/setup-isolation.sh --sandbox-all --main main
#   bash scripts/setup-isolation.sh --sandbox agent1 --sandbox agent2
#
# Non-technical users: just run it and follow the prompts.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m' # No Color

info()  { echo -e "${BLUE}ℹ${NC}  $*"; }
ok()    { echo -e "${GREEN}✅${NC} $*"; }
warn()  { echo -e "${YELLOW}⚠️${NC}  $*"; }
fail()  { echo -e "${RED}❌${NC} $*"; }
header() { echo -e "\n${BOLD}━━━ $* ━━━${NC}\n"; }

# --- Detect environment ---

OC_DIR="${HOME}/.openclaw"
OC_NPM=""
OC_BIN=""
PYTHON="python3"

detect_environment() {
    # Find OpenClaw data directory
    if [[ ! -d "$OC_DIR" ]]; then
        fail "OpenClaw data directory not found at $OC_DIR"
        echo "  Is OpenClaw installed? Try: npm install -g openclaw"
        exit 1
    fi
    ok "OpenClaw data: $OC_DIR"

    # Find OpenClaw npm install
    local npm_root
    npm_root="$(npm root -g 2>/dev/null)"
    if [[ -d "${npm_root}/openclaw" ]]; then
        OC_NPM="${npm_root}/openclaw"
    elif [[ -d "/opt/homebrew/lib/node_modules/openclaw" ]]; then
        OC_NPM="/opt/homebrew/lib/node_modules/openclaw"
    elif [[ -d "/usr/local/lib/node_modules/openclaw" ]]; then
        OC_NPM="/usr/local/lib/node_modules/openclaw"
    elif [[ -d "/usr/lib/node_modules/openclaw" ]]; then
        OC_NPM="/usr/lib/node_modules/openclaw"
    fi

    if [[ -z "$OC_NPM" || ! -d "$OC_NPM" ]]; then
        fail "Could not find OpenClaw npm installation"
        echo "  Try: npm root -g  (then look for openclaw/ inside)"
        exit 1
    fi
    ok "OpenClaw install: $OC_NPM"

    # Find openclaw binary
    OC_BIN="$(command -v openclaw 2>/dev/null || true)"
    if [[ -z "$OC_BIN" ]]; then
        # Try common locations
        for candidate in /opt/homebrew/bin/openclaw /usr/local/bin/openclaw; do
            if [[ -x "$candidate" ]]; then
                OC_BIN="$candidate"
                break
            fi
        done
    fi
    if [[ -n "$OC_BIN" ]]; then
        local version
        version="$("$OC_BIN" --version 2>/dev/null || echo 'unknown')"
        ok "OpenClaw binary: $OC_BIN ($version)"
    else
        warn "openclaw binary not found in PATH (gateway restart will need to be manual)"
    fi

    # Check python3
    if ! command -v python3 &>/dev/null; then
        fail "python3 not found. Install Python 3 first."
        exit 1
    fi
    ok "Python: $(python3 --version 2>&1)"
}

# --- Step 1: Apply the isolation patch ---

apply_patch() {
    header "Step 1/4: Applying isolation patch"

    local patcher="${REPO_DIR}/scripts/patch-openclaw-isolation.py"
    if [[ ! -f "$patcher" ]]; then
        fail "Patch script not found at $patcher"
        exit 1
    fi

    # Check if already patched
    if "$PYTHON" "$patcher" --oc-dir "$OC_NPM" --verify 2>/dev/null; then
        ok "Isolation patch already applied"
        return 0
    fi

    info "Applying isolation patch to bundle..."
    if "$PYTHON" "$patcher" --oc-dir "$OC_NPM"; then
        ok "Isolation patch applied successfully"
    else
        local exit_code=$?
        if [[ $exit_code -eq 2 ]]; then
            fail "Patch anchor strings not found — your OpenClaw version may be too new/old"
            echo "  This patch was built for OpenClaw 2026.4.x"
            echo "  Your version: $("$OC_BIN" --version 2>/dev/null || echo 'unknown')"
        else
            fail "Patch failed (exit $exit_code)"
        fi
        exit 1
    fi
}

# --- Step 2: Configure exec-approvals ---

configure_approvals() {
    header "Step 2/4: Configuring per-agent exec approvals"

    local configurator="${REPO_DIR}/scripts/configure-exec-approvals.py"
    if [[ ! -f "$configurator" ]]; then
        fail "Configure script not found at $configurator"
        exit 1
    fi

    # Pass through any --sandbox / --sandbox-all / --main flags
    info "Running exec-approvals configuration..."
    echo ""
    "$PYTHON" "$configurator" "$@"
    local exit_code=$?

    if [[ $exit_code -eq 0 ]]; then
        ok "Exec approvals configured"
    else
        fail "Configuration failed (exit $exit_code)"
        exit 1
    fi
}

# --- Step 3: Install launchd watchdog ---

install_watchdog() {
    header "Step 3/4: Installing auto-patch watchdog"

    # Only on macOS
    if [[ "$(uname)" != "Darwin" ]]; then
        warn "Skipping launchd watchdog (not macOS)"
        echo "  On Linux, you can set up a systemd path unit or inotifywait script manually."
        return 0
    fi

    local label="com.openclaw.isolation-patch"
    local plist_dir="${HOME}/Library/LaunchAgents"
    local plist_path="${plist_dir}/${label}.plist"
    local wrapper_dir="${OC_DIR}/scripts"
    local wrapper_path="${wrapper_dir}/reapply-isolation-patch.sh"
    local log_dir="${OC_DIR}/logs"

    mkdir -p "$wrapper_dir" "$log_dir" "$plist_dir"

    # Write the watchdog wrapper script
    cat > "$wrapper_path" << WATCHDOG_EOF
#!/usr/bin/env zsh
# reapply-isolation-patch.sh — Auto-reapply isolation patch after OpenClaw updates
# Installed by: setup-isolation.sh
# Triggered by: launchd WatchPaths on the OpenClaw dist/ directory

set -uo pipefail

LOGFILE="${log_dir}/isolation-patch.launchd.log"
PATCHER="${REPO_DIR}/scripts/patch-openclaw-isolation.py"
OC_NPM="${OC_NPM}"

_log() {
    echo "[\$(date '+%Y-%m-%d %H:%M:%S')] \$*" >> "\$LOGFILE"
}

_log "WatchPaths triggered — checking isolation patch status"

if [[ ! -f "\$PATCHER" ]]; then
    _log "ERROR: patcher not found at \$PATCHER — skipping"
    exit 0
fi

if [[ ! -d "\$OC_NPM" ]]; then
    _log "ERROR: OpenClaw not found at \$OC_NPM — skipping"
    exit 0
fi

if python3 "\$PATCHER" --oc-dir "\$OC_NPM" --verify >> "\$LOGFILE" 2>&1; then
    _log "Patch already present — nothing to do"
    exit 0
fi

_log "Patch missing — re-applying..."
if python3 "\$PATCHER" --oc-dir "\$OC_NPM" >> "\$LOGFILE" 2>&1; then
    _log "✅ Patch re-applied successfully"
    _log "Restarting OpenClaw gateway..."
    if ${OC_BIN:-openclaw} gateway restart >> "\$LOGFILE" 2>&1; then
        _log "✅ Gateway restart scheduled"
    else
        _log "⚠️ Gateway restart failed (may need manual restart)"
    fi
else
    patch_exit=\$?
    if [[ \$patch_exit -eq 2 ]]; then
        _log "⚠️ Patcher could not find anchor strings — OpenClaw version may have changed upstream"
    else
        _log "⚠️ Patcher failed with exit \$patch_exit"
    fi
fi
WATCHDOG_EOF
    chmod +x "$wrapper_path"
    ok "Watchdog script: $wrapper_path"

    # Write the launchd plist
    cat > "$plist_path" << PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>${label}</string>

    <key>ProgramArguments</key>
    <array>
      <string>/bin/zsh</string>
      <string>${wrapper_path}</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>WatchPaths</key>
    <array>
      <string>${OC_NPM}/package.json</string>
      <string>${OC_NPM}/dist</string>
    </array>

    <key>StandardOutPath</key>
    <string>${log_dir}/isolation-patch.launchd.log</string>

    <key>StandardErrorPath</key>
    <string>${log_dir}/isolation-patch.launchd.log</string>
  </dict>
</plist>
PLIST_EOF
    ok "LaunchAgent plist: $plist_path"

    # Unload old version if present, then load new one
    launchctl bootout "gui/$(id -u)/${label}" 2>/dev/null || true
    if launchctl bootstrap "gui/$(id -u)" "$plist_path" 2>/dev/null; then
        ok "Watchdog loaded and active"
    else
        # Try legacy load
        launchctl load -w "$plist_path" 2>/dev/null
        ok "Watchdog loaded (legacy mode)"
    fi

    info "The watchdog will automatically re-patch after OpenClaw npm updates"
}

# --- Step 4: Restart gateway ---

restart_gateway() {
    header "Step 4/4: Restarting gateway"

    if [[ -z "$OC_BIN" ]]; then
        warn "openclaw binary not found — please restart the gateway manually"
        echo "  Run: ocplatform gateway restart"
        return 0
    fi

    info "Restarting OpenClaw gateway..."
    if "$OC_BIN" gateway restart 2>/dev/null; then
        ok "Gateway restart scheduled"
        echo ""
        info "The gateway will come back up in a few seconds."
        info "Your agents will reconnect automatically."
    else
        warn "Gateway restart command failed — try manually: openclaw gateway restart"
    fi
}

# --- Main ---

main() {
    echo ""
    echo -e "${BOLD}╔══════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}║   OpenClaw Per-Agent Exec Isolation Setup     ║${NC}"
    echo -e "${BOLD}╚══════════════════════════════════════════════════╝${NC}"
    echo ""

    # Parse our flags vs configure-exec-approvals flags
    local config_args=()
    for arg in "$@"; do
        config_args+=("$arg")
    done

    detect_environment
    apply_patch
    configure_approvals "${config_args[@]}"
    install_watchdog
    restart_gateway

    header "Setup complete!"
    echo -e "  ${GREEN}•${NC} Isolation patch applied to OpenClaw bundle"
    echo -e "  ${GREEN}•${NC} Per-agent exec approvals configured"
    echo -e "  ${GREEN}•${NC} Auto-patch watchdog installed (survives npm updates)"
    echo -e "  ${GREEN}•${NC} Gateway restarting with new config"
    echo ""
    echo -e "  To verify:  ${BOLD}openclaw --version${NC}"
    echo -e "  Watchdog log: ${BOLD}${OC_DIR}/logs/isolation-patch.launchd.log${NC}"
    echo ""
}

main "$@"
