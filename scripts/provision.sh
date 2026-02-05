#!/usr/bin/env bash
set -euo pipefail

# Provision OpenClaw gateway via Ansible.
# Reads secrets from Pulumi, passes them to Ansible as extra vars.
#
# Usage:
#   ./scripts/provision.sh                          # Full provision
#   ./scripts/provision.sh --tags config            # Config only
#   ./scripts/provision.sh --check --diff           # Dry run
#   ./scripts/provision.sh --tags sandbox -e force_sandbox_rebuild=true

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PULUMI_DIR="$REPO_DIR/pulumi"
ANSIBLE_DIR="$REPO_DIR/ansible"

# Temp directory for secrets (cleaned up on exit)
SECRETS_DIR=$(mktemp -d)
trap 'rm -rf "$SECRETS_DIR"' EXIT

echo "=== Reading secrets from Pulumi ==="

# Read secrets from Pulumi stack outputs and config
cd "$PULUMI_DIR"

gateway_token=$(pulumi stack output openclawGatewayToken --show-secrets 2>/dev/null || echo "")
workspace_deploy_private_key=$(pulumi stack output workspaceDeployPrivateKey --show-secrets 2>/dev/null || echo "")

claude_setup_token=$(pulumi config get claudeSetupToken 2>/dev/null || echo "")
telegram_bot_token=$(pulumi config get telegramBotToken 2>/dev/null || echo "")
telegram_user_id=$(pulumi config get telegramUserId 2>/dev/null || echo "")
workspace_repo_url=$(pulumi config get workspaceRepoUrl 2>/dev/null || echo "")
tailscale_hostname=$(pulumi stack output tailscaleHostname 2>/dev/null || echo "openclaw-vps")

# Write secrets to temp YAML file
SECRETS_FILE="$SECRETS_DIR/secrets.yml"
cat > "$SECRETS_FILE" <<EOF
---
gateway_token: "$(echo "$gateway_token" | sed 's/"/\\"/g')"
claude_setup_token: "$(echo "$claude_setup_token" | sed 's/"/\\"/g')"
telegram_bot_token: "$(echo "$telegram_bot_token" | sed 's/"/\\"/g')"
telegram_user_id: "$telegram_user_id"
workspace_repo_url: "$workspace_repo_url"
workspace_deploy_key: |
$(echo "$workspace_deploy_private_key" | sed 's/^/  /')
EOF
chmod 600 "$SECRETS_FILE"

echo "=== Waiting for Tailscale SSH connectivity ==="

# Resolve host — try Tailscale IP first, fall back to MagicDNS
HOST=""
if command -v tailscale &>/dev/null; then
    HOST=$(tailscale status --json 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
for peer in (data.get('Peer') or {}).values():
    if peer.get('HostName','').lower() == '${tailscale_hostname}'.lower():
        addrs = peer.get('TailscaleIPs', [])
        for a in addrs:
            if '.' in a:
                print(a)
                sys.exit(0)
        if addrs:
            print(addrs[0])
            sys.exit(0)
" 2>/dev/null || echo "")
fi

if [ -z "$HOST" ]; then
    # Fall back to MagicDNS — let Ansible inventory handle resolution
    echo "Could not resolve Tailscale IP directly, relying on inventory"
fi

# Retry loop: wait for SSH via Tailscale
MAX_RETRIES=30
RETRY_DELAY=10
for i in $(seq 1 $MAX_RETRIES); do
    if ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
       ubuntu@"${HOST:-$tailscale_hostname}" true 2>/dev/null; then
        echo "SSH connectivity established"
        break
    fi
    if [ "$i" -eq "$MAX_RETRIES" ]; then
        echo "ERROR: Could not establish SSH connection after $MAX_RETRIES attempts"
        exit 1
    fi
    echo "Waiting for SSH... (attempt $i/$MAX_RETRIES)"
    sleep "$RETRY_DELAY"
done

echo "=== Running Ansible playbook ==="

cd "$ANSIBLE_DIR"

# Install required Ansible collections (idempotent)
ansible-galaxy collection install -r requirements.yml --force-with-deps 2>/dev/null || true

ansible-playbook playbook.yml \
    -e "@$SECRETS_FILE" \
    "$@"

echo "=== Provisioning complete ==="
