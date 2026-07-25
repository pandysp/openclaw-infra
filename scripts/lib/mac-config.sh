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
#   2. Per-deployment POLICY — which agents get daemons, which daemons run, and
#      account order for qmd-http port bases — lives in the single `mac:` block
#      of ansible/group_vars/openclaw.yml (see openclaw.yml.example). That file
#      is gitignored, so your choices never propagate to the shared script.
#      If you are not on a Mac, that block is the only thing in the shared SoT
#      you can ignore wholesale.
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

# All Mac policy is read ONCE at source time into a single JSON blob (a per-call
# yq|jq would fork 36+ times across a deploy). One yq invocation resolves the
# nested `mac:` block and falls back to the pre-nesting flat keys, so an existing
# fork that set mac_daemons/mac_accounts keeps working untouched.
#
# The default blob (no config file) is the permissive one: no services disabled,
# no agent gate, no account order. Every read below therefore degrades to
# "everything enabled" — fail-safe: a broken, empty, or missing config can never
# silently tear the fleet down.
_MAC_POLICY_DEFAULT='{"agents":null,"daemons":{},"accounts":[],"legacy":false}'
_MAC_POLICY_JSON="$_MAC_POLICY_DEFAULT"
if [ -f "$_MAC_OPENCLAW_YML" ]; then
    # `legacy` is true only when the flat keys are what is actually supplying
    # policy (no `mac:` block present) — that is the one case worth warning about.
    #
    # ORDER IS LOAD-BEARING: `legacy` MUST come first. Traversing `.mac.agents`
    # auto-vivifies `.mac` in mikefarah yq (v4), so any later `has("mac")` or
    # `.mac == null` sees a node the traversal itself created and the flat-key
    # warning silently never fires. Verified on yq v4.53.3.
    _MAC_POLICY_JSON="$(yq -o json -I 0 '{
        "legacy":   ((has("mac") | not) and (has("mac_daemons") or has("mac_accounts"))),
        "agents":   (.mac.agents   // null),
        "daemons":  (.mac.daemons  // .mac_daemons  // {}),
        "accounts": (.mac.accounts // .mac_accounts // [])
    }' "$_MAC_OPENCLAW_YML" 2>/dev/null || echo "$_MAC_POLICY_DEFAULT")"
    [ -n "$_MAC_POLICY_JSON" ] || _MAC_POLICY_JSON="$_MAC_POLICY_DEFAULT"
fi

# Disabled-service set. Only keys explicitly set to false land here, so absent /
# null / empty ⇒ enabled. Selecting `== false` explicitly also sidesteps jq's
# `//`, which treats a literal false as empty.
declare -A _MAC_SVC_DISABLED=()
while IFS= read -r _svc; do
    [ -n "$_svc" ] && _MAC_SVC_DISABLED["$_svc"]=1
done < <(printf '%s' "$_MAC_POLICY_JSON" \
    | jq -r '.daemons | to_entries[] | select(.value == false) | .key' 2>/dev/null || true)

# Per-agent gate. `_MAC_AGENTS_CONFIGURED` flips only when at least one agent is
# listed, so both an absent `mac.agents` and an explicitly empty one mean "every
# agent" — the same fail-safe as the service gate, and deliberately unlike the
# VPS-side `obsidian_headless_agents: []` (which means "nobody"). The asymmetry
# is intentional: that key opts INTO an optional feature, whereas this one
# RESTRICTS an already-running set, so the safe default is the permissive one.
declare -A _MAC_AGENT_ALLOWED=()
_MAC_AGENTS_CONFIGURED=0
while IFS= read -r _agent; do
    [ -n "$_agent" ] || continue
    _MAC_AGENT_ALLOWED["$_agent"]=1
    _MAC_AGENTS_CONFIGURED=1
done < <(printf '%s' "$_MAC_POLICY_JSON" | jq -r '(.agents // [])[]' 2>/dev/null || true)

if [ "$(printf '%s' "$_MAC_POLICY_JSON" | jq -r '.legacy' 2>/dev/null)" = "true" ]; then
    echo "NOTE: openclaw.yml still uses the flat mac_daemons/mac_accounts keys. Nest them under a single 'mac:' block (mac.daemons / mac.accounts / mac.agents) — see openclaw.yml.example. The flat keys still work." >&2
fi

# Is a Mac daemon service enabled for deployment? Pure lookup — no subprocess.
# $1 = svc token (e.g. qmd-http).
mac_service_enabled() {
    [ -z "${_MAC_SVC_DISABLED[$1]:-}" ]
}

# Is the per-agent gate configured at all? The deployer consults this before
# reconciling gated-out agents, so an unconfigured run never removes anything.
mac_agents_configured() {
    [ "$_MAC_AGENTS_CONFIGURED" = 1 ]
}

# Does this agent get Mac daemons? Unconfigured ⇒ every agent. $1 = agent id.
mac_agent_enabled() {
    mac_agents_configured || return 0
    [ -n "${_MAC_AGENT_ALLOWED[$1]:-}" ]
}

# Agent ids listed in mac.agents, one per line — lets callers that know the real
# roster flag a typo'd entry that would otherwise gate an agent out in silence.
mac_agents_listed() {
    mac_agents_configured || return 0
    printf '%s\n' "${!_MAC_AGENT_ALLOWED[@]}"
}

# Port-base index for an account. Precedence: OPENCLAW_ACCOUNT_INDEX env >
# position in mac.accounts > 0 (with a warning). Never hard-fails, so a
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
    idx="$(printf '%s' "$_MAC_POLICY_JSON" \
        | jq -r --arg a "$account" '.accounts | index($a) // ""' 2>/dev/null || echo "")"
    if [ -n "$idx" ] && [ "$idx" != "null" ]; then
        echo "$idx"; return
    fi
    echo "WARNING: account '$account' not found in mac.accounts (openclaw.yml) and OPENCLAW_ACCOUNT_INDEX unset — using port-base index 0. Set OPENCLAW_ACCOUNT_INDEX to avoid cross-account qmd-http port collisions." >&2
    echo 0
}
