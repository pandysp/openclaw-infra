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
HOSTNAME="${OPENCLAW_HOSTNAME:-openclaw-vps}"
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

FULL_HOSTNAME="${HOSTNAME}.${TAILNET}"

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
if tailscale ping "$FULL_HOSTNAME" -c 1 --timeout 5s > /dev/null 2>&1; then
    check_pass "Tailscale can reach $FULL_HOSTNAME"
else
    check_fail "Cannot reach $FULL_HOSTNAME via Tailscale"
    echo "   Make sure the server has completed cloud-init and Tailscale is authenticated."
    exit 1
fi

# 2. Check SSH access
echo ""
echo "2. Checking SSH access..."
if ssh -o ConnectTimeout=10 -o BatchMode=yes "ubuntu@$FULL_HOSTNAME" "echo 'SSH OK'" > /dev/null 2>&1; then
    check_pass "SSH access working"
else
    check_warn "SSH not accessible (may need to add SSH key to Tailscale ACLs)"
fi

# 3. Check Docker container
echo ""
echo "3. Checking Docker container status..."
CONTAINER_STATUS=$(ssh -o ConnectTimeout=10 "ubuntu@$FULL_HOSTNAME" \
    "docker ps --filter name=openclaw --format '{{.Status}}'" 2>/dev/null || echo "")

if [[ "$CONTAINER_STATUS" == *"Up"* ]]; then
    check_pass "OpenClaw container is running: $CONTAINER_STATUS"
else
    check_fail "OpenClaw container not running"
    echo "   Check logs: ssh ubuntu@$FULL_HOSTNAME 'sudo journalctl -u openclaw -n 50'"
fi

# 4. Check Tailscale Serve
echo ""
echo "4. Checking Tailscale Serve configuration..."
SERVE_STATUS=$(ssh -o ConnectTimeout=10 "ubuntu@$FULL_HOSTNAME" \
    "tailscale serve status 2>&1" || echo "")

if [[ "$SERVE_STATUS" == *"127.0.0.1:18789"* ]] || [[ "$SERVE_STATUS" == *"localhost:18789"* ]]; then
    check_pass "Tailscale Serve configured correctly"
else
    check_warn "Tailscale Serve may not be configured"
    echo "   Status: $SERVE_STATUS"
fi

# 5. Check gateway health endpoint
echo ""
echo "5. Checking gateway health..."
HEALTH_CHECK=$(curl -s --max-time 10 "https://$FULL_HOSTNAME/" 2>/dev/null || echo "FAILED")

if [[ "$HEALTH_CHECK" != "FAILED" ]] && [[ "$HEALTH_CHECK" != "" ]]; then
    check_pass "Gateway responding at https://$FULL_HOSTNAME/"
else
    check_warn "Gateway not responding (may still be starting)"
fi

# 6. Security audit: No public ports
echo ""
echo "6. Security audit: Checking for exposed ports..."
PUBLIC_IP=$(ssh -o ConnectTimeout=10 "ubuntu@$FULL_HOSTNAME" \
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
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║                    Verification Complete                         ║"
echo "╠══════════════════════════════════════════════════════════════════╣"
echo "║                                                                  ║"
echo "║  Access your OpenClaw instance at:                               ║"
echo "║  https://$FULL_HOSTNAME/                                         ║"
echo "║                                                                  ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
