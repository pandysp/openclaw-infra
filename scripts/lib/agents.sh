#!/usr/bin/env bash
# Shared agent resolution from openclaw.yml (the single source of truth).
#
# Reads openclaw_agents and derives workspace naming conventions:
#   - Workspace dir: <id>-workspace
#   - GitHub repo: openclaw-workspace (default) or openclaw-workspace-<id>
#
# Requires: yq, jq
#
# Usage:
#   source "$(dirname "$0")/lib/agents.sh"
#   for id in $(get_agent_ids); do
#       repo=$(workspace_repo_name "$id")
#       echo "$id → $repo"
#   done

_AGENTS_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_OPENCLAW_YML="$_AGENTS_LIB_DIR/../../ansible/group_vars/openclaw.yml"

for _cmd in yq jq; do
    command -v "$_cmd" &>/dev/null || { echo "ERROR: $_cmd not found. Install with: brew install $_cmd" >&2; exit 1; }
done

[ -f "$_OPENCLAW_YML" ] || { echo "ERROR: openclaw.yml not found: $_OPENCLAW_YML" >&2; exit 1; }

# Parse once, cache the result
_AGENTS_JSON=$(yq -o json '.openclaw_agents // []' "$_OPENCLAW_YML")

# All agent IDs, one per line
get_agent_ids() {
    echo "$_AGENTS_JSON" | jq -r '.[].id'
}

# Number of agents
get_agent_count() {
    echo "$_AGENTS_JSON" | jq 'length'
}

# Exit 0 if agent is the default, 1 otherwise
is_default_agent() {
    [ "$(echo "$_AGENTS_JSON" | jq -r --arg id "$1" '.[] | select(.id == $id) | .is_default // false')" = "true" ]
}

# GitHub repo name: openclaw-workspace (default) or openclaw-workspace-<id>
workspace_repo_name() {
    if is_default_agent "$1"; then
        echo "openclaw-workspace"
    else
        echo "openclaw-workspace-$1"
    fi
}

# Local workspace dir: $HOME/main-workspace for the default agent (main),
# otherwise <WORKSPACES_DIR>/<id>-workspace.  Args: $1=agent_id  $2=WORKSPACES_DIR
workspace_dir_for() {
    if is_default_agent "$1"; then
        echo "$HOME/main-workspace"
    else
        echo "$2/${1}-workspace"
    fi
}

# Resolve a concrete node bin dir for LaunchAgents (they don't inherit the shell
# PATH, and version-manager shims are dead without shell activation). Ask node
# itself for its real binary via process.execPath: this resolves THROUGH any shim
# (mise, nvm, asdf, …) to an install dir that works unactivated, so no
# manager-specific branch is needed. Fall back to Homebrew / /usr/local only if
# node isn't on PATH at setup time.
resolve_node_bin_dir() {
    if command -v node &>/dev/null; then
        dirname "$(node -e 'process.stdout.write(process.execPath)')"
    elif [ -x /opt/homebrew/bin/node ]; then
        echo /opt/homebrew/bin
    else
        echo /usr/local/bin
    fi
}
