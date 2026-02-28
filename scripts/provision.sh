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

# Read env var by name, defaulting to empty string (safe under set -u)
read_env() {
    eval "printf '%s' \"\${$1:-}\""
}

echo "=== Reading secrets ==="

# Read agent IDs — from Pulumi env var (set during `pulumi up`) or Pulumi CLI (day-2 manual runs)
agent_ids_str="${PROVISION_AGENT_IDS:-}"

# Prefer PROVISION_* env vars (set by Pulumi), fall back to Pulumi CLI (day-2 manual runs)
if [ -n "${PROVISION_GATEWAY_TOKEN:-}" ]; then
    echo "Using secrets from Pulumi environment variables"
    # All PROVISION_* vars already in environment — nothing to do here
else
    echo "Reading secrets from Pulumi CLI"
    cd "$PULUMI_DIR"

    # Read agent IDs from Pulumi config if not in environment
    if [ -z "$agent_ids_str" ]; then
        agent_ids_str=$(pulumi config get agentIds 2>/dev/null || echo "")
    fi

    # Required
    export PROVISION_GATEWAY_TOKEN=$(pulumi stack output openclawGatewayToken --show-secrets) || {
        echo "ERROR: Failed to read gateway token from Pulumi. Is PULUMI_CONFIG_PASSPHRASE set?"
        exit 1
    }
    export PROVISION_CLAUDE_SETUP_TOKEN=$(pulumi config get claudeSetupToken) || {
        echo "ERROR: Failed to read claudeSetupToken from Pulumi config."
        exit 1
    }

    # Main agent config (no suffix in key names)
    export PROVISION_TELEGRAM_BOT_TOKEN=$(pulumi config get telegramBotToken 2>/dev/null || echo "")
    export PROVISION_TELEGRAM_USER_ID=$(pulumi config get telegramUserId 2>/dev/null || echo "")
    export PROVISION_TELEGRAM_GROUP_ID=$(pulumi config get telegramGroupId 2>/dev/null || echo "")
    export PROVISION_WORKSPACE_REPO_URL=$(pulumi config get workspaceRepoUrl 2>/dev/null || echo "")
    export PROVISION_TAILSCALE_HOSTNAME=$(pulumi stack output tailscaleHostname 2>/dev/null || echo "openclaw-vps")
    export PROVISION_XAI_API_KEY=$(pulumi config get xaiApiKey 2>/dev/null || echo "")
    export PROVISION_GROQ_API_KEY=$(pulumi config get groqApiKey 2>/dev/null || echo "")
    export PROVISION_GITHUB_TOKEN=$(pulumi config get githubToken 2>/dev/null || echo "")
    export PROVISION_OBSIDIAN_ANDY_VAULT_REPO_URL=$(pulumi config get obsidianAndyVaultRepoUrl 2>/dev/null || echo "")
    export PROVISION_OBSIDIAN_AUTH_TOKEN=$(pulumi config get obsidianAuthToken 2>/dev/null || echo "")
    export PROVISION_OBSIDIAN_VAULT_PASSWORD=$(pulumi config get obsidianVaultPassword 2>/dev/null || echo "")

    # Read deploy keys: try structured export first, fall back to individual exports
    # (individual exports exist until first `pulumi up` after migration)
    _keys_json=$(pulumi stack output agentWorkspaceKeys --json --show-secrets 2>/dev/null || echo "{}")
    _main_key=$(echo "$_keys_json" | jq -r '.main.privateKey // ""')
    if [ -z "$_main_key" ]; then
        _main_key=$(pulumi stack output workspaceDeployPrivateKey --show-secrets 2>/dev/null || echo "")
    fi
    export PROVISION_WORKSPACE_DEPLOY_KEY="$_main_key"

    # Per-agent config (from Pulumi CLI)
    export PROVISION_AGENT_IDS="$agent_ids_str"
    IFS=',' read -ra _cli_agents <<< "$agent_ids_str"
    for _id in "${_cli_agents[@]}"; do
        [ -z "$_id" ] && continue
        _upper=$(echo "$_id" | tr '[:lower:]' '[:upper:]')
        _pascal=$(echo "$_id" | awk '{print toupper(substr($0,1,1)) substr($0,2)}')

        export "PROVISION_GITHUB_TOKEN_${_upper}=$(pulumi config get "githubToken${_pascal}" 2>/dev/null || echo "")"
        export "PROVISION_TELEGRAM_${_upper}_USER_ID=$(pulumi config get "telegram${_pascal}UserId" 2>/dev/null || echo "")"
        export "PROVISION_TELEGRAM_${_upper}_GROUP_ID=$(pulumi config get "telegram${_pascal}GroupId" 2>/dev/null || echo "")"
        export "PROVISION_WHATSAPP_${_upper}_PHONE=$(pulumi config get "whatsapp${_pascal}Phone" 2>/dev/null || echo "")"
        export "PROVISION_WORKSPACE_${_upper}_REPO_URL=$(pulumi config get "workspace${_pascal}RepoUrl" 2>/dev/null || echo "")"
        # Try structured export, fall back to individual export
        _agent_key=$(echo "$_keys_json" | jq -r ".\"${_id}\".privateKey // \"\"")
        if [ -z "$_agent_key" ]; then
            _agent_key=$(pulumi stack output "workspace${_pascal}DeployPrivateKey" --show-secrets 2>/dev/null || echo "")
        fi
        export "PROVISION_WORKSPACE_${_upper}_DEPLOY_KEY=${_agent_key}"
        export "PROVISION_OBSIDIAN_${_upper}_VAULT_REPO_URL=$(pulumi config get "obsidian${_pascal}VaultRepoUrl" 2>/dev/null || echo "")"
    done
fi

# Parse agent IDs into array (handles empty string → empty array)
agent_ids=()
if [ -n "$agent_ids_str" ]; then
    IFS=',' read -ra agent_ids <<< "$agent_ids_str"
fi

# Validate required secrets
gateway_token=$(read_env PROVISION_GATEWAY_TOKEN)
claude_setup_token=$(read_env PROVISION_CLAUDE_SETUP_TOKEN)
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
validate_deploy_key "workspace (main)" \
    "$(read_env PROVISION_WORKSPACE_REPO_URL)" \
    "$(read_env PROVISION_WORKSPACE_DEPLOY_KEY)"
for id in "${agent_ids[@]}"; do
    [ -z "$id" ] && continue
    upper=$(echo "$id" | tr '[:lower:]' '[:upper:]')
    validate_deploy_key "workspace ($id)" \
        "$(read_env "PROVISION_WORKSPACE_${upper}_REPO_URL")" \
        "$(read_env "PROVISION_WORKSPACE_${upper}_DEPLOY_KEY")"
done

# Status summary
echo "  gateway_token: set"
echo "  claude_setup_token: set"
echo "  telegram: $([ -n "$(read_env PROVISION_TELEGRAM_BOT_TOKEN)" ] && echo "configured" || echo "skipped")"
echo "  workspace_sync (main): $([ -n "$(read_env PROVISION_WORKSPACE_REPO_URL)" ] && echo "configured" || echo "skipped")"
echo "  grok_search: $([ -n "$(read_env PROVISION_XAI_API_KEY)" ] && echo "configured" || echo "skipped")"
echo "  groq_voice: $([ -n "$(read_env PROVISION_GROQ_API_KEY)" ] && echo "configured" || echo "skipped")"
echo "  github_mcp (main): $([ -n "$(read_env PROVISION_GITHUB_TOKEN)" ] && echo "configured" || echo "skipped")"
echo "  obsidian (andy): $([ -n "$(read_env PROVISION_OBSIDIAN_ANDY_VAULT_REPO_URL)" ] && echo "configured" || echo "skipped")"
echo "  obsidian_headless: $([ -n "$(read_env PROVISION_OBSIDIAN_AUTH_TOKEN)" ] && echo "configured" || echo "skipped")"
for id in "${agent_ids[@]}"; do
    [ -z "$id" ] && continue
    upper=$(echo "$id" | tr '[:lower:]' '[:upper:]')
    [ -n "$(read_env "PROVISION_TELEGRAM_${upper}_USER_ID")" ] && echo "  telegram_${id}: configured"
    [ -n "$(read_env "PROVISION_TELEGRAM_${upper}_GROUP_ID")" ] && echo "  telegram_${id}_group: configured"
    [ -n "$(read_env "PROVISION_WHATSAPP_${upper}_PHONE")" ] && echo "  whatsapp_${id}: configured"
    echo "  workspace_sync ($id): $([ -n "$(read_env "PROVISION_WORKSPACE_${upper}_REPO_URL")" ] && echo "configured" || echo "skipped")"
    echo "  github_mcp ($id): $([ -n "$(read_env "PROVISION_GITHUB_TOKEN_${upper}")" ] && echo "configured" || echo "skipped")"
    [ -n "$(read_env "PROVISION_OBSIDIAN_${upper}_VAULT_REPO_URL")" ] && echo "  obsidian ($id): configured"
done

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
python3 -c "
import json, sys, os

# Static keys: (yaml_key, env_var) — main agent + global config
static = [
    ('gateway_token', 'PROVISION_GATEWAY_TOKEN'),
    ('claude_setup_token', 'PROVISION_CLAUDE_SETUP_TOKEN'),
    ('telegram_bot_token', 'PROVISION_TELEGRAM_BOT_TOKEN'),
    ('telegram_user_id', 'PROVISION_TELEGRAM_USER_ID'),
    ('telegram_group_id', 'PROVISION_TELEGRAM_GROUP_ID'),
    ('workspace_repo_url', 'PROVISION_WORKSPACE_REPO_URL'),
    ('xai_api_key', 'PROVISION_XAI_API_KEY'),
    ('groq_api_key', 'PROVISION_GROQ_API_KEY'),
    ('github_token', 'PROVISION_GITHUB_TOKEN'),
    ('obsidian_andy_vault_repo_url', 'PROVISION_OBSIDIAN_ANDY_VAULT_REPO_URL'),
    ('obsidian_auth_token', 'PROVISION_OBSIDIAN_AUTH_TOKEN'),
    ('obsidian_vault_password', 'PROVISION_OBSIDIAN_VAULT_PASSWORD'),
]

with open(sys.argv[1], 'w') as f:
    f.write('---\n')
    for yaml_key, env_var in static:
        v = os.environ.get(env_var, '')
        f.write(f'{yaml_key}: {json.dumps(v)}\n')

    # Per-agent keys (derived from PROVISION_AGENT_IDS)
    agent_ids = [a.strip() for a in os.environ.get('PROVISION_AGENT_IDS', '').split(',') if a.strip()]
    for aid in agent_ids:
        upper = aid.upper()
        per_agent = [
            (f'github_token_{aid}', f'PROVISION_GITHUB_TOKEN_{upper}'),
            (f'telegram_{aid}_user_id', f'PROVISION_TELEGRAM_{upper}_USER_ID'),
            (f'telegram_{aid}_group_id', f'PROVISION_TELEGRAM_{upper}_GROUP_ID'),
            (f'whatsapp_{aid}_phone', f'PROVISION_WHATSAPP_{upper}_PHONE'),
            (f'workspace_{aid}_repo_url', f'PROVISION_WORKSPACE_{upper}_REPO_URL'),
            (f'obsidian_{aid}_vault_repo_url', f'PROVISION_OBSIDIAN_{upper}_VAULT_REPO_URL'),
        ]
        for yaml_key, env_var in per_agent:
            v = os.environ.get(env_var, '')
            f.write(f'{yaml_key}: {json.dumps(v)}\n')
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
append_deploy_key "workspace_deploy_key" "$(read_env PROVISION_WORKSPACE_DEPLOY_KEY)" "$SECRETS_FILE"
for id in "${agent_ids[@]}"; do
    [ -z "$id" ] && continue
    upper=$(echo "$id" | tr '[:lower:]' '[:upper:]')
    append_deploy_key "workspace_${id}_deploy_key" \
        "$(read_env "PROVISION_WORKSPACE_${upper}_DEPLOY_KEY")" "$SECRETS_FILE"
done

# Append Codex auth credentials (block scalar preserves JSON structure)
append_deploy_key "codex_auth_json" "$codex_auth_json" "$SECRETS_FILE"
chmod 600 "$SECRETS_FILE"

echo "=== Waiting for Tailscale SSH connectivity ==="

tailscale_hostname=$(read_env PROVISION_TAILSCALE_HOSTNAME)
tailscale_hostname="${tailscale_hostname:-openclaw-vps}"

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
    echo "║    ./scripts/provision.sh --tags telegram,whatsapp           ║"
    echo "╚══════════════════════════════════════════════════════════════════╝"
    echo ""
    echo "=== Provisioning complete (with warnings) ==="
    exit 2
else
    echo "=== Provisioning complete ==="
fi
