#!/usr/bin/env bash
#
# Check that the server's TLS clients reject the compromised eMudhra certificate chains.
#
# Runs scripts/lib/tls-trust-check.py on the server over Tailscale SSH: the installed
# Node, every running provisioned Node service (on its own, possibly replaced, Node),
# and Node, Python, curl and uv inside each OpenClaw image, each against the 12 unsafe
# test sites of the October 2026 eMudhra incident and 4 healthy controls. Also fails
# if a running container uses an older build of its image. Read-only apart from
# short-lived `docker run --rm` probe containers.
#
# Usage: ./scripts/verify-tls-trust.sh <host>.<tailnet>.ts.net [host] [images]
# Requires: tailscale. Exits 1 if any target fails or a test site is unreachable.

set -euo pipefail

host="${1:?Usage: $0 <host>.<tailnet>.ts.net [host] [images]}"
shift
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
node_min=$(sed -nE 's/^nodejs_min_version: "([^"]+)"$/\1/p' "$root/ansible/group_vars/all.yml")
[[ -n "$node_min" ]] || { echo "ERROR: nodejs_min_version not found in ansible/group_vars/all.yml" >&2; exit 1; }

# printf %q keeps the remote command a fixed word list whatever the arguments hold.
tailscale ssh "ubuntu@$host" \
    "XDG_RUNTIME_DIR=/run/user/1000 python3 - $(printf '%q ' "$node_min" "$@")" \
    < "$root/scripts/lib/tls-trust-check.py"
