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
    check_fail "SSH connection failed"
    echo ""
    echo -e "${RED}SSH connection failed — skipping 11 remote checks${NC}"
    echo "   Possible causes: SSH key not in Tailscale ACLs, server still booting, sshd not running"
    echo ""
    echo "═══════════════════════════════════════════════════════════════════"
    echo "                    Verification Complete"
    echo "═══════════════════════════════════════════════════════════════════"
    exit 1
fi

# 3. Check Node.js version (must be >= v22)
echo ""
echo "3. Checking Node.js version..."
NODE_VERSION=$(ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "ubuntu@$FULL_HOSTNAME" \
    "node --version" 2>/dev/null || echo "")

NODE_MAJOR=$(echo "$NODE_VERSION" | grep -oE '[0-9]+' | head -1)
if [[ -z "$NODE_VERSION" ]]; then
    check_fail "Node.js not found or not accessible"
elif [[ "${NODE_MAJOR:-0}" -ge 22 ]]; then
    check_pass "Node.js version: $NODE_VERSION"
else
    check_warn "Node.js version $NODE_VERSION (expected v22+)"
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

# 7. Check gateway port on localhost (18789 is the only port openclaw binds)
echo ""
echo "7. Checking local ports on server..."
PORTS_CHECK=$(ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "ubuntu@$FULL_HOSTNAME" \
    "ss -tlnp | grep -E ':18789'" 2>/dev/null || echo "")

if [[ -n "$PORTS_CHECK" ]]; then
    check_pass "OpenClaw gateway listening on localhost:18789"
    echo "   $PORTS_CHECK" | head -2
else
    check_warn "Gateway port 18789 not found"
fi

# 8. Security audit: no public ports
# Force IPv4 for the scan — BSD nc on macOS ignores -w for IPv6 and hangs
# indefinitely on unreachable addresses. The Hetzner cloud firewall applies
# uniformly to v4 and v6, so scanning v4 is sufficient to confirm the intent.
echo ""
echo "8. Security audit: Checking for exposed ports..."
PUBLIC_IP=$(ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "ubuntu@$FULL_HOSTNAME" \
    "curl -s --ipv4 --max-time 5 ifconfig.me" 2>/dev/null || echo "UNKNOWN")

if [ "$PUBLIC_IP" != "UNKNOWN" ] && [[ "$PUBLIC_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "   Server public IPv4: $PUBLIC_IP"
    # Python-based scan: BSD nc's -w timeout is unreliable against silently
    # dropped packets (Hetzner firewall drops without RST, so SYN_SENT
    # never resolves). socket.settimeout is deterministic.
    for PORT in 22 80 443 8080 18789; do
        RESULT=$(python3 -c "
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(2)
try:
    s.connect(('$PUBLIC_IP', $PORT))
    print('open')
except Exception:
    print('closed')
finally:
    s.close()
" 2>/dev/null)
        if [[ "$RESULT" == "open" ]]; then
            check_fail "Port $PORT is publicly accessible!"
        else
            check_pass "Port $PORT is blocked (good)"
        fi
    done
else
    check_warn "Could not determine server public IPv4 (got: ${PUBLIC_IP})"
fi

# 9. OpenClaw built-in status
# Server-side timeout guards against openclaw status hanging during config
# hot-reload or MCP bootstrap (observed taking >2min on some invocations).
echo ""
echo "9. Checking OpenClaw status..."
OPENCLAW_STATUS=$(ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "ubuntu@$FULL_HOSTNAME" \
    "timeout 60 openclaw status 2>&1" || echo "FAILED")

if [[ "$OPENCLAW_STATUS" == "FAILED" ]] || [[ -z "$OPENCLAW_STATUS" ]]; then
    check_warn "Could not get OpenClaw status (timed out or unreachable)"
elif echo "$OPENCLAW_STATUS" | grep -qE "Gateway service\s+.*running"; then
    check_pass "OpenClaw status OK (gateway service running)"
    echo "$OPENCLAW_STATUS" | grep -E "^│ (Gateway|Agents|Update|Channel) " | head -5 | sed 's/^/   /'
else
    check_warn "OpenClaw status returned but gateway service marker missing"
    echo "$OPENCLAW_STATUS" | head -8 | sed 's/^/   /'
fi

# 10. OpenClaw health check
# Look for negative signals rather than just non-empty output. A channel line
# reading "Discord: error" would pass a non-empty check but indicates a problem.
echo ""
echo "10. Checking OpenClaw health..."
OPENCLAW_HEALTH=$(ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "ubuntu@$FULL_HOSTNAME" \
    "timeout 60 openclaw health 2>&1" || echo "FAILED")

if [[ "$OPENCLAW_HEALTH" == "FAILED" ]] || [[ -z "$OPENCLAW_HEALTH" ]]; then
    check_warn "Could not get OpenClaw health"
elif echo "$OPENCLAW_HEALTH" | grep -qiE ":\s*(error|offline|expired|disconnected|failed)"; then
    check_fail "OpenClaw health reports a failing channel/agent"
    echo "$OPENCLAW_HEALTH" | grep -iE ":\s*(error|offline|expired|disconnected|failed)" | sed 's/^/   /'
else
    check_pass "OpenClaw health OK"
    echo "$OPENCLAW_HEALTH" | head -10 | sed 's/^/   /'
fi

# 11. OpenClaw security audit
echo ""
echo "11. Running OpenClaw security audit..."
SECURITY_AUDIT=$(ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "ubuntu@$FULL_HOSTNAME" \
    "timeout 180 openclaw security audit --deep 2>&1" || echo "FAILED")

# The audit summary line reads e.g. "Summary: 0 critical · 3 warn · 2 info".
# Grep for it rather than tail -3, which otherwise shows the last finding body
# and hides the counts that actually matter.
SUMMARY_LINE=$(echo "$SECURITY_AUDIT" | grep -E "^Summary:" | head -1)
if [[ "$SUMMARY_LINE" == *"0 critical"* ]]; then
    check_pass "Security audit passed"
    [[ -n "$SUMMARY_LINE" ]] && echo "   $SUMMARY_LINE"
elif [[ "$SECURITY_AUDIT" != "FAILED" ]]; then
    check_warn "Security audit returned critical findings"
    [[ -n "$SUMMARY_LINE" ]] && echo "   $SUMMARY_LINE"
else
    check_warn "Could not run security audit"
fi

# 12. Channel status — one SSH call, parse once per channel
echo ""
echo "12. Checking configured channels..."
CHANNELS_STATUS=$(ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "ubuntu@$FULL_HOSTNAME" \
    "timeout 60 openclaw channels status 2>&1" || echo "FAILED")

check_channel() {
    local name="$1"
    local line
    # `|| true`: an unconfigured channel (no matching line) makes grep exit 1,
    # which under `set -euo pipefail` would kill the script at the assignment
    # before the "not configured" branch below can handle it. Only bites when
    # some channels are absent (staging runs Telegram only; prod has all three,
    # so every grep matched and it never surfaced there).
    line=$(echo "$CHANNELS_STATUS" | grep -iE "^- $name" | head -1 || true)
    if [[ -z "$line" ]]; then
        echo "   $name not configured (optional)"
    elif [[ "$line" == *"enabled"* ]]; then
        check_pass "$name channel enabled"
        echo "$line" | sed 's/^/   /'
    else
        check_warn "$name channel present but not enabled"
        echo "$line" | sed 's/^/   /'
    fi
}

check_channel "Telegram"
check_channel "WhatsApp"
check_channel "Discord"

# 13. Check cron jobs
echo ""
echo "13. Checking scheduled tasks..."
CRON_LIST=$(ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "ubuntu@$FULL_HOSTNAME" \
    "timeout 60 openclaw cron list 2>&1" || echo "FAILED")

# Each cron job row starts with a UUID. Matching on status (idle|running|ok)
# was fragile and missed jobs whose status doesn't happen to fall in that set;
# UUID prefix matches every job regardless of current state.
CRON_COUNT=$(echo "$CRON_LIST" | grep -c -E '^[0-9a-f]{8}-[0-9a-f]{4}-' || true)
if [[ "$CRON_LIST" == "FAILED" ]]; then
    check_warn "Could not reach server to list cron jobs"
elif [[ "$CRON_COUNT" -gt 0 ]]; then
    check_pass "$CRON_COUNT scheduled task(s) configured"
    echo "$CRON_LIST" | grep -E '^[0-9a-f]{8}-[0-9a-f]{4}-' | sed 's/^/   /' | head -5
else
    echo "   No cron jobs configured (optional)"
fi

# 14. Check local gateway token (Mac client only)
LOCAL_CONFIG="$HOME/.openclaw/openclaw.json"
TOKEN_LEN=$(OPENCLAW_CONFIG="$LOCAL_CONFIG" python3 -c "
import json, os
with open(os.environ['OPENCLAW_CONFIG']) as f:
    d = json.load(f)
print(len(d.get('gateway', {}).get('remote', {}).get('token', '')))" || echo "0")
if [ "$TOKEN_LEN" -gt 0 ] 2>/dev/null; then
    echo ""
    echo "14. Checking local gateway token..."
    check_pass "Local gateway.remote.token is set"
elif [ -f "$LOCAL_CONFIG" ]; then
    # Non-fatal: verify continues to report all checks
    echo ""
    echo "14. Checking local gateway token..."
    check_fail "Local gateway.remote.token is EMPTY — node host cannot authenticate"
    echo "   Fix: run ./scripts/setup-mac-node.sh or restore from backup:"
    echo "   cat ~/.openclaw/openclaw.json.bak | python3 -c \"import json,sys; print(json.load(sys.stdin)['gateway']['remote']['token'])\""
fi

# 15. Version match — IaC pin vs installed. Catches drift across VPS, local CLI,
# and the Mac node host: these three must stay in lockstep to avoid protocol
# mismatches after a skipped upgrade.
echo ""
echo "15. Checking version alignment (IaC pin vs installed)..."
IAC_VERSION=$(grep -E '^openclaw_version:' "$(dirname "${BASH_SOURCE[0]}")/../ansible/group_vars/all.yml" 2>/dev/null | sed -E 's/.*"([^"]+)".*/\1/' || echo "")
VPS_VERSION=$(ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "ubuntu@$FULL_HOSTNAME" 'openclaw --version' 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || echo "")
LOCAL_VERSION=$(openclaw --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || echo "")

if [[ -z "$IAC_VERSION" ]]; then
    check_warn "Could not read openclaw_version from ansible/group_vars/all.yml"
elif [[ -z "$VPS_VERSION" ]]; then
    check_warn "Could not query VPS openclaw version"
elif [[ "$VPS_VERSION" != "$IAC_VERSION" ]]; then
    check_fail "VPS on $VPS_VERSION but IaC pins $IAC_VERSION — run ./scripts/provision.sh --tags openclaw"
elif [[ -n "$LOCAL_VERSION" ]] && [[ "$LOCAL_VERSION" != "$IAC_VERSION" ]]; then
    check_warn "Local CLI on $LOCAL_VERSION but VPS/IaC on $IAC_VERSION — brew upgrade openclaw-cli"
else
    check_pass "All components on $IAC_VERSION (IaC=$IAC_VERSION, VPS=$VPS_VERSION, local=${LOCAL_VERSION:-n/a})"
fi

echo ""
echo "═══════════════════════════════════════════════════════════════════"
echo "                    Verification Complete"
echo "═══════════════════════════════════════════════════════════════════"
echo ""
echo "Access your OpenClaw instance at:"
echo "  https://$FULL_HOSTNAME/"
echo "═══════════════════════════════════════════════════════════════════"
