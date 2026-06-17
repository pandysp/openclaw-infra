#!/usr/bin/env bash
# Build local qmd semantic-search indexes for Mac workspaces. PREP ONLY —
# installs no services.
#
# Per selected agent, this does the one-time setup the qmd daemons depend on:
#   - .qmd/ directory with collections config + initial index/embeddings
#   - Text extraction (PDFs, images, .docx, .xlsx) via .scripts/extract
#   - Install ~/.local/bin/qmd-watch-<agent>.sh (the helper the watch daemon runs)
#
# The qmd-watch + qmd-http daemons are deployed separately by
# deploy-mac-daemons.sh as login-independent system LaunchDaemons (it also owns
# the per-account HTTP port assignment). Splitting prep from the service layer
# means running this for prep can never double-run a daemon on one sqlite index.
#
# Agent list is read from ansible/group_vars/openclaw.yml (single source of truth).
#
# Prerequisites:
#   - yq + jq installed (brew install yq jq)
#   - bun installed (brew install oven-sh/bun/bun)
#   - qmd installed (bun install -g @tobilu/qmd)
#   - fswatch installed (brew install fswatch)
#
# Usage:
#   ./scripts/setup-mac-qmd.sh                    # prep all agents
#   ./scripts/setup-mac-qmd.sh main tl             # prep specific agents
#   ./scripts/setup-mac-qmd.sh --uninstall         # remove qmd-watch helper scripts
#   ./scripts/setup-mac-qmd.sh --status            # show index status
#
# Re-running is safe (idempotent). Existing indexes are preserved.
# Deploy/teardown of the daemons themselves: deploy-mac-daemons.sh.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/agents.sh"

WORKSPACES_DIR="$HOME/dev/personal/workspaces"
WATCH_TEMPLATE="$SCRIPT_DIR/templates/qmd-watch-mac.sh.tmpl"
BIN_DIR="$HOME/.local/bin"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs/openclaw"
GUI_DOMAIN="gui/$(id -u)"

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
    echo "qmd Index Status (prep artifacts only)"
    echo "Daemon status lives in: deploy-mac-daemons.sh --status"
    echo "======================================================="
    for agent_id in $(get_agent_ids); do
        local workspace_dir="$(workspace_dir_for "$agent_id" "$WORKSPACES_DIR")"
        echo ""
        echo "--- $agent_id ---"

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

        # Watch helper script
        if [ -f "$(watch_script_path "$agent_id")" ]; then
            echo "  Watcher helper: installed"
        else
            echo "  Watcher helper: not installed"
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
# Summary
# ============================================================

echo "=========================================="
echo "  qmd prep complete! (${#SELECTED_AGENTS[@]} agents)"
echo "=========================================="
echo ""
echo "This installed no services. Deploy the qmd daemons (and their per-account"
echo "HTTP ports) with:"
echo "  ./scripts/deploy-mac-daemons.sh ${SELECTED_AGENTS[*]}"
echo ""
echo "Status:    $0 --status                       # index status (this script)"
echo "           ./scripts/deploy-mac-daemons.sh --status   # daemon status"
echo "Logs:      $LOG_DIR/qmd-{watch,http}-*.log"
echo "Uninstall: $0 --uninstall"
