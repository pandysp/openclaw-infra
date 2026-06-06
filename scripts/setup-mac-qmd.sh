#!/usr/bin/env bash
# Setup local qmd semantic search for Mac workspaces.
#
# Mirrors the VPS qmd Ansible role: per-workspace watcher + HTTP daemon.
# Each workspace gets:
#   - .qmd/ directory with collections config
#   - fswatch-based watcher (LaunchAgent) for live index updates
#   - HTTP daemon (LaunchAgent) serving MCP endpoint
#   - Text extraction (PDFs, images, .docx, .xlsx) via .scripts/extract
#
# Agent list is read from ansible/group_vars/openclaw.yml (single source of truth).
# Port assignment is positional over all agents (matches VPS convention).
#
# Prerequisites:
#   - yq + jq installed (brew install yq jq)
#   - bun installed (brew install oven-sh/bun/bun)
#   - qmd installed (bun install -g @tobilu/qmd)
#   - fswatch installed (brew install fswatch)
#
# Usage:
#   ./scripts/setup-mac-qmd.sh                    # Full setup (all agents)
#   ./scripts/setup-mac-qmd.sh main tl             # Specific agents only
#   ./scripts/setup-mac-qmd.sh --uninstall         # Remove LaunchAgents + scripts
#   ./scripts/setup-mac-qmd.sh --status            # Show service status
#
# Re-running is safe (idempotent). Existing indexes are preserved.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/agents.sh"

WORKSPACES_DIR="$HOME/code/personal/workspaces"
WATCH_TEMPLATE="$SCRIPT_DIR/templates/qmd-watch-mac.sh.tmpl"
BIN_DIR="$HOME/.local/bin"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs/openclaw"
GUI_DOMAIN="gui/$(id -u)"

# Port assignment: positional starting at 8191 (mirrors VPS qmd_http_base_port)
QMD_HTTP_BASE_PORT=8191

# --- Helpers ---

log() { echo "==> $*"; }
warn() { echo "WARNING: $*" >&2; }

plist_id_watch() { echo "com.qmd.watch-$1"; }
plist_id_http()  { echo "com.qmd.http-$1"; }

plist_path_watch() { echo "$LAUNCH_AGENTS_DIR/$(plist_id_watch "$1").plist"; }
plist_path_http()  { echo "$LAUNCH_AGENTS_DIR/$(plist_id_http "$1").plist"; }

watch_script_path() { echo "$BIN_DIR/qmd-watch-$1.sh"; }

bootout_if_loaded() {
    local plist="$1"
    if [ -f "$plist" ]; then
        launchctl bootout "$GUI_DOMAIN" "$plist" 2>/dev/null || true
    fi
}

# Build port map (positional over ALL agents, ensures consistency with VPS)
declare -A PORT_MAP
_port=$QMD_HTTP_BASE_PORT
for _id in $(get_agent_ids); do
    PORT_MAP["$_id"]=$_port
    _port=$((_port + 1))
done

# --- Uninstall (scan-based: finds all matching services regardless of current config) ---

uninstall() {
    log "Uninstalling Mac qmd services..."
    for plist in "$LAUNCH_AGENTS_DIR"/com.qmd.watch-*.plist \
                 "$LAUNCH_AGENTS_DIR"/com.qmd.http-*.plist; do
        [ -f "$plist" ] || continue
        bootout_if_loaded "$plist"
        rm -f "$plist"
        log "  Removed: $(basename "$plist")"
    done
    for script in "$BIN_DIR"/qmd-watch-*.sh; do
        [ -f "$script" ] || continue
        rm -f "$script"
        log "  Removed: $(basename "$script")"
    done
    log "Uninstall complete. Index data (.qmd/) was NOT removed."
    exit 0
}

# --- Status ---

show_status() {
    echo ""
    echo "qmd Service Status"
    echo "==================="
    for agent_id in $(get_agent_ids); do
        local agent_port="${PORT_MAP[$agent_id]}"
        local workspace_dir="$(workspace_dir_for "$agent_id" "$WORKSPACES_DIR")"
        echo ""
        echo "--- $agent_id (port $agent_port) ---"

        # Watcher
        local watch_label
        watch_label="$(plist_id_watch "$agent_id")"
        if launchctl list "$watch_label" &>/dev/null; then
            local pid
            pid=$(launchctl list "$watch_label" 2>/dev/null | grep '"PID"' | tr -cd '0-9')
            echo "  Watcher: running${pid:+ (pid: $pid)}"
        else
            echo "  Watcher: not running"
        fi

        # HTTP daemon
        local http_label
        http_label="$(plist_id_http "$agent_id")"
        if launchctl list "$http_label" &>/dev/null; then
            if curl -sf "http://localhost:$agent_port/health" &>/dev/null; then
                echo "  HTTP:    running (http://localhost:$agent_port/mcp)"
            else
                echo "  HTTP:    loaded but not responding"
            fi
        else
            echo "  HTTP:    not running"
        fi

        # Index stats
        if [ -f "$workspace_dir/.qmd/index.sqlite" ]; then
            local size
            size=$(du -h "$workspace_dir/.qmd/index.sqlite" | cut -f1)
            echo "  Index:   $size"
            (
                export QMD_CONFIG_DIR="$workspace_dir/.qmd"
                export INDEX_PATH="$workspace_dir/.qmd/index.sqlite"
                qmd status 2>/dev/null | grep -E "(Total|Vectors|collections)" || true
            )
        else
            echo "  Index:   not created"
        fi
    done
    echo ""
    exit 0
}

# --- Parse args ---

if [ "${1:-}" = "--uninstall" ]; then
    uninstall
fi

if [ "${1:-}" = "--status" ]; then
    show_status
fi

# Resolve selected agents
ALL_AGENT_IDS=($(get_agent_ids))
SELECTED_AGENTS=()

if [ $# -gt 0 ]; then
    for arg in "$@"; do
        found=false
        for id in "${ALL_AGENT_IDS[@]}"; do
            if [ "$id" = "$arg" ]; then
                SELECTED_AGENTS+=("$arg")
                found=true
                break
            fi
        done
        if ! $found; then
            echo "ERROR: Unknown agent '$arg'. Available: ${ALL_AGENT_IDS[*]}"
            exit 1
        fi
    done
else
    SELECTED_AGENTS=("${ALL_AGENT_IDS[@]}")
fi

# --- Preflight checks ---

log "Preflight checks..."

MISSING=()
command -v bun &>/dev/null || MISSING+=("bun (brew install oven-sh/bun/bun)")
command -v qmd &>/dev/null || { export PATH="$HOME/.bun/bin:$PATH"; command -v qmd &>/dev/null || MISSING+=("qmd (bun install -g @tobilu/qmd)"); }
command -v fswatch &>/dev/null || MISSING+=("fswatch (brew install fswatch)")
command -v pdftotext &>/dev/null || MISSING+=("pdftotext (brew install poppler)")
command -v uv &>/dev/null || MISSING+=("uv (brew install uv)")

if [ ${#MISSING[@]} -gt 0 ]; then
    echo "ERROR: Missing required tools:"
    for m in "${MISSING[@]}"; do
        echo "  - $m"
    done
    exit 1
fi

# Install tesseract if missing (needed for OCR on images + scanned PDFs)
if ! command -v tesseract &>/dev/null; then
    log "Installing tesseract (OCR for images + scanned PDFs)..."
    brew install tesseract tesseract-lang
fi

if [ ! -f "$WATCH_TEMPLATE" ]; then
    echo "ERROR: Watcher template not found: $WATCH_TEMPLATE"
    exit 1
fi

mkdir -p "$BIN_DIR"
mkdir -p "$LAUNCH_AGENTS_DIR"
mkdir -p "$LOG_DIR"

echo ""
echo "=========================================="
echo "  Mac qmd Semantic Search Setup"
echo "  Agents: ${SELECTED_AGENTS[*]}"
echo "=========================================="
echo ""

# ============================================================
# STEP 1: Create .qmd directories + collections
# ============================================================

log "Step 1: Initialize qmd indexes"

for agent_id in "${SELECTED_AGENTS[@]}"; do
    workspace_dir="$(workspace_dir_for "$agent_id" "$WORKSPACES_DIR")"

    if [ ! -d "$workspace_dir" ]; then
        warn "Workspace missing: $workspace_dir — skipping"
        continue
    fi

    echo "  --- $agent_id ($workspace_dir) ---"

    # Create .qmd directory
    mkdir -p "$workspace_dir/.qmd"

    # Ensure .qmd is gitignored
    if [ -f "$workspace_dir/.gitignore" ]; then
        if ! grep -q '^\.qmd' "$workspace_dir/.gitignore"; then
            echo ".qmd/" >> "$workspace_dir/.gitignore"
            echo "  Added .qmd/ to .gitignore"
        fi
    fi

    # Create extract-cache directory
    mkdir -p "$workspace_dir/.scripts/extract-cache"

    # Use workspace's own qmd sync if available (agent-maintained, richer)
    export QMD_CONFIG_DIR="$workspace_dir/.qmd"
    export INDEX_PATH="$workspace_dir/.qmd/index.sqlite"

    if [ -x "$workspace_dir/.scripts/qmd" ]; then
        echo "  Running workspace qmd sync (agent-maintained script)..."
        "$workspace_dir/.scripts/qmd" sync || warn "qmd sync had errors for $agent_id"
    else
        echo "  No .scripts/qmd found — manual collection setup..."

        # Run extract first
        if [ -x "$workspace_dir/.scripts/extract" ]; then
            echo "  Running text extraction..."
            "$workspace_dir/.scripts/extract" sync || warn "extract sync had errors for $agent_id"
        fi

        # Check existing collections
        existing=$(qmd collection list 2>/dev/null | sed -n 's/^\([a-zA-Z_-]*\) (.*/\1/p' || true)

        # Collection: workspace (all markdown/text/csv)
        if ! echo "$existing" | grep -q '^workspace$'; then
            echo "  Creating workspace collection..."
            qmd collection add "$workspace_dir" --name workspace --mask '**/*.{md,txt,csv}'
        else
            echo "  workspace collection exists"
        fi

        # Collection: extracted-content (JSON cache from binary file extraction)
        if ! echo "$existing" | grep -q '^extracted-content$'; then
            if [ -d "$workspace_dir/.scripts/extract-cache" ]; then
                echo "  Creating extracted-content collection..."
                qmd collection add "$workspace_dir/.scripts/extract-cache" --name extracted-content --mask '*.json'
            fi
        else
            echo "  extracted-content collection exists"
        fi

        # Update index
        echo "  Updating BM25 index..."
        qmd update || warn "qmd update failed for $agent_id"

        echo "  Creating embeddings (this may take a while on first run)..."
        qmd embed || warn "qmd embed failed for $agent_id"
    fi

    unset QMD_CONFIG_DIR INDEX_PATH
done

echo ""

# ============================================================
# STEP 2: Install watcher scripts
# ============================================================

log "Step 2: Install watcher scripts"

for agent_id in "${SELECTED_AGENTS[@]}"; do
    workspace_dir="$(workspace_dir_for "$agent_id" "$WORKSPACES_DIR")"

    if [ ! -d "$workspace_dir" ]; then
        continue
    fi

    echo "  --- $agent_id ---"

    script_path="$(watch_script_path "$agent_id")"
    sed \
        -e "s|__WORKSPACE_DIR__|${workspace_dir}|g" \
        -e "s|__AGENT_ID__|${agent_id}|g" \
        "$WATCH_TEMPLATE" > "$script_path"
    chmod +x "$script_path"
    echo "  Installed: $script_path"
done

echo ""

# ============================================================
# STEP 3: Create LaunchAgent plists
# ============================================================

log "Step 3: Create LaunchAgents"

QMD_BIN="$(command -v qmd || echo "$HOME/.bun/bin/qmd")"
QMD_NODE_BIN_DIR="$(resolve_node_bin_dir)"
QMD_LAUNCHAGENT_PATH="$HOME/.bun/bin:${QMD_NODE_BIN_DIR}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

for agent_id in "${SELECTED_AGENTS[@]}"; do
    workspace_dir="$(workspace_dir_for "$agent_id" "$WORKSPACES_DIR")"
    agent_port="${PORT_MAP[$agent_id]}"

    if [ ! -d "$workspace_dir" ]; then
        continue
    fi

    echo "  --- $agent_id (port $agent_port) ---"

    # Watcher LaunchAgent
    plist_watch="$(plist_path_watch "$agent_id")"
    label_watch="$(plist_id_watch "$agent_id")"
    script_path="$(watch_script_path "$agent_id")"

    cat > "$plist_watch" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${label_watch}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${script_path}</string>
    </array>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${QMD_LAUNCHAGENT_PATH}</string>
        <key>HOME</key>
        <string>${HOME}</string>
    </dict>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/qmd-watch-${agent_id}.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/qmd-watch-${agent_id}.log</string>
    <key>ThrottleInterval</key>
    <integer>30</integer>
</dict>
</plist>
EOF
    echo "  Installed: $plist_watch"

    # HTTP daemon LaunchAgent
    plist_http="$(plist_path_http "$agent_id")"
    label_http="$(plist_id_http "$agent_id")"

    cat > "$plist_http" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${label_http}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${QMD_BIN}</string>
        <string>mcp</string>
        <string>--http</string>
        <string>--port</string>
        <string>${agent_port}</string>
    </array>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${QMD_LAUNCHAGENT_PATH}</string>
        <key>HOME</key>
        <string>${HOME}</string>
        <key>QMD_CONFIG_DIR</key>
        <string>${workspace_dir}/.qmd</string>
        <key>INDEX_PATH</key>
        <string>${workspace_dir}/.qmd/index.sqlite</string>
    </dict>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/qmd-http-${agent_id}.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/qmd-http-${agent_id}.log</string>
    <key>ThrottleInterval</key>
    <integer>30</integer>
</dict>
</plist>
EOF
    echo "  Installed: $plist_http"
done

echo ""

# ============================================================
# STEP 4: Load LaunchAgents
# ============================================================

log "Step 4: Loading LaunchAgents"

for agent_id in "${SELECTED_AGENTS[@]}"; do
    workspace_dir="$(workspace_dir_for "$agent_id" "$WORKSPACES_DIR")"

    if [ ! -d "$workspace_dir" ]; then
        continue
    fi

    # Watcher
    plist_watch="$(plist_path_watch "$agent_id")"
    if [ -f "$plist_watch" ]; then
        bootout_if_loaded "$plist_watch"
        launchctl bootstrap "$GUI_DOMAIN" "$plist_watch"
        echo "  Loaded: $(plist_id_watch "$agent_id")"
    fi

    # HTTP daemon
    plist_http="$(plist_path_http "$agent_id")"
    if [ -f "$plist_http" ]; then
        bootout_if_loaded "$plist_http"
        launchctl bootstrap "$GUI_DOMAIN" "$plist_http"
        echo "  Loaded: $(plist_id_http "$agent_id")"
    fi
done

echo ""

# ============================================================
# STEP 5: Verify
# ============================================================

log "Step 5: Verify"

sleep 2  # give daemons a moment to start

ALL_OK=true
for agent_id in "${SELECTED_AGENTS[@]}"; do
    agent_port="${PORT_MAP[$agent_id]}"

    echo "  --- $agent_id ---"

    # Check watcher
    if launchctl list "$(plist_id_watch "$agent_id")" &>/dev/null; then
        echo "  Watcher: OK"
    else
        warn "  Watcher: FAILED to start"
        ALL_OK=false
    fi

    # Check HTTP daemon
    if curl -sf "http://localhost:$agent_port/health" &>/dev/null; then
        echo "  HTTP:    OK (http://localhost:$agent_port/mcp)"
    else
        echo "  HTTP:    starting... (may take a few seconds for first model load)"
    fi
done

echo ""
echo "=========================================="
echo "  Setup complete!"
echo "=========================================="
echo ""
echo "Ports:"
for agent_id in "${SELECTED_AGENTS[@]}"; do
    echo "  ${agent_id}: http://localhost:${PORT_MAP[$agent_id]}/mcp"
done
echo ""
echo "Status:   $0 --status"
echo "Logs:     $LOG_DIR/qmd-{watch,http}-*.log"
echo "Uninstall: $0 --uninstall"
echo ""
if ! $ALL_OK; then
    echo "Some services failed to start. Check logs for details."
    exit 1
fi
