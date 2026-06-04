#!/usr/bin/env bash
# Setup Mac workspace sync: git backup (hourly) + Obsidian Headless (continuous).
#
# Mirrors the VPS sync setup for all agent workspaces on the Mac:
#   - Git sync scripts + LaunchAgents (hourly at :30, staggered from VPS at :00)
#   - Obsidian Headless vault linking + LaunchAgents (continuous sync)
#
# Agent list is read from ansible/group_vars/openclaw.yml (single source of truth).
# Workspace dir convention: <agent_id>-workspace
# Repo convention: openclaw-workspace (default) or openclaw-workspace-<id>
#
# Prerequisites:
#   - yq + jq installed (brew install yq jq)
#   - gh CLI authenticated (for repo creation)
#   - ob CLI installed (pnpm install -g @nicekiwi/obsidian-headless)
#   - SSH key configured for git@github.com (used for push)
#   - ~/.obsidian-headless/auth_token exists (ob login)
#
# Usage:
#   ./scripts/setup-mac-workspaces.sh              # Full setup
#   ./scripts/setup-mac-workspaces.sh --uninstall   # Remove all LaunchAgents + scripts
#
# Re-running is safe (idempotent). Each step checks existing state before acting.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/agents.sh"

WORKSPACES_DIR="$HOME/code/personal/workspaces"
GITIGNORE_SRC="$SCRIPT_DIR/../ansible/roles/workspace/files/gitignore-workspace"
SYNC_TEMPLATE="$SCRIPT_DIR/templates/workspace-git-sync-mac.sh.tmpl"
SYNC_BIN_DIR="$HOME/.local/bin"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
OB_BIN="$HOME/Library/pnpm/ob"
GITHUB_ORG="pandysp"
GUI_DOMAIN="gui/$(id -u)"
INITIAL_SYNC_TIMEOUT=120

# Resolve node binary path for LaunchAgents (ob wrapper needs `node` in PATH).
# asdf shims don't work in LaunchAgent context — use the direct install path.
if command -v asdf &>/dev/null; then
    NODE_BIN_DIR="$(asdf where nodejs 2>/dev/null)/bin"
elif command -v node &>/dev/null; then
    # Active node (mise/nvm/direct install) — the runtime that obsidian-headless's
    # native modules (better-sqlite3) were built against. LaunchAgents don't inherit
    # the shell PATH, so resolve the concrete bin dir now, at setup time.
    NODE_BIN_DIR="$(dirname "$(command -v node)")"
elif [ -x "/opt/homebrew/bin/node" ]; then
    NODE_BIN_DIR="/opt/homebrew/bin"
else
    NODE_BIN_DIR="/usr/local/bin"
fi
LAUNCHAGENT_PATH="${NODE_BIN_DIR}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

# Excluded folders for Obsidian Sync — must match VPS deployment
OBSIDIAN_EXCLUDED_FOLDERS=".git,.venv,.packages,.npm-packages,.bin,.cache,.local,.npm,.qmd,.scripts,.claude,.ralph,.env,.beads,.config,.dev,.dolt,.openclaw,.pi,.repos,.state,node_modules,repos,claude-code-mcp,reranker-bench,obsidian,migration"

# --- Helpers ---

log() { echo "==> $*"; }
warn() { echo "WARNING: $*" >&2; }

plist_id_git() { echo "com.openclaw.workspace-git-sync-$1"; }
plist_id_ob()  { echo "com.openclaw.obsidian-headless-$1"; }

plist_path_git() { echo "$LAUNCH_AGENTS_DIR/$(plist_id_git "$1").plist"; }
plist_path_ob()  { echo "$LAUNCH_AGENTS_DIR/$(plist_id_ob "$1").plist"; }

sync_script_path() { echo "$SYNC_BIN_DIR/workspace-git-sync-$1.sh"; }

bootout_if_loaded() {
    local plist="$1"
    if [ -f "$plist" ]; then
        launchctl bootout "$GUI_DOMAIN" "$plist" 2>/dev/null || true
    fi
}

# --- Uninstall (scan-based: finds all matching services regardless of current config) ---

uninstall() {
    log "Uninstalling Mac workspace sync..."
    for plist in "$LAUNCH_AGENTS_DIR"/com.openclaw.workspace-git-sync-*.plist \
                 "$LAUNCH_AGENTS_DIR"/com.openclaw.obsidian-headless-*.plist; do
        [ -f "$plist" ] || continue
        bootout_if_loaded "$plist"
        rm -f "$plist"
        log "  Removed: $(basename "$plist")"
    done
    for script in "$SYNC_BIN_DIR"/workspace-git-sync-*.sh; do
        [ -f "$script" ] || continue
        rm -f "$script"
        log "  Removed: $(basename "$script")"
    done
    log "Uninstall complete. Workspace directories and git repos were NOT removed."
    exit 0
}

if [ "${1:-}" = "--uninstall" ]; then
    uninstall
fi

# --- Preflight checks ---

if [ ! -d "$WORKSPACES_DIR" ]; then
    echo "ERROR: Workspaces directory not found: $WORKSPACES_DIR"
    exit 1
fi

if [ ! -f "$GITIGNORE_SRC" ]; then
    echo "ERROR: Gitignore source not found: $GITIGNORE_SRC"
    exit 1
fi

if [ ! -f "$SYNC_TEMPLATE" ]; then
    echo "ERROR: Sync script template not found: $SYNC_TEMPLATE"
    exit 1
fi

if ! command -v gh &>/dev/null; then
    echo "ERROR: gh CLI not found. Install with: brew install gh"
    exit 1
fi

if [ ! -x "$OB_BIN" ] && ! command -v ob &>/dev/null; then
    # Try PATH fallback
    OB_BIN="$(command -v ob 2>/dev/null || true)"
    if [ -z "$OB_BIN" ]; then
        echo "ERROR: ob CLI not found. Install with: pnpm install -g @nicekiwi/obsidian-headless"
        exit 1
    fi
fi

if [ ! -f "$HOME/.obsidian-headless/auth_token" ]; then
    echo "ERROR: Obsidian auth token not found. Run: ob login"
    exit 1
fi

# Verify SSH access to GitHub
if ! ssh -T git@github.com 2>&1 | grep -q "successfully authenticated"; then
    warn "SSH authentication to GitHub may not be configured. Git push may fail."
fi

mkdir -p "$SYNC_BIN_DIR"
mkdir -p "$LAUNCH_AGENTS_DIR"

# Obsidian vault password (used for linking)
# Accept via: file path OBSIDIAN_VAULT_PASSWORD_FILE, env var OBSIDIAN_VAULT_PASSWORD, or interactive prompt
if [ -n "${OBSIDIAN_VAULT_PASSWORD_FILE:-}" ] && [ -f "$OBSIDIAN_VAULT_PASSWORD_FILE" ]; then
    VAULT_PASSWORD="$(tr -d '\n' < "$OBSIDIAN_VAULT_PASSWORD_FILE")"
elif [ -n "${OBSIDIAN_VAULT_PASSWORD:-}" ]; then
    VAULT_PASSWORD="$OBSIDIAN_VAULT_PASSWORD"
else
    echo ""
    read -s -p "Obsidian vault password (E2EE): " VAULT_PASSWORD
    echo ""
    if [ -z "$VAULT_PASSWORD" ]; then
        echo "ERROR: Vault password cannot be empty"
        exit 1
    fi
fi

AGENT_IDS=($(get_agent_ids))

echo ""
echo "=========================================="
echo "  Mac Workspace Sync Setup"
echo "  Agents: ${AGENT_IDS[*]}"
echo "=========================================="
echo ""

# ============================================================
# STEP 1: Git repo setup
# ============================================================

log "Step 1: Git repository setup"

for agent_id in "${AGENT_IDS[@]}"; do
    repo_name=$(workspace_repo_name "$agent_id")
    workspace_dir="$(workspace_dir_for "$agent_id" "$WORKSPACES_DIR")"

    if [ ! -d "$workspace_dir" ]; then
        warn "Workspace directory missing: $workspace_dir — skipping"
        continue
    fi

    echo "  --- $agent_id ---"

    # Deploy .gitignore
    if ! diff -q "$GITIGNORE_SRC" "$workspace_dir/.gitignore" &>/dev/null; then
        cp "$GITIGNORE_SRC" "$workspace_dir/.gitignore"
        echo "  Deployed .gitignore"
    fi

    if [ -d "$workspace_dir/.git" ]; then
        # Existing repo — rewrite HTTPS remote to SSH
        current_remote=$(git -C "$workspace_dir" remote get-url origin 2>/dev/null || echo "")
        ssh_url="git@github.com:${GITHUB_ORG}/${repo_name}.git"

        if echo "$current_remote" | grep -q "https://"; then
            git -C "$workspace_dir" remote set-url origin "$ssh_url"
            echo "  Rewrote remote: HTTPS → SSH ($ssh_url)"
        elif [ "$current_remote" = "$ssh_url" ]; then
            echo "  Remote already SSH"
        else
            echo "  Remote: $current_remote (unchanged)"
        fi

        # Untrack files now covered by .gitignore
        (cd "$workspace_dir" && git ls-files -ci --exclude-standard -z | xargs -0 git rm --cached 2>/dev/null) || true
        if ! git -C "$workspace_dir" diff --cached --quiet 2>/dev/null; then
            git -C "$workspace_dir" commit -m "chore: untrack gitignored files"
            echo "  Untracked gitignored files"
        fi
    else
        # No local git — need to initialize
        echo "  No .git found, initializing..."

        # Create repo on GitHub if needed
        if ! gh repo view "${GITHUB_ORG}/${repo_name}" --json name &>/dev/null; then
            log "  Creating GitHub repo: ${GITHUB_ORG}/${repo_name}"
            gh repo create "${GITHUB_ORG}/${repo_name}" --private
            echo "  Created private repo"
        fi

        ssh_url="git@github.com:${GITHUB_ORG}/${repo_name}.git"

        (
            cd "$workspace_dir"
            git init -b main
            git remote add origin "$ssh_url"

            # Untrack files covered by .gitignore before first commit
            git add -A
            git commit -m "Initial commit from Mac workspace" || true

            # Try to fetch and merge remote (may have VPS content)
            if git fetch origin main 2>/dev/null; then
                if ! git merge -X ours origin/main --allow-unrelated-histories --no-edit 2>/dev/null; then
                    git merge --abort 2>/dev/null || true
                    warn "  Could not merge remote for $agent_id — will force push"
                fi
            fi

            git push -u origin main || git push --force-with-lease -u origin main
        )
        echo "  Initialized + pushed"
    fi
done

echo ""

# ============================================================
# STEP 2: Git sync scripts + LaunchAgents
# ============================================================

log "Step 2: Git sync scripts + LaunchAgents (hourly at :30)"

for agent_id in "${AGENT_IDS[@]}"; do
    workspace_dir="$(workspace_dir_for "$agent_id" "$WORKSPACES_DIR")"

    if [ ! -d "$workspace_dir/.git" ]; then
        warn "$agent_id has no .git — skipping git sync setup"
        continue
    fi

    echo "  --- $agent_id ---"

    # Install sync script from template
    script_path="$(sync_script_path "$agent_id")"
    sed \
        -e "s|__WORKSPACE_DIR__|${workspace_dir}|g" \
        -e "s|__AGENT_ID__|${agent_id}|g" \
        "$SYNC_TEMPLATE" > "$script_path"
    chmod +x "$script_path"
    echo "  Installed: $script_path"

    # Create LaunchAgent plist
    plist="$(plist_path_git "$agent_id")"
    label="$(plist_id_git "$agent_id")"
    log_dir="$HOME/Library/Logs/openclaw"
    mkdir -p "$log_dir"

    cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${script_path}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Minute</key>
        <integer>30</integer>
    </dict>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${LAUNCHAGENT_PATH}</string>
        <key>HOME</key>
        <string>${HOME}</string>
    </dict>
    <key>StandardOutPath</key>
    <string>${log_dir}/git-sync-${agent_id}.log</string>
    <key>StandardErrorPath</key>
    <string>${log_dir}/git-sync-${agent_id}.log</string>
</dict>
</plist>
EOF
    echo "  Installed: $plist"
done

echo ""

# ============================================================
# STEP 3: Obsidian Headless vault linking
# ============================================================

log "Step 3: Obsidian Headless vault linking"

for agent_id in "${AGENT_IDS[@]}"; do
    workspace_dir="$(workspace_dir_for "$agent_id" "$WORKSPACES_DIR")"
    vault_name="${agent_id}-workspace"

    if [ ! -d "$workspace_dir" ]; then
        warn "Workspace missing: $workspace_dir — skipping"
        continue
    fi

    echo "  --- $agent_id ---"

    # Check if already linked
    if "$OB_BIN" sync-status --path "$workspace_dir" &>/dev/null; then
        echo "  Already linked to Obsidian Sync"
        continue
    fi

    # Find vault ID by name
    vault_id=$("$OB_BIN" sync-list-remote 2>&1 | grep "$vault_name" | awk '{print $1}' || true)
    if [ -z "$vault_id" ]; then
        warn "Remote vault '$vault_name' not found — skipping. Create it on VPS first."
        continue
    fi

    echo "  Linking vault $vault_name (ID: $vault_id)..."

    "$OB_BIN" sync-setup \
        --vault "$vault_id" \
        --path "$workspace_dir" \
        --password "$VAULT_PASSWORD" \
        --device-name "mac-${agent_id}"

    echo "  Configuring exclusions..."
    "$OB_BIN" sync-config \
        --path "$workspace_dir" \
        --excluded-folders "$OBSIDIAN_EXCLUDED_FOLDERS"

    echo "  Running initial sync (timeout: ${INITIAL_SYNC_TIMEOUT}s)..."
    # macOS has no `timeout` command — use background + sleep + kill
    "$OB_BIN" sync --path "$workspace_dir" &
    OB_PID=$!
    (
        sleep "$INITIAL_SYNC_TIMEOUT"
        kill "$OB_PID" 2>/dev/null || true
    ) &
    TIMER_PID=$!
    wait "$OB_PID" 2>/dev/null || true
    kill "$TIMER_PID" 2>/dev/null || true
    wait "$TIMER_PID" 2>/dev/null || true
    echo "  Initial sync done (or timed out — daemon will continue)"
done

echo ""

# ============================================================
# STEP 4: Obsidian Headless LaunchAgents
# ============================================================

log "Step 4: Obsidian Headless LaunchAgents (continuous sync)"

for agent_id in "${AGENT_IDS[@]}"; do
    workspace_dir="$(workspace_dir_for "$agent_id" "$WORKSPACES_DIR")"

    if [ ! -d "$workspace_dir" ]; then
        warn "Workspace missing: $workspace_dir — skipping"
        continue
    fi

    echo "  --- $agent_id ---"

    plist="$(plist_path_ob "$agent_id")"
    label="$(plist_id_ob "$agent_id")"
    log_dir="$HOME/Library/Logs/openclaw"
    mkdir -p "$log_dir"

    cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${OB_BIN}</string>
        <string>sync</string>
        <string>--path</string>
        <string>${workspace_dir}</string>
        <string>--continuous</string>
    </array>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${LAUNCHAGENT_PATH}</string>
        <key>HOME</key>
        <string>${HOME}</string>
    </dict>
    <key>StandardOutPath</key>
    <string>${log_dir}/obsidian-headless-${agent_id}.log</string>
    <key>StandardErrorPath</key>
    <string>${log_dir}/obsidian-headless-${agent_id}.log</string>
</dict>
</plist>
EOF
    echo "  Installed: $plist"
done

echo ""

# ============================================================
# STEP 5: Load LaunchAgents
# ============================================================

log "Step 5: Loading LaunchAgents"

for agent_id in "${AGENT_IDS[@]}"; do
    # Git sync agent
    plist_git="$(plist_path_git "$agent_id")"
    if [ -f "$plist_git" ]; then
        bootout_if_loaded "$plist_git"
        launchctl bootstrap "$GUI_DOMAIN" "$plist_git"
        echo "  Loaded: $(plist_id_git "$agent_id")"
    fi

    # Obsidian headless agent
    plist_ob="$(plist_path_ob "$agent_id")"
    if [ -f "$plist_ob" ]; then
        bootout_if_loaded "$plist_ob"
        launchctl bootstrap "$GUI_DOMAIN" "$plist_ob"
        echo "  Loaded: $(plist_id_ob "$agent_id")"
    fi
done

echo ""

# ============================================================
# Summary
# ============================================================

agent_count=${#AGENT_IDS[@]}
echo "=========================================="
echo "  Setup complete! ($agent_count agents)"
echo "=========================================="
echo ""
echo "Verification:"
echo "  launchctl list | grep openclaw    # Should show $((agent_count * 2)) agents"
echo "  ob sync-status --path .../<id>-workspace   # Check vault link"
echo "  ~/.local/bin/workspace-git-sync-<id>.sh     # Manual git sync test"
echo ""
echo "Logs: ~/Library/Logs/openclaw/"
echo ""
echo "To uninstall: $0 --uninstall"
