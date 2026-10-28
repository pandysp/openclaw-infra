#!/usr/bin/env bash
# Set one agent's MCP tool deny rules: every other agent's servers, both those in
# the configuration (the desired list) and those still registered with the
# adapter. Retired servers stay denied until their registration is gone, and a
# server type that is not registered yet is denied ahead of time.
# Server names: <prefix> belongs to main, <prefix>-<agent-id> to that agent.
# Prints UPDATED when it changed the rules, OK otherwise.
#
# Usage: openclaw-mcp-deny.sh <openclaw.json> <agent-id> <desired-deny-json>
set -euo pipefail
export XDG_RUNTIME_DIR=/run/user/1000

config=$1 id=$2 desired=$3
current=$(jq -cS --arg id "$id" '.agents.entries[$id] | select(. != null) | .tools.deny // [] | sort' "$config") \
    || { echo "ERROR: agent $id is missing from agents.entries" >&2; exit 1; }
want=$(jq -cS --arg id "$id" --argjson desired "$desired" '
    [.plugins.entries["openclaw-mcp-adapter"].config.servers[]?.name
     | select((if test("-") then sub("^[^-]+-"; "") else "main" end) != $id) | . + "_*"]
    + $desired | unique' "$config")
if [ "$current" = "$want" ]; then
    echo OK
else
    openclaw config set "agents.entries.$id.tools.deny" --json "$want"
    echo UPDATED
fi
