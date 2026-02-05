#!/usr/bin/env bash
set -euo pipefail

# Provision OpenClaw gateway via Ansible.
#
# When called by Pulumi (via command:local:Command), secrets are passed
# as PROVISION_* environment variables — no Pulumi CLI calls needed.
# When called manually (day-2), falls back to reading from Pulumi CLI.
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

echo "=== Reading secrets ==="

# Prefer PROVISION_* env vars (set by Pulumi), fall back to Pulumi CLI (day-2 manual runs)
if [ -n "${PROVISION_GATEWAY_TOKEN:-}" ]; then
    echo "Using secrets from Pulumi environment variables"
    gateway_token="$PROVISION_GATEWAY_TOKEN"
    claude_setup_token="${PROVISION_CLAUDE_SETUP_TOKEN:-}"
    telegram_bot_token="${PROVISION_TELEGRAM_BOT_TOKEN:-}"
    telegram_user_id="${PROVISION_TELEGRAM_USER_ID:-}"
    workspace_repo_url="${PROVISION_WORKSPACE_REPO_URL:-}"
    workspace_deploy_private_key="${PROVISION_WORKSPACE_DEPLOY_KEY:-}"
    tailscale_hostname="${PROVISION_TAILSCALE_HOSTNAME:-openclaw-vps}"
else
    echo "Reading secrets from Pulumi CLI"
    cd "$PULUMI_DIR"
    gateway_token=$(pulumi stack output openclawGatewayToken --show-secrets) || {
        echo "ERROR: Failed to read gateway token from Pulumi. Is PULUMI_CONFIG_PASSPHRASE set?"
        exit 1
    }
    claude_setup_token=$(pulumi config get claudeSetupToken) || {
        echo "ERROR: Failed to read claudeSetupToken from Pulumi config."
        exit 1
    }
    workspace_deploy_private_key=$(pulumi stack output workspaceDeployPrivateKey --show-secrets 2>/dev/null || echo "")
    telegram_bot_token=$(pulumi config get telegramBotToken 2>/dev/null || echo "")
    telegram_user_id=$(pulumi config get telegramUserId 2>/dev/null || echo "")
    workspace_repo_url=$(pulumi config get workspaceRepoUrl 2>/dev/null || echo "")
    tailscale_hostname=$(pulumi stack output tailscaleHostname 2>/dev/null || echo "openclaw-vps")
fi

# Validate required secrets
if [ -z "$gateway_token" ]; then
    echo "ERROR: gateway_token is empty."
    exit 1
fi
if [ -z "$claude_setup_token" ]; then
    echo "ERROR: claude_setup_token is empty."
    exit 1
fi

# Validate: if workspace sync is configured, deploy key must exist
if [ -n "$workspace_repo_url" ] && [ -z "$workspace_deploy_private_key" ]; then
    echo "ERROR: workspaceRepoUrl is set but workspaceDeployPrivateKey is missing."
    exit 1
fi

echo "  gateway_token: set"
echo "  claude_setup_token: set"
echo "  telegram: $([ -n "$telegram_bot_token" ] && echo "configured" || echo "skipped")"
echo "  workspace_sync: $([ -n "$workspace_repo_url" ] && echo "configured" || echo "skipped")"

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

resolve_tailscale_ip() {
    if ! command -v tailscale &>/dev/null; then
        return 1
    fi
    local raw
    raw=$(tailscale status --json 2>&1) || return 1
    echo "$raw" | python3 -c "
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
" 2>/dev/null
}

# Retry loop: resolve Tailscale IP and attempt SSH each iteration
MAX_RETRIES=30
RETRY_DELAY=10
HOST=""
SSH_ERR=""
for i in $(seq 1 $MAX_RETRIES); do
    # Re-resolve Tailscale IP each attempt (server may not be on tailnet yet)
    if [ -z "$HOST" ]; then
        HOST=$(resolve_tailscale_ip) || true
        if [ -n "$HOST" ]; then
            echo "Resolved Tailscale IP: $HOST"
        fi
    fi

    TARGET="${HOST:-$tailscale_hostname}"
    SSH_ERR=$(ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
       ubuntu@"$TARGET" true 2>&1) && {
        HOST="$TARGET"
        echo "SSH connectivity established (via $HOST)"
        break
    }
    if [ "$i" -eq "$MAX_RETRIES" ]; then
        echo "ERROR: Could not establish SSH connection after $MAX_RETRIES attempts"
        echo "Last SSH error: $SSH_ERR"
        exit 1
    fi
    echo "Waiting for Tailscale SSH... (attempt $i/$MAX_RETRIES)"
    sleep "$RETRY_DELAY"
done

# Export for Ansible inventory
export OPENCLAW_SSH_HOST="$HOST"

echo "=== Running Ansible playbook ==="

cd "$ANSIBLE_DIR"

# Install required Ansible collections
ansible-galaxy collection install -r requirements.yml --force-with-deps || {
    echo "ERROR: Failed to install Ansible Galaxy collections."
    exit 1
}

ansible-playbook playbook.yml \
    -e "@$SECRETS_FILE" \
    "$@"

echo "=== Provisioning complete ==="
