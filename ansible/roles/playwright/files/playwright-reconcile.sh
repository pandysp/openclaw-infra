#!/usr/bin/env bash
# Remove Playwright resources the configuration no longer wants, before the role
# creates the wanted ones:
#   - browsers of agents that are gone (unit, container, network),
#   - networks whose subnet changed (agent order changed), so they can be recreated
#     without overlapping; that browser's unit stays and the role starts it again,
#   - with no agents at all (feature off): the relay and its network out too.
# Prints CHANGED when it removed anything.
#
# Usage: playwright-reconcile.sh <units-dir> <egress-network> [<agent-id>=<subnet> ...]
set -euo pipefail

units=$1 egress=$2
shift 2
declare -A want=()
for pair in "$@"; do want[${pair%%=*}]=${pair#*=}; done
changed=""

stop_browser() {  # $1 = agent id
    local name="openclaw-playwright-browser-$1"
    systemctl --user disable --now "$name.service" >/dev/null 2>&1 || true
    docker rm -f "$name" >/dev/null 2>&1 || true
}

for unit in "$units"/openclaw-playwright-browser-*.service; do
    [ -e "$unit" ] || continue
    id=$(basename "$unit" .service)
    id=${id#openclaw-playwright-browser-}
    [ -n "${want[$id]+set}" ] && continue
    stop_browser "$id"
    rm -f "$unit"
    changed=yes
done

for network in $(docker network ls --format '{{.Name}}' | grep '^openclaw-playwright-net-' || true); do
    id=${network#openclaw-playwright-net-}
    if [ -n "${want[$id]+set}" ]; then
        subnet=$(docker network inspect --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}' "$network")
        [ "$subnet" = "${want[$id]}" ] && continue
        stop_browser "$id"
    fi
    # The relay is attached to every browser network.
    docker network disconnect -f "$network" openclaw-playwright-relay >/dev/null 2>&1 || true
    docker network rm "$network" >/dev/null
    changed=yes
done

if [ "${#want[@]}" -eq 0 ]; then
    if [ -e "$units/openclaw-playwright-relay.service" ]; then
        systemctl --user disable --now openclaw-playwright-relay.service >/dev/null 2>&1 || true
        rm -f "$units/openclaw-playwright-relay.service"
        changed=yes
    fi
    docker rm -f openclaw-playwright-relay >/dev/null 2>&1 || true
    if docker network inspect "$egress" >/dev/null 2>&1; then
        docker network rm "$egress" >/dev/null
        changed=yes
    fi
fi

if [ -n "$changed" ]; then
    systemctl --user daemon-reload
    echo CHANGED
fi
