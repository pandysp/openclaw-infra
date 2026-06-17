#!/usr/bin/env bash
# Deploy openclaw workspace services as per-account *system* LaunchDaemons.
#
# Why daemons, not LaunchAgents: a Mac Studio is shared by two accounts
# (spannagel, tl) that are usually NOT logged into the GUI. Per-user
# LaunchAgents only run while their owner has an active GUI (Aqua) session, so
# the workspace services die whenever the account is logged out. System
# LaunchDaemons in /Library/LaunchDaemons run login-independent; each carries a
# <UserName> key so launchd still runs the program AS the target account.
#
# Four services per agent workspace (mirrors the two LaunchAgent generators
# this replaces — setup-mac-workspaces.sh + setup-mac-qmd.sh):
#   git-sync         hourly git backup (StartCalendarInterval :30)
#   obsidian-headless continuous Obsidian Sync (KeepAlive)
#   qmd-watch        fswatch-driven qmd reindex (KeepAlive)
#   qmd-http         qmd MCP HTTP endpoint (KeepAlive)
#
# Everything is namespaced per account so two accounts on one Mac never collide:
#   - daemon label:   com.openclaw.<svc>.<account>.<agent>
#   - qmd-http port:   8191 + accountIndex*100 + agentPositionInFullList
#   - qmd-watch lock:  /tmp/qmd-watch-<account>-<agent>.lock
#   (the qmd embed lock stays machine-global: /tmp/qmd-embed-global.lock, so two
#    accounts never load the 314MB embedding model at the same time.)
#
# This script does NOT do workspace prep (git init, vault linking, index build).
# Those one-time steps still live in the two LaunchAgent generators and must
# have been run first. This script only (re)deploys the daemons and tears down
# the superseded per-user LaunchAgents so they can't double-run on a future GUI
# login.
#
# Reuses the per-agent helper scripts the existing generators install in
# ~/.local/bin (workspace-git-sync-<agent>.sh, qmd-watch-<agent>.sh) and reads
# the agent list + path helpers from lib/agents.sh (the openclaw.yml SoT).
#
# Run AS the target account (sudo is used only for the /Library writes and the
# `launchctl ... system` calls). Idempotent and re-runnable.
#
# Usage:
#   ./scripts/deploy-mac-daemons.sh                 # all agents, current account
#   ./scripts/deploy-mac-daemons.sh main tl          # only these agents
#   ./scripts/deploy-mac-daemons.sh --status         # show daemon status
#   ./scripts/deploy-mac-daemons.sh --uninstall      # remove this account's daemons
#
# Env:
#   OPENCLAW_ACCOUNT_INDEX  override the account->port-base index (0/1/2/...)

set -euo pipefail

if [ "${BASH_VERSINFO[0]:-0}" -lt 4 ]; then
    echo "ERROR: requires bash 4+ (got ${BASH_VERSION:-unknown}); invoke with Homebrew bash, not /bin/bash." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=scripts/lib/agents.sh
source "$SCRIPT_DIR/lib/agents.sh"

# --- Account + identity ------------------------------------------------------

ACCOUNT="$(id -un)"
ACCOUNT_UID="$(id -u)"
HOME_DIR="$HOME"

WORKSPACES_DIR="$HOME_DIR/dev/personal/workspaces"
BIN_DIR="$HOME_DIR/.local/bin"
LOG_DIR="$HOME_DIR/Library/Logs/openclaw"
USER_LAUNCH_AGENTS_DIR="$HOME_DIR/Library/LaunchAgents"
SYSTEM_DAEMONS_DIR="/Library/LaunchDaemons"
GUI_DOMAIN="gui/$ACCOUNT_UID"

# qmd HTTP port base; +100 per account, +1 per agent (positional, full list).
QMD_HTTP_BASE_PORT=8191

log()  { echo "==> $*"; }
warn() { echo "WARNING: $*" >&2; }
die()  { echo "ERROR: $*" >&2; exit 1; }

# Authenticate sudo once, non-interactively when possible. Over SSH there is no
# tty for a password prompt, so set SUDO_ASKPASS to a helper that prints the
# password and this uses `sudo -A`. Falls back to an interactive prompt when a
# real terminal is present. Once primed, later plain `sudo` calls use the cache.
sudo_prime() {
    sudo -n -v 2>/dev/null && return 0
    [ -n "${SUDO_ASKPASS:-}" ] && sudo -A -v 2>/dev/null && return 0
    sudo -v 2>/dev/null && return 0
    die "sudo is required to manage ${SYSTEM_DAEMONS_DIR} and the system launchd domain. Pre-authenticate sudo, or set SUDO_ASKPASS=<helper> for non-interactive (SSH) use."
}

# --- Account index -> port-base offset ---------------------------------------
# OPENCLAW_ACCOUNT_INDEX wins; else map known accounts; else hard-fail. We never
# default an unknown account to 0 — that would silently collide port bases with
# spannagel across machines.
resolve_account_index() {
    if [ -n "${OPENCLAW_ACCOUNT_INDEX:-}" ]; then
        case "$OPENCLAW_ACCOUNT_INDEX" in
            ''|*[!0-9]*) die "OPENCLAW_ACCOUNT_INDEX must be a non-negative integer (got: '$OPENCLAW_ACCOUNT_INDEX')" ;;
        esac
        echo "$OPENCLAW_ACCOUNT_INDEX"
        return
    fi
    case "$ACCOUNT" in
        spannagel)        echo 0 ;;
        tl)               echo 1 ;;
        andreasspannagel) echo 2 ;;
        *) die "Unknown account '$ACCOUNT' — no port-base index mapping. Set OPENCLAW_ACCOUNT_INDEX=<n> explicitly." ;;
    esac
}
ACCOUNT_INDEX="$(resolve_account_index)"

# --- Daemon labels (new, per-account) ----------------------------------------
# com.openclaw.<svc>.<account>.<agent>
daemon_label()  { echo "com.openclaw.$1.${ACCOUNT}.$2"; }   # $1=svc $2=agent
daemon_plist()  { echo "$SYSTEM_DAEMONS_DIR/$(daemon_label "$1" "$2").plist"; }

# Reused helper scripts installed by the existing generators in ~/.local/bin.
git_sync_script()  { echo "$BIN_DIR/workspace-git-sync-$1.sh"; }
qmd_watch_script() { echo "$BIN_DIR/qmd-watch-$1.sh"; }

# Old per-user LaunchAgent plists this deployment supersedes. Hardcoded — their
# label form is unrelated to the new daemon scheme, so they cannot be derived.
old_launchagent_plists() {
    local agent="$1"
    echo "$USER_LAUNCH_AGENTS_DIR/com.openclaw.workspace-git-sync-${agent}.plist"
    echo "$USER_LAUNCH_AGENTS_DIR/com.openclaw.obsidian-headless-${agent}.plist"
    echo "$USER_LAUNCH_AGENTS_DIR/com.qmd.watch-${agent}.plist"
    echo "$USER_LAUNCH_AGENTS_DIR/com.qmd.http-${agent}.plist"
}

# --- Per-service PATHs (three distinct ones — do NOT collapse) ----------------
# git-sync: no node needed (the sync script only shells out to git).
GIT_SYNC_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
# obsidian-headless: HARD-PIN Node 23.11.0. ob's better-sqlite3 12.6.2 has no
# Node-26 prebuilt and won't compile on Node 26; resolve_node_bin_dir() would
# hand back the default Node 26 here, so we must construct the path explicitly.
OBSIDIAN_NODE_BIN="$HOME_DIR/.local/share/mise/installs/node/23.11.0/bin"
OBSIDIAN_PATH="${OBSIDIAN_NODE_BIN}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
# qmd-watch + qmd-http: bun first, then mise shims (default Node 26 is fine for
# qmd — its better-sqlite3 12.10.0 has a Node-26 prebuilt).
QMD_PATH="$HOME_DIR/.bun/bin:$HOME_DIR/.local/share/mise/shims:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

# --- Programs ----------------------------------------------------------------
# OB_BIN: the daemon spec says ~/Library/pnpm/bin/ob, but the validated existing
# setup uses ~/Library/pnpm/ob (no /bin/) — and on this machine only the latter
# exists. Try the spec path, fall back to the validated path, then PATH; use the
# first executable. (Discrepancy surfaced in the deploy summary.)
resolve_ob_bin() {
    local cand
    for cand in "$HOME_DIR/Library/pnpm/bin/ob" "$HOME_DIR/Library/pnpm/ob"; do
        [ -x "$cand" ] && { echo "$cand"; return 0; }
    done
    cand="$(command -v ob 2>/dev/null || true)"
    [ -n "$cand" ] && { echo "$cand"; return 0; }
    return 1
}
QMD_BIN="$HOME_DIR/.bun/bin/qmd"   # PATH already leads with .bun/bin; pin per spec.

# --- Port map: positional over the FULL agent list, then we look up selected --
# Computing the index within the selected subset would mis-assign ports on a
# scoped run, so the map is always built over every agent in get_agent_ids order.
declare -A PORT_MAP
_port_idx=0
for _id in $(get_agent_ids); do
    PORT_MAP["$_id"]=$((QMD_HTTP_BASE_PORT + ACCOUNT_INDEX * 100 + _port_idx))
    _port_idx=$((_port_idx + 1))
done

# =============================================================================
# Plist emitters — each writes a complete plist to stdout.
# All four carry <UserName>$ACCOUNT</UserName> (the crux of a system daemon
# running as the user) and absolute ProgramArguments.
# =============================================================================

emit_git_sync_plist() {
    local agent="$1" label script
    label="$(daemon_label git-sync "$agent")"
    script="$(git_sync_script "$agent")"
    cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${label}</string>
    <key>UserName</key>
    <string>${ACCOUNT}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${script}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Minute</key>
        <integer>30</integer>
    </dict>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${GIT_SYNC_PATH}</string>
        <key>HOME</key>
        <string>${HOME_DIR}</string>
    </dict>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/git-sync-${agent}.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/git-sync-${agent}.log</string>
</dict>
</plist>
EOF
}

emit_obsidian_plist() {
    local agent="$1" ob_bin="$2" workspace="$3" label
    label="$(daemon_label obsidian-headless "$agent")"
    cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${label}</string>
    <key>UserName</key>
    <string>${ACCOUNT}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${ob_bin}</string>
        <string>sync</string>
        <string>--path</string>
        <string>${workspace}</string>
        <string>--continuous</string>
    </array>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${OBSIDIAN_PATH}</string>
        <key>HOME</key>
        <string>${HOME_DIR}</string>
    </dict>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/obsidian-headless-${agent}.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/obsidian-headless-${agent}.log</string>
</dict>
</plist>
EOF
}

emit_qmd_watch_plist() {
    local agent="$1" script="$2" label
    label="$(daemon_label qmd-watch "$agent")"
    # No QMD_CONFIG_DIR/INDEX_PATH here — the watch script self-exports them.
    cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${label}</string>
    <key>UserName</key>
    <string>${ACCOUNT}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${script}</string>
    </array>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${QMD_PATH}</string>
        <key>HOME</key>
        <string>${HOME_DIR}</string>
    </dict>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/qmd-watch-${agent}.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/qmd-watch-${agent}.log</string>
</dict>
</plist>
EOF
}

emit_qmd_http_plist() {
    local agent="$1" port="$2" workspace="$3" label
    label="$(daemon_label qmd-http "$agent")"
    cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${label}</string>
    <key>UserName</key>
    <string>${ACCOUNT}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${QMD_BIN}</string>
        <string>mcp</string>
        <string>--http</string>
        <string>--port</string>
        <string>${port}</string>
    </array>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${QMD_PATH}</string>
        <key>HOME</key>
        <string>${HOME_DIR}</string>
        <key>QMD_CONFIG_DIR</key>
        <string>${workspace}/.qmd</string>
        <key>INDEX_PATH</key>
        <string>${workspace}/.qmd/index.sqlite</string>
    </dict>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/qmd-http-${agent}.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/qmd-http-${agent}.log</string>
</dict>
</plist>
EOF
}

# =============================================================================
# Install / load a single daemon from a temp plist (idempotent).
#   $1 = label   $2 = path to the generated temp plist
# install replaces the /Library file atomically; bootout-then-bootstrap reloads.
# bootout is best-effort (the daemon may not be loaded yet).
# =============================================================================
install_daemon() {
    local label="$1" tmp="$2" dest
    dest="$SYSTEM_DAEMONS_DIR/${label}.plist"
    sudo install -o root -g wheel -m 644 "$tmp" "$dest"
    sudo launchctl bootout "system/${label}" 2>/dev/null || true
    # bootout is asynchronous; an immediate bootstrap can hit
    # "Bootstrap failed: 5: Input/output error" on the teardown race. Retry a
    # few times with a short settle, then warn (don't abort the whole run).
    for _ in 1 2 3 4 5; do
        if sudo launchctl bootstrap system "$dest"; then
            echo "  loaded: ${label}"
            return 0
        fi
        sleep 1
    done
    warn "bootstrap failed after retries: ${label} (plist installed; will load at next boot)"
    return 0
}

# Best-effort teardown of one old LaunchAgent: bootout from the GUI domain (which
# may not exist over SSH — hence 2>/dev/null), then rm. The rm is load-bearing:
# it stops the LaunchAgent from double-running on the next GUI login.
teardown_old_launchagent() {
    local plist="$1"
    [ -f "$plist" ] || return 0
    launchctl bootout "$GUI_DOMAIN" "$plist" 2>/dev/null || true
    rm -f "$plist"
    echo "  removed old LaunchAgent: $(basename "$plist")"
}

# Idempotently rewrite the qmd-watch agent lock to the per-account form. Anchored
# on '^AGENT_LOCK=' so re-runs are stable (matching the literal old value would
# double-namespace: spannagel-spannagel-<agent> on the second pass).
namespace_qmd_watch_lock() {
    local script="$1" agent="$2"
    sed -i '' -E "s|^AGENT_LOCK=.*|AGENT_LOCK=\"/tmp/qmd-watch-${ACCOUNT}-${agent}.lock\"|" "$script"
}

# =============================================================================
# --status
# =============================================================================
show_status() {
    sudo_prime
    echo ""
    echo "openclaw daemon status — account '${ACCOUNT}' (index ${ACCOUNT_INDEX})"
    echo "==================================================================="
    local agent svc label workspace port
    for agent in $(get_agent_ids); do
        workspace="$(workspace_dir_for "$agent" "$WORKSPACES_DIR")"
        port="${PORT_MAP[$agent]}"
        echo ""
        echo "--- ${agent} (qmd-http port ${port}) ---"
        for svc in git-sync obsidian-headless qmd-watch qmd-http; do
            label="$(daemon_label "$svc" "$agent")"
            if sudo launchctl print "system/${label}" &>/dev/null; then
                printf "  %-18s loaded\n" "$svc:"
            elif [ -f "$(daemon_plist "$svc" "$agent")" ]; then
                printf "  %-18s installed, not loaded\n" "$svc:"
            else
                printf "  %-18s not deployed\n" "$svc:"
            fi
        done
        if [ -f "$workspace/.qmd/index.sqlite" ]; then
            if curl -sf "http://localhost:${port}/health" &>/dev/null; then
                echo "  qmd-http health:   responding"
            else
                echo "  qmd-http health:   not responding"
            fi
        fi
    done
    echo ""
    exit 0
}

# =============================================================================
# --uninstall  (scan-based: every system daemon for THIS account, all services)
# =============================================================================
uninstall() {
    sudo_prime
    log "Uninstalling openclaw system daemons for account '${ACCOUNT}'..."
    local svc plist label
    local found=0
    for svc in git-sync obsidian-headless qmd-watch qmd-http; do
        # Pin the svc token + account; '.plist' anchors the trailing agent
        # segment so e.g. a spannagel-account agent named 'tl' is never matched.
        for plist in "$SYSTEM_DAEMONS_DIR/com.openclaw.${svc}.${ACCOUNT}."*.plist; do
            [ -f "$plist" ] || continue
            found=1
            label="$(basename "$plist" .plist)"
            sudo launchctl bootout "system/${label}" 2>/dev/null || true
            sudo rm -f "$plist"
            echo "  removed: ${label}"
        done
    done
    [ "$found" -eq 0 ] && log "  (no daemons found for this account)"
    log "Uninstall complete. Workspaces, indexes, and helper scripts were NOT removed."
    exit 0
}

# =============================================================================
# Arg parsing
# =============================================================================
case "${1:-}" in
    --status)    show_status ;;
    --uninstall) uninstall ;;
esac

mapfile -t ALL_AGENT_IDS < <(get_agent_ids)
SELECTED_AGENTS=()
if [ $# -gt 0 ]; then
    for arg in "$@"; do
        case "$arg" in
            --*) die "Unknown flag '$arg'. Valid: --status, --uninstall" ;;
        esac
        found=false
        for id in "${ALL_AGENT_IDS[@]}"; do
            [ "$id" = "$arg" ] && { SELECTED_AGENTS+=("$arg"); found=true; break; }
        done
        $found || die "Unknown agent '$arg'. Available: ${ALL_AGENT_IDS[*]}"
    done
else
    SELECTED_AGENTS=("${ALL_AGENT_IDS[@]}")
fi

# =============================================================================
# Preflight
# =============================================================================
OB_BIN="$(resolve_ob_bin || true)"   # may be empty -> obsidian service skipped

# launchd does not create the parent of StandardOutPath; without this the
# daemons silently fail to spawn.
mkdir -p "$LOG_DIR"

# Temp dir for generated plists (installed into /Library via `sudo install`).
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/openclaw-daemons.XXXXXX")"
cleanup_tmp() { rm -rf "$TMP_DIR"; }
trap cleanup_tmp EXIT

echo ""
echo "=========================================="
echo "  openclaw system-daemon deploy"
echo "  account:  ${ACCOUNT} (index ${ACCOUNT_INDEX})"
echo "  agents:   ${SELECTED_AGENTS[*]}"
echo "  ob bin:   ${OB_BIN:-<not found — obsidian skipped>}"
echo "=========================================="

# Prime sudo once up front so the per-daemon installs don't each re-prompt.
sudo_prime

# =============================================================================
# Per-agent deployment
# =============================================================================
for agent in "${SELECTED_AGENTS[@]}"; do
    workspace="$(workspace_dir_for "$agent" "$WORKSPACES_DIR")"
    port="${PORT_MAP[$agent]}"

    echo ""
    log "agent '${agent}'  (workspace: ${workspace}, qmd-http port: ${port})"

    if [ ! -d "$workspace" ]; then
        warn "workspace missing — skipping all services for '${agent}': ${workspace}"
        continue
    fi

    # --- git-sync ------------------------------------------------------------
    git_script="$(git_sync_script "$agent")"
    if [ -x "$git_script" ] || [ -f "$git_script" ]; then
        tmp="$TMP_DIR/$(daemon_label git-sync "$agent").plist"
        emit_git_sync_plist "$agent" > "$tmp"
        install_daemon "$(daemon_label git-sync "$agent")" "$tmp"
    else
        warn "git-sync skipped for '${agent}': helper missing — $git_script (run setup-mac-workspaces.sh first)"
    fi

    # --- obsidian-headless ---------------------------------------------------
    # Gate on three prereqs — a missing one must skip cleanly, not deploy a
    # crash-looping daemon: (1) ob binary present, (2) Node 23.11.0 installed
    # (better-sqlite3 12.6.2 has no Node-26 build), (3) the vault is actually
    # linked for this workspace (else `ob sync --continuous` errors forever).
    if [ -z "${OB_BIN:-}" ]; then
        warn "obsidian-headless skipped for '${agent}': ob binary not found (install @nicekiwi/obsidian-headless)"
    elif [ ! -d "$OBSIDIAN_NODE_BIN" ]; then
        warn "obsidian-headless skipped for '${agent}': Node 23.11.0 not installed ($OBSIDIAN_NODE_BIN)"
    elif ! PATH="$OBSIDIAN_PATH" "$OB_BIN" sync-status --path "$workspace" &>/dev/null; then
        warn "obsidian-headless skipped for '${agent}': vault not linked for $workspace (run setup-mac-workspaces.sh first)"
    else
        tmp="$TMP_DIR/$(daemon_label obsidian-headless "$agent").plist"
        emit_obsidian_plist "$agent" "$OB_BIN" "$workspace" > "$tmp"
        install_daemon "$(daemon_label obsidian-headless "$agent")" "$tmp"
    fi

    # --- qmd-watch -----------------------------------------------------------
    # Prereqs: the installed watch script AND a .qmd index dir. We also rewrite
    # the script's agent lock to the per-account form (idempotent).
    watch_script="$(qmd_watch_script "$agent")"
    if [ ! -f "$watch_script" ]; then
        warn "qmd-watch skipped for '${agent}': helper missing — $watch_script (run setup-mac-qmd.sh first)"
    elif [ ! -d "$workspace/.qmd" ]; then
        warn "qmd-watch skipped for '${agent}': no index dir — $workspace/.qmd (run setup-mac-qmd.sh first)"
    else
        namespace_qmd_watch_lock "$watch_script" "$agent"
        tmp="$TMP_DIR/$(daemon_label qmd-watch "$agent").plist"
        emit_qmd_watch_plist "$agent" "$watch_script" > "$tmp"
        install_daemon "$(daemon_label qmd-watch "$agent")" "$tmp"
    fi

    # --- qmd-http ------------------------------------------------------------
    # Prereq: a built index. The qmd binary itself is pinned to ~/.bun/bin/qmd.
    if [ ! -f "$workspace/.qmd/index.sqlite" ]; then
        warn "qmd-http skipped for '${agent}': no index — $workspace/.qmd/index.sqlite (run setup-mac-qmd.sh first)"
    elif [ ! -x "$QMD_BIN" ]; then
        warn "qmd-http skipped for '${agent}': qmd binary not found at $QMD_BIN (bun install -g @tobilu/qmd)"
    else
        tmp="$TMP_DIR/$(daemon_label qmd-http "$agent").plist"
        emit_qmd_http_plist "$agent" "$port" "$workspace" > "$tmp"
        install_daemon "$(daemon_label qmd-http "$agent")" "$tmp"
    fi

    # --- tear down the superseded per-user LaunchAgents ----------------------
    # Do this AFTER the daemons are up so there's no service gap. Best-effort
    # bootout (gui domain may not exist over SSH); the rm is what matters.
    while IFS= read -r old_plist; do
        teardown_old_launchagent "$old_plist"
    done < <(old_launchagent_plists "$agent")
done

echo ""
echo "=========================================="
echo "  Deploy complete — account '${ACCOUNT}'"
echo "=========================================="
echo ""
echo "qmd-http ports:"
for agent in "${SELECTED_AGENTS[@]}"; do
    echo "  ${agent}: http://localhost:${PORT_MAP[$agent]}/mcp"
done
echo ""
echo "Status:    $0 --status"
echo "Logs:      ${LOG_DIR}/{git-sync,obsidian-headless,qmd-watch,qmd-http}-<agent>.log"
echo "Uninstall: $0 --uninstall"
