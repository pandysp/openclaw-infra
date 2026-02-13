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

set -euo pipefail

# --- Resolve gateway hostname from Tailscale ---
GATEWAY_HOST=$(tailscale status 2>/dev/null | grep -m1 'openclaw-vps' | awk '{print $2}')
if [ -z "$GATEWAY_HOST" ]; then
    echo "ERROR: Cannot find openclaw-vps in tailscale status"
    echo "  Make sure Tailscale is connected and the VPS is online"
    exit 1
fi

MAGIC_DNS_SUFFIX=$(tailscale status --json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('MagicDNSSuffix',''))" 2>/dev/null || true)
if [ -n "$MAGIC_DNS_SUFFIX" ]; then
    GATEWAY_FQDN="${GATEWAY_HOST}.${MAGIC_DNS_SUFFIX}"
else
    GATEWAY_FQDN="$GATEWAY_HOST"
fi

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

# --- Configure exec approvals (allow all for now) ---
echo ""
echo "Configuring node-side exec approvals..."
openclaw approvals allowlist add --agent "*" "/bin/*" 2>/dev/null || true
openclaw approvals allowlist add --agent "*" "/usr/bin/*" 2>/dev/null || true
openclaw approvals allowlist add --agent "*" "/opt/homebrew/bin/*" 2>/dev/null || true
openclaw approvals allowlist add --agent "*" "*" 2>/dev/null || true

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
