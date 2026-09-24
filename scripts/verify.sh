#!/bin/bash
#
# OpenClaw Post-Deployment Verification Script
#
# Run this after `pulumi up` to verify the deployment.
# Requires: tailscale CLI, jq

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color
FAILURES=0

# Configuration
EXPECTED_HOSTNAME="${OPENCLAW_HOSTNAME:-openclaw-vps}"
TAILNET="${TAILNET:-}"  # Your tailnet domain (e.g., tail12345.ts.net)

echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║              OpenClaw Deployment Verification                    ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""

# Require one exact online peer; never choose somebody else's suffix match.
TAILSCALE_STATUS=$(tailscale status --json)
if [[ -z "$TAILNET" ]]; then
    TAILNET=$(printf '%s' "$TAILSCALE_STATUS" | jq -er '.MagicDNSSuffix | select(type == "string" and length > 0)')
fi
FULL_HOSTNAME=$(printf '%s' "$TAILSCALE_STATUS" | jq -er --arg name "$EXPECTED_HOSTNAME" --arg suffix "$TAILNET" '
    [.Peer[] | select(.HostName == $name)] |
    if length == 1 and .[0].Online == true and .[0].DNSName == ($name + "." + $suffix + ".")
    then .[0].DNSName | rtrimstr(".")
    else error("Expected exactly one online peer with the requested hostname") end')
if [[ -n "${STAGING_HOST:-}" && "$FULL_HOSTNAME" != "$STAGING_HOST" ]]; then
    echo "ERROR: Verification target differs from this run's staging host"
    exit 1
fi
echo "Verifying exact host: $FULL_HOSTNAME"

# Test functions
check_pass() {
    echo -e "${GREEN}✓ $1${NC}"
}

check_fail() {
    echo -e "${RED}✗ $1${NC}"
    FAILURES=$((FAILURES + 1))
}

check_warn() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

# 1. Check Tailscale connectivity
echo ""
echo "1. Checking Tailscale connectivity..."
check_pass "Tailscale can reach $EXPECTED_HOSTNAME"

# 2. Check SSH access
echo ""
echo "2. Checking SSH access..."
if tailscale ssh "ubuntu@$FULL_HOSTNAME" "echo 'SSH OK'" > /dev/null 2>&1; then
    check_pass "SSH access working"
else
    check_fail "SSH connection failed"
    echo ""
    echo -e "${RED}SSH connection failed — skipping remaining checks${NC}"
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
NODE_VERSION=$(tailscale ssh "ubuntu@$FULL_HOSTNAME" \
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
SERVICE_STATUS=$(tailscale ssh "ubuntu@$FULL_HOSTNAME" \
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
SERVE_STATUS=$(tailscale ssh "ubuntu@$FULL_HOSTNAME" \
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
if HTTP_STATUS=$(curl --silent --show-error --max-time 10 --output /dev/null \
    --write-out '%{http_code}' "https://$FULL_HOSTNAME/") && [[ "$HTTP_STATUS" =~ ^2[0-9][0-9]$ ]]; then
    check_pass "Gateway responding at https://$FULL_HOSTNAME/"
else
    check_fail "Gateway HTTPS request did not complete with HTTP success"
fi

# 7. Check gateway port on localhost (18789 is the only port openclaw binds)
echo ""
echo "7. Checking local ports on server..."
PORTS_CHECK=$(tailscale ssh "ubuntu@$FULL_HOSTNAME" \
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
PUBLIC_IP="${STAGING_PUBLIC_IP:-}"
if { [[ -n "$PUBLIC_IP" ]] || PUBLIC_IP=$(tailscale ssh "ubuntu@$FULL_HOSTNAME" \
    "curl --fail --silent --show-error --ipv4 --max-time 5 https://ifconfig.me" 2>/dev/null); } &&
    [[ "$PUBLIC_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "   Server public IPv4: $PUBLIC_IP"
    # Python-based scan: BSD nc's -w timeout is unreliable against silently
    # dropped packets (Hetzner firewall drops without RST, so SYN_SENT
    # never resolves). socket.settimeout is deterministic.
    for PORT in 22 80 443 8080 18789; do
        if ! RESULT=$(python3 - "$PUBLIC_IP" "$PORT" 2>/dev/null <<'PY'
import ipaddress, socket, sys
address = str(ipaddress.IPv4Address(sys.argv[1]))
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
    connection.settimeout(2)
    try:
        connection.connect((address, int(sys.argv[2])))
        print('open')
    except (TimeoutError, ConnectionRefusedError):
        print('closed')
PY
        ); then
            check_fail "Port $PORT scan failed; no network evidence was obtained"
        elif [[ "$RESULT" == "open" ]]; then
            check_fail "Port $PORT is publicly accessible!"
        elif [[ "$RESULT" == "closed" ]]; then
            check_pass "Port $PORT is blocked (good)"
        else
            check_fail "Port $PORT scan returned an invalid result"
        fi
    done
else
    check_fail "Could not determine server public IPv4; public-port checks were not performed"
fi

# 9. OpenClaw health check
# Require a completed command and an explicit healthy JSON result.
echo ""
echo "9. Checking OpenClaw health..."
if OPENCLAW_HEALTH=$(tailscale ssh "ubuntu@$FULL_HOSTNAME" "timeout 60 openclaw health --json"); then
    if printf '%s' "$OPENCLAW_HEALTH" | jq -se 'length == 1 and (.[0] | type == "object" and .ok == true)' >/dev/null; then
        check_pass "OpenClaw health OK"
    else
        check_fail "OpenClaw returned an invalid or unhealthy result"
    fi
else
    check_fail "OpenClaw health did not complete successfully (SSH, timeout or health failure)"
fi

# 10. OpenClaw security audit
echo ""
echo "10. Running OpenClaw security audit..."
if SECURITY_AUDIT=$(tailscale ssh "ubuntu@$FULL_HOSTNAME" \
    "timeout 180 openclaw security audit --deep --json"); then
    if printf '%s' "$SECURITY_AUDIT" | jq -se \
        'length == 1 and (.[0] | type == "object" and .summary.critical == 0)' >/dev/null; then
        check_pass "Security audit passed (0 critical findings)"
    else
        check_fail "Security audit returned critical findings or an invalid result"
    fi
else
    check_fail "Security audit did not complete successfully (SSH, timeout or audit failure)"
fi

# 11. Channel status — one SSH call, parse once per channel
echo ""
echo "11. Checking configured channels..."
if CHANNELS_STATUS=$(tailscale ssh "ubuntu@$FULL_HOSTNAME" \
    "timeout 60 openclaw channels status --json" 2>/dev/null) &&
    printf '%s' "$CHANNELS_STATUS" | jq -es --arg required "${REQUIRED_CHANNELS:-}" '
        def healthy: .enabled == true and .configured == true and .running == true and
                     .lastError == null and .connected == true;
        length == 1 and (.[0].channelAccounts |
            type == "object" and all(.[]; type == "array") and
            all(.[][] | select(.enabled == true and .configured == true); healthy) and
            (. as $accounts | all($required | split(",")[] | select(length > 0);
                $accounts[.] | type == "array" and length > 0 and all(.[]; healthy))))
    ' >/dev/null 2>&1; then
    check_pass "Configured and explicitly required channels are healthy"
else
    check_fail "Channel query failed, a required channel is missing, or an account is unhealthy"
fi

# 12. Check scheduled automation policy
echo ""
echo "12. Checking scheduled automation policy..."
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
POLICY_OK=true
POLICY_PYTHON=python3
if command -v ansible-playbook >/dev/null 2>&1; then
    ANSIBLE_SHEBANG=$(head -1 "$(command -v ansible-playbook)")
    if [[ "$ANSIBLE_SHEBANG" == '#!'* ]]; then
        ANSIBLE_PYTHON=${ANSIBLE_SHEBANG#\#!}
        if [[ -x "$ANSIBLE_PYTHON" ]] && "$ANSIBLE_PYTHON" -c 'import yaml' >/dev/null 2>&1; then
            POLICY_PYTHON="$ANSIBLE_PYTHON"
        fi
    fi
fi
if ! "$POLICY_PYTHON" -c 'import yaml' >/dev/null 2>&1; then
    POLICY_OK=false
    AUTOMATION_POLICY=""
    check_fail "Could not load the YAML parser used for scheduled automation verification"
elif ! AUTOMATION_POLICY=$("$POLICY_PYTHON" - "$REPO_DIR/ansible/group_vars/all.yml" "$REPO_DIR/ansible/group_vars/openclaw.yml" 2>/dev/null << 'PYEOF'
import json, pathlib, sys, yaml

def require_mapping(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} root must be a mapping")
    return value

defaults = require_mapping(yaml.safe_load(pathlib.Path(sys.argv[1]).read_text()) or {}, "all.yml")
override_path = pathlib.Path(sys.argv[2])
override = yaml.safe_load(override_path.read_text()) if override_path.exists() else {}
override = require_mapping(override or {}, "openclaw.yml")
agents = override.get("openclaw_agents", defaults.get("openclaw_agents", []))
default_enabled = override.get(
    "openclaw_scheduled_automation_default",
    defaults.get("openclaw_scheduled_automation_default", False),
)
if not isinstance(default_enabled, bool):
    raise ValueError("openclaw_scheduled_automation_default must be a boolean")
if not isinstance(agents, list) or not agents:
    raise ValueError("effective openclaw_agents must be a non-empty list")
seen = set()
paused = []
active = []
for agent in agents:
    if not isinstance(agent, dict):
        raise ValueError("every effective agent must be a mapping")
    agent_id = agent.get("id")
    if not isinstance(agent_id, str) or not agent_id or agent_id in seen:
        raise ValueError("effective agent IDs must be non-empty and unique")
    seen.add(agent_id)
    enabled = agent.get("scheduled_automation_enabled", default_enabled)
    if not isinstance(enabled, bool):
        raise ValueError(f"scheduled automation switch for {agent_id} must be a boolean")
    (active if enabled else paused).append(agent_id)
print(json.dumps({"paused": paused, "active": active}))
PYEOF
); then
    POLICY_OK=false
    AUTOMATION_POLICY=""
    check_fail "Could not parse scheduled automation policy; check the agent configuration (private diagnostics withheld)"
fi

CRON_OK=true
if CRON_RAW=$(tailscale ssh "ubuntu@$FULL_HOSTNAME" \
    "timeout 60 openclaw cron list --all --json" 2>/dev/null); then
    CRON_JSON=$(printf '%s\n' "$CRON_RAW" | sed -E '/^\[[A-Za-z][^]]*\]/d')
else
    CRON_OK=false
    CRON_JSON=""
    check_fail "Cron query failed; check the gateway connection and CLI (private diagnostics withheld)"
fi

STATUS_OK=true
if STATUS_RAW=$(tailscale ssh "ubuntu@$FULL_HOSTNAME" \
    "timeout 60 openclaw status --json" 2>/dev/null); then
    STATUS_JSON=$(printf '%s\n' "$STATUS_RAW" | sed -E '/^\[[A-Za-z][^]]*\]/d')
else
    STATUS_OK=false
    STATUS_JSON=""
    check_fail "Heartbeat query failed; check the gateway connection and CLI (private diagnostics withheld)"
fi

BOOTSTRAP_OK=true
if BOOTSTRAP_RAW=$(tailscale ssh "ubuntu@$FULL_HOSTNAME" \
    "openclaw config get agents.defaults.skipBootstrap" 2>/dev/null); then
    SKIP_BOOTSTRAP=$(printf '%s\n' "$BOOTSTRAP_RAW" | sed -E '/^\[[A-Za-z][^]]*\]/d')
else
    BOOTSTRAP_OK=false
    SKIP_BOOTSTRAP=""
    check_fail "Could not read agents.defaults.skipBootstrap; check the gateway connection and CLI (private diagnostics withheld)"
fi

if [[ "$CRON_OK" == true ]] && ! jq -e '.jobs | type == "array"' >/dev/null 2>&1 <<< "$CRON_JSON"; then
    check_fail "Could not read cron state including disabled jobs"
elif [[ "$STATUS_OK" == true ]] && ! jq -e '.heartbeat.agents | type == "array"' >/dev/null 2>&1 <<< "$STATUS_JSON"; then
    check_fail "Could not read heartbeat state"
elif [[ "$CRON_OK" != true ]] || [[ "$STATUS_OK" != true ]] || [[ "$POLICY_OK" != true ]]; then
    : # The specific query/parser failure was already reported above.
else
    PAUSED=$(jq -c '.paused' <<< "$AUTOMATION_POLICY")
    ACTIVE=$(jq -c '.active' <<< "$AUTOMATION_POLICY")
    CRON_TOTAL=$(jq '.jobs | length' <<< "$CRON_JSON")
    CRON_ENABLED=$(jq '[.jobs[] | select(.enabled)] | length' <<< "$CRON_JSON")
    CRON_DISABLED=$(jq '[.jobs[] | select(.enabled | not)] | length' <<< "$CRON_JSON")
    ENABLED_FOR_PAUSED=$(jq --argjson paused "$PAUSED" \
        '[.jobs[] | select(.agentId as $id | $paused | index($id)) | select(.enabled)] | length' <<< "$CRON_JSON")
    LIVE_HEARTBEATS_FOR_PAUSED=$(jq --argjson paused "$PAUSED" \
        '[.heartbeat.agents[] | select(.agentId as $id | $paused | index($id)) | select(.everyMs != null)] | length' <<< "$STATUS_JSON")
    MISSING_ACTIVE_HEARTBEATS=$(jq --argjson active "$ACTIVE" \
        '[ $active[] as $id | select([.heartbeat.agents[] | select(.agentId == $id and .everyMs != null)] | length == 0) ] | length' <<< "$STATUS_JSON")

    if [[ "$ENABLED_FOR_PAUSED" -eq 0 ]] && [[ "$LIVE_HEARTBEATS_FOR_PAUSED" -eq 0 ]]; then
        check_pass "Paused agents have no enabled cron jobs or live heartbeats"
    else
        check_fail "Paused-agent policy violated: cron=$ENABLED_FOR_PAUSED heartbeat=$LIVE_HEARTBEATS_FOR_PAUSED"
    fi
    if [[ "$MISSING_ACTIVE_HEARTBEATS" -eq 0 ]]; then
        check_pass "Enabled agents have live heartbeat cadences"
    else
        check_fail "$MISSING_ACTIVE_HEARTBEATS enabled agent(s) lack a live heartbeat cadence"
    fi
    echo "   Cron inventory: total=$CRON_TOTAL enabled=$CRON_ENABLED disabled=$CRON_DISABLED"
fi
if [[ "$BOOTSTRAP_OK" != true ]]; then
    : # The transport/CLI failure was already reported above.
elif [[ "$SKIP_BOOTSTRAP" == "true" ]]; then
    check_pass "Workspace bootstrap replacement is disabled"
else
    check_fail "agents.defaults.skipBootstrap is '$SKIP_BOOTSTRAP', expected true — context files may be re-scaffolded"
fi

# 13. Check local gateway token (Mac client only)
LOCAL_CONFIG="$HOME/.openclaw/openclaw.json"
echo ""
echo "13. Checking local gateway token..."
if [[ ! -f "$LOCAL_CONFIG" ]]; then
    echo "   Local OpenClaw node config not present (optional on this machine)"
elif ! TOKEN_LEN=$(OPENCLAW_CONFIG="$LOCAL_CONFIG" python3 -c "
import json, os
with open(os.environ['OPENCLAW_CONFIG']) as f:
    d = json.load(f)
print(len(d.get('gateway', {}).get('remote', {}).get('token', '')))" 2>/dev/null); then
    check_fail "Local OpenClaw node config is unreadable"
elif [ "$TOKEN_LEN" -gt 0 ] 2>/dev/null; then
    check_pass "Local gateway.remote.token is set"
else
    # Non-fatal: verify continues to report all checks
    check_fail "Local gateway.remote.token is EMPTY — node host cannot authenticate"
    echo "   Fix: run ./scripts/setup-mac-node.sh; never print the gateway token or its backup."
fi

# 14. Version match — IaC pin vs installed. Catches drift across VPS, local CLI,
# and the Mac node host: these three must stay in lockstep to avoid protocol
# mismatches after a skipped upgrade.
echo ""
echo "14. Checking version alignment (IaC pin vs installed)..."
IAC_VERSION=$(grep -E '^openclaw_version:' "$(dirname "${BASH_SOURCE[0]}")/../ansible/group_vars/all.yml" 2>/dev/null | sed -E 's/.*"([^"]+)".*/\1/' || echo "")
VPS_VERSION=$(tailscale ssh "ubuntu@$FULL_HOSTNAME" 'openclaw --version' 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || echo "")
LOCAL_VERSION=$(openclaw --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || echo "")

if [[ -z "$IAC_VERSION" ]]; then
    check_warn "Could not read openclaw_version from ansible/group_vars/all.yml"
elif [[ -z "$VPS_VERSION" ]]; then
    check_warn "Could not query VPS openclaw version"
elif [[ "$VPS_VERSION" != "$IAC_VERSION" ]]; then
    check_fail "VPS on $VPS_VERSION but IaC pins $IAC_VERSION — run ./scripts/provision.sh --tags openclaw"
elif [[ -n "$LOCAL_VERSION" ]] && [[ "$LOCAL_VERSION" != "$IAC_VERSION" ]]; then
    check_warn "Local CLI on $LOCAL_VERSION but VPS/IaC on $IAC_VERSION — align versions intentionally before using the local CLI as contract evidence"
else
    check_pass "All components on $IAC_VERSION (IaC=$IAC_VERSION, VPS=$VPS_VERSION, local=${LOCAL_VERSION:-n/a})"
fi

echo ""
echo "═══════════════════════════════════════════════════════════════════"
echo "                    Verification Complete"
echo "═══════════════════════════════════════════════════════════════════"
echo ""
if [[ "$FAILURES" -gt 0 ]]; then
    echo -e "${RED}$FAILURES verification check(s) failed.${NC}"
    exit 1
fi

echo "Access your OpenClaw instance at:"
echo "  https://$FULL_HOSTNAME/"
echo "═══════════════════════════════════════════════════════════════════"
