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
    api_token="${PROVISION_API_TOKEN:-}"
    provider="${PROVISION_PROVIDER:-claude}"
    token_provider="${PROVISION_TOKEN_PROVIDER:-anthropic}"
    auth_choice="${PROVISION_AUTH_CHOICE:-token}"
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
    # Read provider configuration
    provider=$(pulumi config get provider 2>/dev/null || echo "claude")
    if [ "$provider" = "claude" ]; then
        api_token=$(pulumi config get claudeSetupToken) || {
            echo "ERROR: Provider is 'claude' but claudeSetupToken not set."
            echo "Run: pulumi config set claudeSetupToken --secret"
            exit 1
        }
        token_provider="anthropic"
        auth_choice="token"
    elif [ "$provider" = "kimi" ]; then
        api_token=$(pulumi config get kimiApiKey) || {
            echo "ERROR: Provider is 'kimi' but kimiApiKey not set."
            echo "Run: pulumi config set kimiApiKey --secret"
            exit 1
        }
        token_provider="kimi-coding"
        auth_choice="kimi-code-api-key"
    else
        echo "ERROR: Invalid provider '$provider'. Must be 'claude' or 'kimi'."
        exit 1
    fi
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
if [ -z "$api_token" ]; then
    echo "ERROR: api_token is empty (provider: $provider)."
    exit 1
fi

# Validate: if workspace sync is configured, deploy key must exist
if [ -n "$workspace_repo_url" ] && [ -z "$workspace_deploy_private_key" ]; then
    echo "ERROR: workspaceRepoUrl is set but workspaceDeployPrivateKey is missing."
    exit 1
fi

echo "  provider: $provider"
echo "  gateway_token: set"
echo "  api_token: set ($token_provider)"
echo "  telegram: $([ -n "$telegram_bot_token" ] && echo "configured" || echo "skipped")"
echo "  workspace_sync: $([ -n "$workspace_repo_url" ] && echo "configured" || echo "skipped")"

# Write secrets to temp YAML file
SECRETS_FILE="$SECRETS_DIR/secrets.yml"
cat > "$SECRETS_FILE" <<EOF
---
gateway_token: "$(echo "$gateway_token" | sed 's/"/\\"/g')"
api_token: "$(echo "$api_token" | sed 's/"/\\"/g')"
provider: "$provider"
token_provider: "$token_provider"
auth_choice: "$auth_choice"
telegram_bot_token: "$(echo "$telegram_bot_token" | sed 's/"/\\"/g')"
telegram_user_id: "$telegram_user_id"
workspace_repo_url: "$workspace_repo_url"
workspace_deploy_key: |
$(echo "$workspace_deploy_private_key" | sed 's/^/  /')
EOF
chmod 600 "$SECRETS_FILE"

echo "=== Waiting for Tailscale SSH connectivity ==="

resolve_tailscale_ips() {
    # Returns ALL IPv4 addresses for peers whose HostName starts with the
    # given tailscale_hostname (handles Tailscale's numeric suffix for
    # duplicate hostnames, e.g. openclaw-vps, openclaw-vps-1, openclaw-vps-2).
    if ! command -v tailscale &>/dev/null; then
        return 1
    fi
    local raw
    raw=$(tailscale status --json 2>&1) || return 1
    echo "$raw" | python3 -c "
import json, sys, re
data = json.load(sys.stdin)
base = '${tailscale_hostname}'.lower()
pattern = re.compile(r'^' + re.escape(base) + r'(-\d+)?$')
for peer in (data.get('Peer') or {}).values():
    if pattern.match(peer.get('HostName','').lower()):
        for a in peer.get('TailscaleIPs', []):
            if '.' in a:
                print(a)
                break
" 2>/dev/null
}

# Retry loop: resolve Tailscale IPs and attempt SSH to each candidate
MAX_RETRIES=30
RETRY_DELAY=10
HOST=""
SSH_ERR=""
for i in $(seq 1 $MAX_RETRIES); do
    # Re-resolve all candidate IPs each attempt (new server may appear mid-loop)
    CANDIDATES=$(resolve_tailscale_ips) || true

    if [ -z "$CANDIDATES" ]; then
        echo "No Tailscale peers found yet (attempt $i/$MAX_RETRIES)"
        sleep "$RETRY_DELAY"
        continue
    fi

    if [ -z "$HOST" ]; then
        echo "Tailscale candidates: $(echo $CANDIDATES | tr '\n' ' ')"
    fi

    for TARGET in $CANDIDATES; do
        SSH_ERR=$(timeout 10 ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
           ubuntu@"$TARGET" true 2>&1) && {
            HOST="$TARGET"
            echo "SSH connectivity established (via $HOST)"
            break 2
        }
    done

    if [ "$i" -eq "$MAX_RETRIES" ]; then
        echo "ERROR: Could not establish SSH connection after $MAX_RETRIES attempts"
        echo "Tried candidates: $(echo $CANDIDATES | tr '\n' ' ')"
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
