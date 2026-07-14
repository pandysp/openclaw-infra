#!/usr/bin/env bash
# Shared configuration for the Mac deployment scripts:
#   deploy-mac-daemons.sh, setup-mac-qmd.sh, setup-mac-workspaces.sh
#
# FORKERS — this is your tuning surface. Two places to configure:
#
#   1. Environment paths / toolchain (below): every value is an env-overridable
#      default (`: "${VAR:=...}"`). Override without editing —
#        WORKSPACES_DIR=~/code/agents ./scripts/deploy-mac-daemons.sh
#      — or change the defaults here. Defaults match the upstream author's macOS
#      fleet, so an unconfigured run reproduces that setup out of the box.
#
#   2. Per-deployment POLICY — which daemons run, and account order for qmd-http
#      port bases — lives in ansible/group_vars/openclaw.yml (mac_daemons /
#      mac_accounts; see openclaw.yml.example). That file is gitignored, so your
#      choices never propagate to the shared script.
#
# Sourced, not executed. The openclaw.yml readers require yq + jq (already
# required by lib/agents.sh, which every caller also sources).

# --- Environment tunables (defaults = upstream author's macOS setup) ---------

# Non-default agent workspaces live at <WORKSPACES_DIR>/<id>-workspace. The
# default agent uses $HOME/main-workspace (see lib/agents.sh workspace_dir_for).
: "${WORKSPACES_DIR:=$HOME/dev/personal/workspaces}"

# Per-agent helper scripts (workspace-git-sync-<agent>.sh, qmd-watch-<agent>.sh).
: "${BIN_DIR:=$HOME/.local/bin}"

# Daemon stdout/stderr logs.
: "${LOG_DIR:=$HOME/Library/Logs/openclaw}"

# obsidian-headless's native better-sqlite3 is ABI-locked to a Node major (the
# 12.6.2 build has no Node-26 prebuild and won't compile on 26), so ob must run
# under a pinned Node regardless of the default. OB_NODE_BIN derives from the
# version via the mise install path; set OB_NODE_BIN directly if you don't use mise.
: "${OB_NODE_VERSION:=23.11.0}"
: "${OB_NODE_BIN:=$HOME/.local/share/mise/installs/node/${OB_NODE_VERSION}/bin}"

# GitHub org owning the openclaw-workspace[-<id>] repos (setup-mac-workspaces.sh).
: "${GITHUB_ORG:=pandysp}"

# qmd binary (bun global install by default).
: "${QMD_BIN:=$HOME/.bun/bin/qmd}"

# qmd-http port base. Effective port = QMD_HTTP_BASE_PORT + accountIndex*100 +
# agentPosition. Only consulted when qmd-http is enabled.
: "${QMD_HTTP_BASE_PORT:=8191}"

# --- Shared helpers ----------------------------------------------------------

# Resolve the real ob binary — never a symlink, which breaks its native-module
# resolution. Tries the spec path, the validated path, then PATH. Echoes the
# path (returns 0), or returns 1 if none found.
resolve_ob_bin() {
    local cand
    for cand in "$HOME/Library/pnpm/bin/ob" "$HOME/Library/pnpm/ob"; do
        [ -x "$cand" ] && { echo "$cand"; return 0; }
    done
    cand="$(command -v ob 2>/dev/null || true)"
    [ -n "$cand" ] && { echo "$cand"; return 0; }
    return 1
}

# --- openclaw.yml policy readers ---------------------------------------------
# Resolve the SoT independently of lib/agents.sh so sourcing order never matters.
_MAC_CFG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_MAC_OPENCLAW_YML="$_MAC_CFG_DIR/../../ansible/group_vars/openclaw.yml"

# Disabled-service set, parsed ONCE at source time (a per-call yq|jq would fork
# 36+ times across a deploy). Only keys explicitly set to false land here, so
# absent key / null / empty / missing file ⇒ enabled — fail-safe: a broken or
# empty config can never silently tear the fleet down. Selecting `== false`
# explicitly also sidesteps jq's `//`, which treats a literal false as empty.
declare -A _MAC_SVC_DISABLED=()
if [ -f "$_MAC_OPENCLAW_YML" ]; then
    while IFS= read -r _svc; do
        [ -n "$_svc" ] && _MAC_SVC_DISABLED["$_svc"]=1
    done < <(yq -o json '.mac_daemons // {}' "$_MAC_OPENCLAW_YML" 2>/dev/null \
        | jq -r 'to_entries[] | select(.value == false) | .key' 2>/dev/null || true)
fi

# Is a Mac daemon service enabled for deployment? Pure lookup — no subprocess.
# $1 = svc token (e.g. qmd-http).
mac_service_enabled() {
    [ -z "${_MAC_SVC_DISABLED[$1]:-}" ]
}

# Port-base index for an account. Precedence: OPENCLAW_ACCOUNT_INDEX env >
# position in mac_accounts > 0 (with a warning). Never hard-fails, so a
# single-account fork works with no config. $1 = account name.
mac_account_index() {
    local account="$1" idx
    if [ -n "${OPENCLAW_ACCOUNT_INDEX:-}" ]; then
        case "$OPENCLAW_ACCOUNT_INDEX" in
            ''|*[!0-9]*)
                echo "WARNING: OPENCLAW_ACCOUNT_INDEX is not a non-negative integer ('$OPENCLAW_ACCOUNT_INDEX') — using 0" >&2
                echo 0; return ;;
            *) echo "$OPENCLAW_ACCOUNT_INDEX"; return ;;
        esac
    fi
    if [ -f "$_MAC_OPENCLAW_YML" ]; then
        idx="$(yq -o json '.mac_accounts // []' "$_MAC_OPENCLAW_YML" 2>/dev/null \
            | jq -r --arg a "$account" 'index($a) // ""' 2>/dev/null || echo "")"
        if [ -n "$idx" ] && [ "$idx" != "null" ]; then
            echo "$idx"; return
        fi
    fi
    echo "WARNING: account '$account' not found in mac_accounts (openclaw.yml) and OPENCLAW_ACCOUNT_INDEX unset — using port-base index 0. Set OPENCLAW_ACCOUNT_INDEX to avoid cross-account qmd-http port collisions." >&2
    echo 0
}
