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
# IMPORTANT: You must also enable node exec in ansible/group_vars/all.yml:
#   node_exec_enabled: true
#
# Then re-provision to install node-exec-mcp and auto-discover the node ID:
#   ./scripts/provision.sh --tags config,plugins
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

# --- Fix versioned Cellar path (survives brew upgrades) ---
PLIST="$HOME/Library/LaunchAgents/ai.openclaw.node.plist"
BREW_PREFIX="$(brew --prefix 2>/dev/null || echo '/opt/homebrew')"
OPENCLAW_BIN="${BREW_PREFIX}/bin/openclaw"
if [ -f "$PLIST" ] && grep -q '/Cellar/openclaw-cli/' "$PLIST"; then
    echo "Patching LaunchAgent to use stable symlink (avoids brew upgrade breakage)..."
    PLIST_PATH="$PLIST" OPENCLAW_BIN="$OPENCLAW_BIN" python3 -c "
import plistlib, os, sys, tempfile

p = os.environ['PLIST_PATH']
openclaw_bin = os.environ['OPENCLAW_BIN']

with open(p, 'rb') as f:
    d = plistlib.load(f)
args = d.get('ProgramArguments', [])
cellar_idx = next((i for i, a in enumerate(args) if '/Cellar/openclaw-cli/' in a), None)
if cellar_idx is not None:
    node_idx = cellar_idx - 1 if cellar_idx > 0 and 'bin/node' in args[cellar_idx - 1] else None
    if node_idx is not None:
        args[node_idx:cellar_idx + 1] = [openclaw_bin]
    else:
        args[cellar_idx] = openclaw_bin
    d['ProgramArguments'] = args
    # Atomic write via temp file
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), suffix='.plist')
    try:
        with os.fdopen(fd, 'wb') as f:
            plistlib.dump(d, f)
        os.replace(tmp, p)
    except BaseException:
        os.unlink(tmp)
        raise
    print(f'  Patched: using {openclaw_bin}')
else:
    print('  Already using stable path, no patch needed')
"
    # Reload with patched plist
    launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
    sleep 1
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    sleep 2
fi

echo ""
echo "Node host installed and running."
openclaw node status

# --- Configure exec approvals (auto-approve all commands) ---
echo ""
echo "Configuring node-side exec approvals..."
APPROVALS_FILE="$HOME/.openclaw/exec-approvals.json"
if [ -f "$APPROVALS_FILE" ]; then
    # Set defaults.security to "full" so all commands are auto-approved.
    # Note: basename-only allowlist patterns (e.g., "echo") are ignored by OpenClaw —
    # only full path patterns (e.g., "/bin/*") work. Using defaults.security: full
    # is simpler and covers everything.
    APPROVALS_FILE="$APPROVALS_FILE" python3 -c "
import json, os, sys
p = os.environ['APPROVALS_FILE']
with open(p) as f:
    d = json.load(f)
if d.get('defaults', {}).get('security') != 'full':
    d.setdefault('defaults', {})['security'] = 'full'
    with open(p, 'w') as f:
        json.dump(d, f, indent=2)
    print('  Set defaults.security: full (auto-approve all commands)')
else:
    print('  Already set to auto-approve')
"
else
    echo "  WARNING: $APPROVALS_FILE not found. Run 'openclaw node install' first."
fi

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Approve the pairing request on the VPS (if first time):"
echo "     ssh ubuntu@openclaw-vps 'openclaw devices list'"
echo "     ssh ubuntu@openclaw-vps 'openclaw devices approve <request-id>'"
echo ""
echo "  2. Enable node exec in ansible/group_vars/all.yml:"
echo "     node_exec_enabled: true"
echo ""
echo "  3. Re-provision to install node-exec-mcp and discover the node ID:"
echo "     ./scripts/provision.sh --tags config,plugins"
echo ""
echo "  4. Verify exec works:"
echo "     ssh ubuntu@openclaw-vps 'openclaw nodes run --cwd /tmp echo hello'"
