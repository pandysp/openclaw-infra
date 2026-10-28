#!/usr/bin/env bash
# Set one agent's MCP tool deny rules: every other agent's servers, both those in
# the configuration (the desired list) and those still registered with the
# adapter. Retired servers stay denied until their registration is gone, and a
# server type that is not registered yet is denied ahead of time.
# Owners come from the configuration (<owners-json>: server name -> agent id).
# A registered server that is no longer configured falls back to the naming rule:
# <prefix> belongs to main, <prefix>-<agent-id> to that agent.
# --tighten keeps every current rule that names a still-registered server: the
# agents role runs before registration changes, so it may add rules but never
# relax them (an ownership change only takes effect once the server's new
# registration is in place). An agent with no rules yet (just created) only gets
# a registered server when its name and its declared owner both say so. The
# plugins role runs it exact after registering.
# Prints UPDATED when it changed the rules, OK otherwise.
#
# Usage: openclaw-mcp-deny.sh [--tighten] <openclaw.json> <agent-id> <desired-deny-json> <owners-json>
set -euo pipefail
export XDG_RUNTIME_DIR=/run/user/1000

tighten=false
if [ "${1:-}" = --tighten ]; then tighten=true; shift; fi
config=$1 id=$2 desired=$3 owners=$4
current=$(jq -cS --arg id "$id" '.agents.entries[$id] | select(. != null) | .tools.deny // [] | sort' "$config") \
    || { echo "ERROR: agent $id is missing from agents.entries" >&2; exit 1; }
want=$(jq -cS --arg id "$id" --argjson desired "$desired" --argjson owners "$owners" \
        --argjson tighten "$tighten" '
    [.plugins.entries["openclaw-mcp-adapter"].config.servers[]?.name] as $registered
    | ((.agents.entries[$id].tools // {}) | has("deny") | not) as $new
    | def by_name: if test("-") then sub("^[^-]+-"; "") else "main" end;
      [$registered[] | select((($owners[.] // by_name) != $id) or ($tighten and $new and (by_name != $id))) | . + "_*"]
    + $desired
    + (if $tighten then [(.agents.entries[$id].tools.deny // [])[] | select(rtrimstr("_*") as $s | $registered | index($s))]
       else [] end)
    | unique' "$config")
if [ "$current" = "$want" ]; then
    echo OK
else
    openclaw config set "agents.entries.$id.tools.deny" --json "$want"
    echo UPDATED
fi
