#!/usr/bin/env bash
# Prepare Mac agent workspaces for sync. PREP ONLY — installs no services.
#
# Per selected agent, this does the one-time setup the daemons depend on:
#   - Git repo setup (.gitignore, SSH remote, create + push if absent)
#   - Install ~/.local/bin/workspace-git-sync-<agent>.sh (the helper the daemon runs)
#   - Obsidian Headless vault linking (+ exclusions + initial sync)
#
# The four per-agent daemons (git-sync, obsidian-headless, qmd-watch, qmd-http)
# are deployed separately by deploy-mac-daemons.sh as login-independent system
# LaunchDaemons. Splitting prep from the service layer lets that one deployer own
# launchctl for every account, so running this for prep can never double-run a daemon.
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
#   ./scripts/setup-mac-workspaces.sh                 # prep all agents
#   ./scripts/setup-mac-workspaces.sh main tl          # prep specific agents
#   ./scripts/setup-mac-workspaces.sh --uninstall      # remove git-sync helper scripts
#
# Re-running is safe (idempotent). Each step checks existing state before acting.
# Deploy/teardown of the daemons themselves: deploy-mac-daemons.sh.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/agents.sh"

WORKSPACES_DIR="$HOME/dev/personal/workspaces"
GITIGNORE_SRC="$SCRIPT_DIR/../ansible/roles/workspace/files/gitignore-workspace"
SYNC_TEMPLATE="$SCRIPT_DIR/templates/workspace-git-sync-mac.sh.tmpl"
SYNC_BIN_DIR="$HOME/.local/bin"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
OB_BIN="$HOME/Library/pnpm/ob"
# ob's native better-sqlite3 (12.6.2) is ABI-locked to Node 23.11.0; pin it so ob
# works regardless of the default node (which may be 26). Mirrors deploy-mac-daemons.sh.
OB_NODE_BIN="$HOME/.local/share/mise/installs/node/23.11.0/bin"
GITHUB_ORG="pandysp"
GUI_DOMAIN="gui/$(id -u)"
INITIAL_SYNC_TIMEOUT=120

# Excluded folders for Obsidian Sync — must match VPS deployment
OBSIDIAN_EXCLUDED_FOLDERS=".git,.venv,.packages,.npm-packages,.bin,.cache,.local,.npm,.qmd,.scripts,.claude,.ralph,.env,.beads,.config,.dev,.dolt,.openclaw,.pi,.repos,.state,node_modules,repos,claude-code-mcp,reranker-bench,obsidian,migration"

# Obsidian config categories to sync — must match VPS (obsidian_headless_configs).
# community-plugin[-data] is what carries community plugins to mobile. Empty disables.
OBSIDIAN_CONFIGS="community-plugin,community-plugin-data"

# --- Helpers ---

log() { echo "==> $*"; }
warn() { echo "WARNING: $*" >&2; }

# Run ob under the pinned Node — its native better-sqlite3 won't load otherwise.
ob_run() {
    if [ -d "$OB_NODE_BIN" ]; then
        PATH="$OB_NODE_BIN:$PATH" "$OB_BIN" "$@"
    else
        "$OB_BIN" "$@"
    fi
}

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

# Resolve selected agents (positional args filter the full list; none = all).
ALL_AGENT_IDS=($(get_agent_ids))
AGENT_IDS=()
if [ $# -gt 0 ]; then
    for arg in "$@"; do
        found=false
        for id in "${ALL_AGENT_IDS[@]}"; do
            [ "$id" = "$arg" ] && { AGENT_IDS+=("$arg"); found=true; break; }
        done
        $found || { echo "ERROR: Unknown agent '$arg'. Available: ${ALL_AGENT_IDS[*]}"; exit 1; }
    done
else
    AGENT_IDS=("${ALL_AGENT_IDS[@]}")
fi

echo ""
echo "=========================================="
echo "  Mac Workspace Prep (no services)"
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
# STEP 2: Git sync helper scripts
# ============================================================

log "Step 2: Git sync helper scripts"

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

    # First-time link only: link the vault to its remote.
    linked_now=false
    if ob_run sync-status --path "$workspace_dir" &>/dev/null; then
        echo "  Already linked to Obsidian Sync"
    else
        # Find vault ID by name
        vault_id=$(ob_run sync-list-remote 2>&1 | grep "$vault_name" | awk '{print $1}' || true)
        if [ -z "$vault_id" ]; then
            warn "Remote vault '$vault_name' not found — skipping. Create it on VPS first."
            continue
        fi
        echo "  Linking vault $vault_name (ID: $vault_id)..."
        ob_run sync-setup \
            --vault "$vault_id" \
            --path "$workspace_dir" \
            --password "$VAULT_PASSWORD" \
            --device-name "mac-${agent_id}"
        linked_now=true
    fi

    # Every run: enforce sync config so config changes (e.g. enabling
    # community-plugin sync) reach already-linked vaults too — mirrors the VPS
    # obsidian-headless role. This script is prep-only; the daemon re-reads the
    # config on its next restart, which deploy-mac-daemons.sh always performs.
    echo "  Configuring sync (exclusions + config categories)..."
    ob_run sync-config \
        --path "$workspace_dir" \
        --excluded-folders "$OBSIDIAN_EXCLUDED_FOLDERS" \
        --configs "$OBSIDIAN_CONFIGS"

    # First-time link only: a blocking initial sync so the vault is populated
    # before the daemon starts.
    if [ "$linked_now" = true ]; then
        echo "  Running initial sync (timeout: ${INITIAL_SYNC_TIMEOUT}s)..."
        # macOS has no `timeout` command — use background + sleep + kill
        ob_run sync --path "$workspace_dir" &
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
    fi
done

echo ""

# ============================================================
# Summary
# ============================================================

agent_count=${#AGENT_IDS[@]}
echo "=========================================="
echo "  Prep complete! ($agent_count agents)"
echo "=========================================="
echo ""
echo "This installed no services. Deploy the daemons with:"
echo "  ./scripts/deploy-mac-daemons.sh ${AGENT_IDS[*]}"
echo ""
echo "Verification:"
echo "  ob sync-status --path <workspace>           # Check vault link"
echo "  ~/.local/bin/workspace-git-sync-<id>.sh     # Manual git sync test"
echo ""
echo "Logs: ~/Library/Logs/openclaw/"
echo ""
echo "To remove git-sync helper scripts: $0 --uninstall"
