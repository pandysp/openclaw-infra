#!/usr/bin/env bash
# Setup the Mac as a node host for OpenClaw remote exec.
# Run this once on the Mac to install a persistent LaunchAgent.
#
# Prerequisites:
#   - openclaw-cli installed (brew install openclaw-cli)
#   - Tailscale connected to the same tailnet as the VPS
#
# Usage:
#   ./scripts/setup-mac-node.sh
#
# After running, approve the pairing request on the VPS:
#   ssh ubuntu@openclaw-vps 'openclaw devices list'
#   ssh ubuntu@openclaw-vps 'openclaw devices approve <request-id>'
#
# Then re-provision to auto-discover and pin the node ID:
#   ./scripts/provision.sh --tags config
#
# Known issue: The gateway sends the VPS workspace path as the working
# directory for node commands. Since this path doesn't exist on macOS,
# agents must pass workdir=/tmp or workdir=/Users/<user> in every exec
# call. See: https://github.com/openclaw/openclaw/issues/15441

set -euo pipefail

# --- Resolve gateway hostname from Tailscale (using JSON for reliability) ---
TS_JSON=$(tailscale status --json 2>&1) || {
    echo "ERROR: 'tailscale status --json' failed: $TS_JSON"
    echo "  Make sure Tailscale is connected and the VPS is online"
    exit 1
}

GATEWAY_HOST=$(echo "$TS_JSON" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for peer in (data.get('Peer') or {}).values():
    if 'openclaw-vps' in peer.get('HostName', ''):
        print(peer['HostName'])
        break
" 2>&1) || {
    echo "ERROR: Failed to parse Tailscale JSON: $GATEWAY_HOST"
    exit 1
}

if [ -z "$GATEWAY_HOST" ]; then
    echo "ERROR: Cannot find openclaw-vps in Tailscale peers"
    echo "  Make sure the VPS is online and joined to your tailnet"
    exit 1
fi

MAGIC_DNS_SUFFIX=$(echo "$TS_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('MagicDNSSuffix',''))" 2>&1) || {
    echo "ERROR: Failed to extract MagicDNS suffix: $MAGIC_DNS_SUFFIX"
    exit 1
}

if [ -z "$MAGIC_DNS_SUFFIX" ]; then
    echo "ERROR: Could not determine Tailscale MagicDNS suffix"
    echo "  Ensure MagicDNS is enabled in your Tailscale admin console"
    echo "  Fallback: set GATEWAY_FQDN manually and re-run"
    exit 1
fi

GATEWAY_FQDN="${GATEWAY_HOST}.${MAGIC_DNS_SUFFIX}"

echo "Gateway: $GATEWAY_FQDN (port 443, TLS via Tailscale Serve)"

# --- Check if already installed ---
CURRENT_STATUS=$(openclaw node status 2>&1 || true)
if echo "$CURRENT_STATUS" | grep -q "running"; then
    echo "Node host is already installed and running."
    echo "$CURRENT_STATUS"
    read -p "Reinstall? [y/N] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 0
    fi
    FORCE_FLAG="--force"
else
    FORCE_FLAG=""
fi

# --- Install LaunchAgent ---
echo "Installing node host LaunchAgent..."
openclaw node install --host "$GATEWAY_FQDN" --port 443 --tls $FORCE_FLAG

echo ""
echo "Node host installed and running."
openclaw node status

# --- Configure exec approvals ---
echo ""
echo "Configuring node-side exec approvals..."

APPROVAL_FAILED=0
add_approval() {
    local pattern="$1"
    if ! openclaw approvals allowlist add --agent "*" "$pattern" 2>&1; then
        echo "  WARNING: Failed to add allowlist pattern: $pattern"
        APPROVAL_FAILED=1
    fi
}

# Standard system paths
add_approval "/bin/*"
add_approval "/usr/bin/*"
add_approval "/opt/homebrew/bin/*"

# Specific commands needed for tmux-based workflows
for cmd in tmux claude ps sleep echo hostname which grep cat tail head ls; do
    add_approval "$cmd"
done

if [ "$APPROVAL_FAILED" -ne 0 ]; then
    echo ""
    echo "WARNING: Some exec approval patterns failed to add."
    echo "  Check: openclaw approvals allowlist list"
    echo "  Add manually: openclaw approvals allowlist add --agent '*' '<pattern>'"
fi

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Approve the pairing request on the VPS (if first time):"
echo "     ssh ubuntu@openclaw-vps 'openclaw devices list'"
echo "     ssh ubuntu@openclaw-vps 'openclaw devices approve <request-id>'"
echo ""
echo "  2. Re-provision to auto-discover the node ID:"
echo "     ./scripts/provision.sh --tags config"
echo ""
echo "  3. Verify exec works:"
echo "     ssh ubuntu@openclaw-vps 'openclaw nodes run --cwd /tmp echo hello'"
