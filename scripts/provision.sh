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
    telegram_manon_user_id="${PROVISION_TELEGRAM_MANON_USER_ID:-}"
    telegram_group_id="${PROVISION_TELEGRAM_GROUP_ID:-}"
    workspace_repo_url="${PROVISION_WORKSPACE_REPO_URL:-}"
    workspace_deploy_private_key="${PROVISION_WORKSPACE_DEPLOY_KEY:-}"
    workspace_manon_repo_url="${PROVISION_WORKSPACE_MANON_REPO_URL:-}"
    workspace_manon_deploy_key="${PROVISION_WORKSPACE_MANON_DEPLOY_KEY:-}"
    workspace_tl_repo_url="${PROVISION_WORKSPACE_TL_REPO_URL:-}"
    workspace_tl_deploy_key="${PROVISION_WORKSPACE_TL_DEPLOY_KEY:-}"
    telegram_henning_user_id="${PROVISION_TELEGRAM_HENNING_USER_ID:-}"
    telegram_ph_group_id="${PROVISION_TELEGRAM_PH_GROUP_ID:-}"
    workspace_henning_repo_url="${PROVISION_WORKSPACE_HENNING_REPO_URL:-}"
    workspace_henning_deploy_key="${PROVISION_WORKSPACE_HENNING_DEPLOY_KEY:-}"
    workspace_ph_repo_url="${PROVISION_WORKSPACE_PH_REPO_URL:-}"
    workspace_ph_deploy_key="${PROVISION_WORKSPACE_PH_DEPLOY_KEY:-}"
    tailscale_hostname="${PROVISION_TAILSCALE_HOSTNAME:-openclaw-vps}"
    xai_api_key="${PROVISION_XAI_API_KEY:-}"
    github_token="${PROVISION_GITHUB_TOKEN:-}"
    github_token_manon="${PROVISION_GITHUB_TOKEN_MANON:-}"
    github_token_tl="${PROVISION_GITHUB_TOKEN_TL:-}"
    github_token_henning="${PROVISION_GITHUB_TOKEN_HENNING:-}"
    github_token_ph="${PROVISION_GITHUB_TOKEN_PH:-}"
    obsidian_andy_vault_repo_url="${PROVISION_OBSIDIAN_ANDY_VAULT_REPO_URL:-}"
    obsidian_manon_vault_repo_url="${PROVISION_OBSIDIAN_MANON_VAULT_REPO_URL:-}"
    obsidian_tl_vault_repo_url="${PROVISION_OBSIDIAN_TL_VAULT_REPO_URL:-}"
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
    telegram_manon_user_id=$(pulumi config get telegramManonUserId 2>/dev/null || echo "")
    telegram_group_id=$(pulumi config get telegramGroupId 2>/dev/null || echo "")
    workspace_repo_url=$(pulumi config get workspaceRepoUrl 2>/dev/null || echo "")
    workspace_manon_repo_url=$(pulumi config get workspaceManonRepoUrl 2>/dev/null || echo "")
    workspace_manon_deploy_key=$(pulumi stack output workspaceManonDeployPrivateKey --show-secrets 2>/dev/null || echo "")
    workspace_tl_repo_url=$(pulumi config get workspaceTlRepoUrl 2>/dev/null || echo "")
    workspace_tl_deploy_key=$(pulumi stack output workspaceTlDeployPrivateKey --show-secrets 2>/dev/null || echo "")
    telegram_henning_user_id=$(pulumi config get telegramHenningUserId 2>/dev/null || echo "")
    telegram_ph_group_id=$(pulumi config get telegramPhGroupId 2>/dev/null || echo "")
    workspace_henning_repo_url=$(pulumi config get workspaceHenningRepoUrl 2>/dev/null || echo "")
    workspace_henning_deploy_key=$(pulumi stack output workspaceHenningDeployPrivateKey --show-secrets 2>/dev/null || echo "")
    workspace_ph_repo_url=$(pulumi config get workspacePhRepoUrl 2>/dev/null || echo "")
    workspace_ph_deploy_key=$(pulumi stack output workspacePhDeployPrivateKey --show-secrets 2>/dev/null || echo "")
    tailscale_hostname=$(pulumi stack output tailscaleHostname 2>/dev/null || echo "openclaw-vps")
    xai_api_key=$(pulumi config get xaiApiKey 2>/dev/null || echo "")
    github_token=$(pulumi config get githubToken 2>/dev/null || echo "")
    github_token_manon=$(pulumi config get githubTokenManon 2>/dev/null || echo "")
    github_token_tl=$(pulumi config get githubTokenTl 2>/dev/null || echo "")
    github_token_henning=$(pulumi config get githubTokenHenning 2>/dev/null || echo "")
    github_token_ph=$(pulumi config get githubTokenPh 2>/dev/null || echo "")
    obsidian_andy_vault_repo_url=$(pulumi config get obsidianAndyVaultRepoUrl 2>/dev/null || echo "")
    obsidian_manon_vault_repo_url=$(pulumi config get obsidianManonVaultRepoUrl 2>/dev/null || echo "")
    obsidian_tl_vault_repo_url=$(pulumi config get obsidianTlVaultRepoUrl 2>/dev/null || echo "")
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

# Validate deploy keys: if repo URL is set, deploy key must exist and be valid
validate_deploy_key() {
    local name="$1" url="$2" key="$3"
    if [ -n "$url" ] && [ -z "$key" ]; then
        echo "ERROR: $name repo URL is set but deploy key is missing."
        exit 1
    fi
    if [ -n "$key" ]; then
        if ! echo "$key" | grep -q "BEGIN OPENSSH PRIVATE KEY"; then
            echo "ERROR: $name deploy key missing header."
            exit 1
        fi
        if ! echo "$key" | grep -q "END OPENSSH PRIVATE KEY"; then
            echo "ERROR: $name deploy key missing footer (possibly truncated)."
            exit 1
        fi
    fi
}
validate_deploy_key "workspace (main)" "$workspace_repo_url" "$workspace_deploy_private_key"
validate_deploy_key "workspace (manon)" "$workspace_manon_repo_url" "$workspace_manon_deploy_key"
validate_deploy_key "workspace (tl)" "$workspace_tl_repo_url" "$workspace_tl_deploy_key"
validate_deploy_key "workspace (henning)" "$workspace_henning_repo_url" "$workspace_henning_deploy_key"
validate_deploy_key "workspace (ph)" "$workspace_ph_repo_url" "$workspace_ph_deploy_key"

echo "  gateway_token: set"
echo "  claude_setup_token: set"
echo "  telegram: $([ -n "$telegram_bot_token" ] && echo "configured" || echo "skipped")"
echo "  telegram_manon: $([ -n "$telegram_manon_user_id" ] && echo "configured" || echo "skipped")"
echo "  telegram_group: $([ -n "$telegram_group_id" ] && echo "configured" || echo "skipped")"
echo "  telegram_henning: $([ -n "$telegram_henning_user_id" ] && echo "configured" || echo "skipped")"
echo "  telegram_ph_group: $([ -n "$telegram_ph_group_id" ] && echo "configured" || echo "skipped")"
echo "  workspace_sync (main): $([ -n "$workspace_repo_url" ] && echo "configured" || echo "skipped")"
echo "  workspace_sync (manon): $([ -n "$workspace_manon_repo_url" ] && echo "configured" || echo "skipped")"
echo "  workspace_sync (tl): $([ -n "$workspace_tl_repo_url" ] && echo "configured" || echo "skipped")"
echo "  workspace_sync (henning): $([ -n "$workspace_henning_repo_url" ] && echo "configured" || echo "skipped")"
echo "  workspace_sync (ph): $([ -n "$workspace_ph_repo_url" ] && echo "configured" || echo "skipped")"
echo "  grok_search: $([ -n "$xai_api_key" ] && echo "configured" || echo "skipped")"
echo "  github_mcp (main): $([ -n "$github_token" ] && echo "configured" || echo "skipped")"
echo "  github_mcp (manon): $([ -n "$github_token_manon" ] && echo "configured" || echo "skipped")"
echo "  github_mcp (tl): $([ -n "$github_token_tl" ] && echo "configured" || echo "skipped")"
echo "  github_mcp (henning): $([ -n "$github_token_henning" ] && echo "configured" || echo "skipped")"
echo "  github_mcp (ph): $([ -n "$github_token_ph" ] && echo "configured" || echo "skipped")"
echo "  obsidian (andy): $([ -n "$obsidian_andy_vault_repo_url" ] && echo "configured" || echo "skipped")"
echo "  obsidian (manon): $([ -n "$obsidian_manon_vault_repo_url" ] && echo "configured" || echo "skipped")"
echo "  obsidian (tl): $([ -n "$obsidian_tl_vault_repo_url" ] && echo "configured" || echo "skipped")"

# Read Codex auth credentials from local machine (optional)
# Run `codex login` locally to create ~/.codex/auth.json before deploying.
codex_auth_json=""
CODEX_AUTH_FILE="${HOME}/.codex/auth.json"
if [ -f "$CODEX_AUTH_FILE" ]; then
    codex_auth_json=$(cat "$CODEX_AUTH_FILE")
    if [ -n "$codex_auth_json" ] && ! echo "$codex_auth_json" | jq empty 2>/dev/null; then
        echo "ERROR: ~/.codex/auth.json is not valid JSON. Run 'codex login' to regenerate."
        exit 1
    fi
fi
echo "  codex_auth: $([ -n "$codex_auth_json" ] && echo "found (~/.codex/auth.json)" || echo "skipped (run 'codex login' to enable)")"

# Write secrets to temp YAML file using Python for safe escaping
SECRETS_FILE="$SECRETS_DIR/secrets.yml"
env \
  _S_GATEWAY_TOKEN="$gateway_token" \
  _S_CLAUDE_SETUP_TOKEN="$claude_setup_token" \
  _S_TELEGRAM_BOT_TOKEN="$telegram_bot_token" \
  _S_TELEGRAM_USER_ID="$telegram_user_id" \
  _S_TELEGRAM_MANON_USER_ID="$telegram_manon_user_id" \
  _S_TELEGRAM_GROUP_ID="$telegram_group_id" \
  _S_TELEGRAM_HENNING_USER_ID="$telegram_henning_user_id" \
  _S_TELEGRAM_PH_GROUP_ID="$telegram_ph_group_id" \
  _S_WORKSPACE_REPO_URL="$workspace_repo_url" \
  _S_XAI_API_KEY="$xai_api_key" \
  _S_WORKSPACE_MANON_REPO_URL="$workspace_manon_repo_url" \
  _S_WORKSPACE_TL_REPO_URL="$workspace_tl_repo_url" \
  _S_WORKSPACE_HENNING_REPO_URL="$workspace_henning_repo_url" \
  _S_WORKSPACE_PH_REPO_URL="$workspace_ph_repo_url" \
  _S_GITHUB_TOKEN="$github_token" \
  _S_GITHUB_TOKEN_MANON="$github_token_manon" \
  _S_GITHUB_TOKEN_TL="$github_token_tl" \
  _S_GITHUB_TOKEN_HENNING="$github_token_henning" \
  _S_GITHUB_TOKEN_PH="$github_token_ph" \
  _S_OBSIDIAN_ANDY_VAULT_REPO_URL="$obsidian_andy_vault_repo_url" \
  _S_OBSIDIAN_MANON_VAULT_REPO_URL="$obsidian_manon_vault_repo_url" \
  _S_OBSIDIAN_TL_VAULT_REPO_URL="$obsidian_tl_vault_repo_url" \
  python3 -c "
import json, sys, os
# json.dumps() safely quotes strings for YAML (handles quotes, backslashes, special chars)
keys = [
    'gateway_token', 'claude_setup_token', 'telegram_bot_token',
    'telegram_user_id', 'telegram_manon_user_id', 'telegram_group_id',
    'telegram_henning_user_id', 'telegram_ph_group_id',
    'workspace_repo_url', 'xai_api_key',
    'workspace_manon_repo_url', 'workspace_tl_repo_url',
    'workspace_henning_repo_url', 'workspace_ph_repo_url',
    'github_token', 'github_token_manon', 'github_token_tl',
    'github_token_henning', 'github_token_ph',
    'obsidian_andy_vault_repo_url', 'obsidian_manon_vault_repo_url',
    'obsidian_tl_vault_repo_url',
]
with open(sys.argv[1], 'w') as f:
    f.write('---\n')
    for k in keys:
        v = os.environ['_S_' + k.upper()]
        f.write(f'{k}: {json.dumps(v)}\n')
" "$SECRETS_FILE"

# Append deploy keys (block scalar when non-empty, explicit empty string otherwise)
append_deploy_key() {
    local name="$1" value="$2" file="$3"
    if [ -n "$value" ]; then
        printf '%s: |\n' "$name" >> "$file"
        echo "$value" | sed 's/^/  /' >> "$file"
    else
        printf '%s: ""\n' "$name" >> "$file"
    fi
}
append_deploy_key "workspace_deploy_key" "$workspace_deploy_private_key" "$SECRETS_FILE"
append_deploy_key "workspace_manon_deploy_key" "$workspace_manon_deploy_key" "$SECRETS_FILE"
append_deploy_key "workspace_tl_deploy_key" "$workspace_tl_deploy_key" "$SECRETS_FILE"
append_deploy_key "workspace_henning_deploy_key" "$workspace_henning_deploy_key" "$SECRETS_FILE"
append_deploy_key "workspace_ph_deploy_key" "$workspace_ph_deploy_key" "$SECRETS_FILE"

# Append Codex auth credentials (block scalar preserves JSON structure)
append_deploy_key "codex_auth_json" "$codex_auth_json" "$SECRETS_FILE"
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

    # Fall back to hostname (MagicDNS may resolve before tailscale status learns the peer)
    if [ -z "$CANDIDATES" ]; then
        CANDIDATES="$tailscale_hostname"
    fi

    if [ -z "$HOST" ]; then
        echo "Tailscale candidates: $(echo $CANDIDATES | tr '\n' ' ')"
    fi

    # Try each candidate via SSH
    for TARGET in $CANDIDATES; do
        SSH_ERR=$(ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
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

# Ensure Ansible is on PATH (Pulumi child processes may not inherit shell PATH)
if ! command -v ansible-galaxy &>/dev/null || ! command -v ansible-playbook &>/dev/null; then
    # Search common install locations
    for candidate in "$HOME/.local/bin" "$HOME/Library/Python"/*/bin /opt/homebrew/bin /usr/local/bin; do
        if [ -x "$candidate/ansible-galaxy" ] && [ -x "$candidate/ansible-playbook" ]; then
            export PATH="$candidate:$PATH"
            echo "Found Ansible in $candidate (added to PATH)"
            break
        fi
    done
    if ! command -v ansible-galaxy &>/dev/null; then
        echo "ERROR: ansible-galaxy not found on PATH."
        echo ""
        echo "Install Ansible:  pip install ansible"
        echo "Or with pipx:     pipx install ansible"
        echo ""
        echo "If already installed, ensure it's on your PATH. Common locations:"
        echo "  ~/.local/bin  (pip install --user)"
        echo "  ~/Library/Python/3.x/bin  (macOS)"
        echo "  /opt/homebrew/bin  (Homebrew)"
        exit 1
    fi
fi

# Install required Ansible collections
ansible-galaxy collection install -r requirements.yml --upgrade || {
    echo "ERROR: Failed to install Ansible Galaxy collections."
    exit 1
}

# Clean up any stale cron-skipped marker from previous runs
rm -f /tmp/ansible-cron-skipped

ansible-playbook playbook.yml \
    -e "@$SECRETS_FILE" \
    "$@"

# Check if cron setup was skipped (gateway not healthy, e.g. first install)
if [ -f /tmp/ansible-cron-skipped ]; then
    rm -f /tmp/ansible-cron-skipped
    echo ""
    echo "╔══════════════════════════════════════════════════════════════════╗"
    echo "║  WARNING: Cron job setup was SKIPPED (gateway not healthy).    ║"
    echo "║  This is expected on first install (device pairing pending).   ║"
    echo "║  After approving devices, re-run:                             ║"
    echo "║    ./scripts/provision.sh --tags telegram                     ║"
    echo "╚══════════════════════════════════════════════════════════════════╝"
    echo ""
    echo "=== Provisioning complete (with warnings) ==="
    exit 2
else
    echo "=== Provisioning complete ==="
fi
