#!/bin/bash
#
# OpenClaw Post-Deployment Verification Script
#
# Run this after `pulumi up` to verify the deployment.
# Requires: tailscale CLI, ssh

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
HOSTNAME_PREFIX="${OPENCLAW_HOSTNAME:-openclaw-vps}"
TAILNET="${TAILNET:-}"  # Your tailnet domain (e.g., tail12345.ts.net)

echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║              OpenClaw Deployment Verification                    ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""

# Check if tailnet is configured
if [ -z "$TAILNET" ]; then
    echo -e "${YELLOW}⚠ TAILNET not set. Set it with: export TAILNET=your-tailnet.ts.net${NC}"
    echo "  Attempting to auto-detect from tailscale status..."
    TAILNET=$(tailscale status --json 2>/dev/null | jq -r '.MagicDNSSuffix // empty' || true)
    if [ -z "$TAILNET" ]; then
        echo -e "${RED}✗ Could not detect tailnet. Please set TAILNET environment variable.${NC}"
        exit 1
    fi
    echo -e "${GREEN}✓ Detected tailnet: $TAILNET${NC}"
fi

# Auto-detect actual hostname (may have numeric suffix like openclaw-vps-1)
echo ""
echo "Detecting OpenClaw server..."
# First try to find an online device (filter lines first, then extract hostname)
DETECTED_HOSTNAME=$(tailscale status 2>/dev/null | grep -E "${HOSTNAME_PREFIX}(-[0-9]+)?" | grep -v "offline" | grep -oE "${HOSTNAME_PREFIX}(-[0-9]+)?" | head -1 || true)

if [ -z "$DETECTED_HOSTNAME" ]; then
    # Fallback: check for any matching device even if offline
    DETECTED_HOSTNAME=$(tailscale status 2>/dev/null | grep -oE "${HOSTNAME_PREFIX}(-[0-9]+)?" | head -1 || true)
fi

if [ -z "$DETECTED_HOSTNAME" ]; then
    echo -e "${RED}✗ No device matching '${HOSTNAME_PREFIX}*' found in Tailscale${NC}"
    exit 1
fi

if [ "$DETECTED_HOSTNAME" != "$HOSTNAME_PREFIX" ]; then
    echo -e "${YELLOW}⚠ Device registered as '$DETECTED_HOSTNAME' (has suffix)${NC}"
    echo "  Tip: Remove stale devices at https://login.tailscale.com/admin/machines"
else
    echo -e "${GREEN}✓ Detected device: $DETECTED_HOSTNAME${NC}"
fi

FULL_HOSTNAME="${DETECTED_HOSTNAME}.${TAILNET}"

# Test functions
check_pass() {
    echo -e "${GREEN}✓ $1${NC}"
}

check_fail() {
    echo -e "${RED}✗ $1${NC}"
}

check_warn() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

# 1. Check Tailscale connectivity
echo ""
echo "1. Checking Tailscale connectivity..."
# Check if device is online in tailscale status (idle, active, or direct all mean online)
# Offline devices show "offline" in the status
if tailscale status | grep "$DETECTED_HOSTNAME" | grep -qv "offline"; then
    check_pass "Tailscale can reach $DETECTED_HOSTNAME"
else
    check_fail "Cannot reach $DETECTED_HOSTNAME via Tailscale"
    echo "   Make sure the server has completed cloud-init and Tailscale is authenticated."
    exit 1
fi

# 2. Check SSH access
echo ""
echo "2. Checking SSH access..."
if ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -o BatchMode=yes "ubuntu@$FULL_HOSTNAME" "echo 'SSH OK'" > /dev/null 2>&1; then
    check_pass "SSH access working"
else
    check_warn "SSH not accessible (may need to add SSH key to Tailscale ACLs)"
fi

# 3. Check Node.js version
echo ""
echo "3. Checking Node.js version..."
NODE_VERSION=$(ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "ubuntu@$FULL_HOSTNAME" \
    "source ~/.nvm/nvm.sh && node --version" 2>/dev/null || echo "")

if [[ "$NODE_VERSION" == v22* ]]; then
    check_pass "Node.js version: $NODE_VERSION"
elif [[ -n "$NODE_VERSION" ]]; then
    check_warn "Node.js version $NODE_VERSION (expected v22+)"
else
    check_fail "Node.js not found or not accessible"
fi

# 4. Check OpenClaw systemd user service
echo ""
echo "4. Checking OpenClaw service status..."
SERVICE_STATUS=$(ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "ubuntu@$FULL_HOSTNAME" \
    "XDG_RUNTIME_DIR=/run/user/1000 systemctl --user is-active openclaw-gateway" 2>/dev/null || echo "inactive")

if [[ "$SERVICE_STATUS" == "active" ]]; then
    check_pass "OpenClaw service is running"
else
    check_fail "OpenClaw service not running (status: $SERVICE_STATUS)"
    echo "   Check logs: ssh ubuntu@$FULL_HOSTNAME 'XDG_RUNTIME_DIR=/run/user/1000 systemctl --user status openclaw-gateway'"
fi

# 5. Check Tailscale Serve
echo ""
echo "5. Checking Tailscale Serve configuration..."
SERVE_STATUS=$(ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "ubuntu@$FULL_HOSTNAME" \
    "tailscale serve status 2>&1" || echo "")

if [[ "$SERVE_STATUS" == *"18789"* ]]; then
    check_pass "Tailscale Serve configured correctly"
else
    check_warn "Tailscale Serve may not be configured"
    echo "   Status: $SERVE_STATUS"
fi

# 6. Check gateway health endpoint
echo ""
echo "6. Checking gateway health..."
HEALTH_CHECK=$(curl -s --max-time 10 "https://$FULL_HOSTNAME/" 2>/dev/null || echo "FAILED")

if [[ "$HEALTH_CHECK" != "FAILED" ]] && [[ "$HEALTH_CHECK" != "" ]]; then
    check_pass "Gateway responding at https://$FULL_HOSTNAME/"
else
    check_warn "Gateway not responding (may still be starting)"
fi

# 7. Check expected ports on localhost
echo ""
echo "7. Checking local ports on server..."
PORTS_CHECK=$(ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "ubuntu@$FULL_HOSTNAME" \
    "ss -tlnp | grep -E ':(18789|18791|18793)'" 2>/dev/null || echo "")

if [[ -n "$PORTS_CHECK" ]]; then
    check_pass "OpenClaw ports listening on localhost"
    echo "   $PORTS_CHECK" | head -3
else
    check_warn "Expected ports (18789, 18791, 18793) not found"
fi

# 8. Security audit: No public ports
echo ""
echo "8. Security audit: Checking for exposed ports..."
PUBLIC_IP=$(ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "ubuntu@$FULL_HOSTNAME" \
    "curl -s ifconfig.me" 2>/dev/null || echo "UNKNOWN")

if [ "$PUBLIC_IP" != "UNKNOWN" ]; then
    echo "   Server public IP: $PUBLIC_IP"

    # Check common ports from external
    for PORT in 22 80 443 8080 18789; do
        if nc -z -w 2 "$PUBLIC_IP" "$PORT" 2>/dev/null; then
            check_fail "Port $PORT is publicly accessible!"
        else
            check_pass "Port $PORT is blocked (good)"
        fi
    done
else
    check_warn "Could not determine public IP"
fi

echo ""
echo "═══════════════════════════════════════════════════════════════════"
echo "                    Verification Complete"
echo "═══════════════════════════════════════════════════════════════════"
echo ""
echo "Access your OpenClaw instance at:"
echo "  https://$FULL_HOSTNAME/"
echo ""
echo "Note: Cloud-init log was already cleaned up during deployment."
echo "═══════════════════════════════════════════════════════════════════"
