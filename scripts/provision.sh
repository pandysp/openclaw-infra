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
    printf '%s' "${!1-}"
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

    # Read each complete snapshot once. A failed CLI read is never an absent
    # optional setting, even if the failed command emitted plausible JSON.
    if ! _config_json=$(pulumi config --json --show-secrets --non-interactive 2>/dev/null |
        jq -ces 'if length == 1 and (.[0] | type == "object") then .[0] else error("Invalid config snapshot") end' 2>/dev/null); then
        echo "ERROR: Could not read Pulumi config. Check login, backend and passphrase; raw output withheld."
        exit 1
    fi
    if ! _outputs_json=$(pulumi stack output --json --show-secrets --non-interactive 2>/dev/null |
        jq -ces 'if length == 1 and (.[0] | type == "object") then .[0] else error("Invalid output snapshot") end' 2>/dev/null); then
        echo "ERROR: Could not read Pulumi stack outputs. Check the selected stack and backend; raw output withheld."
        exit 1
    fi

    config_value() {
        printf '%s' "$_config_json" | jq -er --arg key "openclaw-infra:$1" '
            if has($key) then
                .[$key] | if type == "object" and (.value | type == "string")
                    then .value else error("Invalid config value") end
            else "" end' 2>/dev/null || {
            echo "ERROR: Pulumi config $1 must contain a string value; raw output withheld." >&2
            return 1
        }
    }
    workspace_key() {
        # Individual exports are supported only when the structured field is absent.
        printf '%s' "$_outputs_json" | jq -er --arg id "$1" --arg legacy "$2" '
            . as $outputs |
            (if has("agentWorkspaceKeys") then .agentWorkspaceKeys else {} end) |
            if type != "object" then error("Invalid workspace keys") else . end |
            (if has($id) then .[$id] else {} end) |
            if type != "object" then error("Invalid workspace key") else . end |
            (if has("privateKey") then .privateKey
             elif $outputs | has($legacy) then $outputs[$legacy] else "" end) |
            if type == "string" then . else error("Invalid private key") end' 2>/dev/null || {
            echo "ERROR: Invalid Pulumi workspace key for $1; raw output withheld." >&2
            return 1
        }
    }

    if [ -z "$agent_ids_str" ]; then
        agent_ids_str=$(config_value agentIds)
    fi
    PROVISION_GATEWAY_TOKEN=$(printf '%s' "$_outputs_json" | jq -er '.openclawGatewayToken | strings | select(length > 0)') || {
        echo "ERROR: Pulumi gateway token is missing or invalid."
        exit 1
    }
    PROVISION_TAILSCALE_HOSTNAME=$(printf '%s' "$_outputs_json" | jq -er '.tailscaleHostname | strings | select(length > 0)') || {
        echo "ERROR: Pulumi provisioning hostname is missing or invalid. Refusing a default host."
        exit 1
    }
    PROVISION_CLAUDE_SETUP_TOKEN=$(config_value claudeSetupToken)
    PROVISION_CLAUDE_OAUTH_CREDENTIALS=$(config_value claudeOAuthCredentials)
    PROVISION_TELEGRAM_BOT_TOKEN=$(config_value telegramBotToken)
    PROVISION_TELEGRAM_USER_ID=$(config_value telegramUserId)
    PROVISION_TELEGRAM_GROUP_ID=$(config_value telegramGroupId)
    PROVISION_WORKSPACE_REPO_URL=$(config_value workspaceRepoUrl)
    PROVISION_XAI_API_KEY=$(config_value xaiApiKey)
    PROVISION_GROQ_API_KEY=$(config_value groqApiKey)
    PROVISION_GEMINI_API_KEY=$(config_value geminiApiKey)
    PROVISION_GITHUB_TOKEN=$(config_value githubToken)
    PROVISION_OBSIDIAN_AUTH_TOKEN=$(config_value obsidianAuthToken)
    PROVISION_OBSIDIAN_VAULT_PASSWORD=$(config_value obsidianVaultPassword)
    PROVISION_DISCORD_BOT_TOKEN=$(config_value discordBotToken)
    PROVISION_DISCORD_GUILD_ID=$(config_value discordGuildId)
    PROVISION_DISCORD_USER_ID=$(config_value discordUserId)
    PROVISION_WORKSPACE_DEPLOY_KEY=$(workspace_key main workspaceDeployPrivateKey)
    export PROVISION_GATEWAY_TOKEN PROVISION_TAILSCALE_HOSTNAME \
        PROVISION_CLAUDE_SETUP_TOKEN PROVISION_CLAUDE_OAUTH_CREDENTIALS \
        PROVISION_TELEGRAM_BOT_TOKEN PROVISION_TELEGRAM_USER_ID PROVISION_TELEGRAM_GROUP_ID \
        PROVISION_WORKSPACE_REPO_URL PROVISION_WORKSPACE_DEPLOY_KEY \
        PROVISION_XAI_API_KEY PROVISION_GROQ_API_KEY PROVISION_GEMINI_API_KEY PROVISION_GITHUB_TOKEN \
        PROVISION_OBSIDIAN_AUTH_TOKEN PROVISION_OBSIDIAN_VAULT_PASSWORD \
        PROVISION_DISCORD_BOT_TOKEN PROVISION_DISCORD_GUILD_ID PROVISION_DISCORD_USER_ID

    export PROVISION_AGENT_IDS="$agent_ids_str"
    IFS=',' read -ra _cli_agents <<< "$agent_ids_str"
    for _id in "${_cli_agents[@]}"; do
        [ -z "$_id" ] && continue
        [[ "$_id" =~ ^[a-zA-Z][a-zA-Z0-9_]*$ ]] || {
            echo "ERROR: Agent IDs must be valid environment-variable suffixes."
            exit 1
        }
        _upper=$(echo "$_id" | tr '[:lower:]' '[:upper:]')
        _pascal=$(echo "$_id" | awk '{print toupper(substr($0,1,1)) substr($0,2)}')
        _value=$(config_value "githubToken${_pascal}")
        export "PROVISION_GITHUB_TOKEN_${_upper}=$_value"
        _value=$(config_value "telegram${_pascal}UserId")
        export "PROVISION_TELEGRAM_${_upper}_USER_ID=$_value"
        _value=$(config_value "telegram${_pascal}GroupId")
        export "PROVISION_TELEGRAM_${_upper}_GROUP_ID=$_value"
        _value=$(config_value "whatsapp${_pascal}Phone")
        export "PROVISION_WHATSAPP_${_upper}_PHONE=$_value"
        _value=$(config_value "workspace${_pascal}RepoUrl")
        export "PROVISION_WORKSPACE_${_upper}_REPO_URL=$_value"
        _value=$(workspace_key "$_id" "workspace${_pascal}DeployPrivateKey")
        export "PROVISION_WORKSPACE_${_upper}_DEPLOY_KEY=$_value"
    done
    unset _config_json _outputs_json _value
fi

# Phoenix may never provision a default host or another run's Tailscale peer.
if [[ -n "${PHOENIX_RESOURCE_NAME:-}" || -n "${STAGING_HOST:-}" ]]; then
    if [[ ! "${PHOENIX_RESOURCE_NAME:-}" =~ ^openclaw-staging-[0-9]+-[0-9]+$ ]] ||
       [[ "${PROVISION_TAILSCALE_HOSTNAME:-}" != "$PHOENIX_RESOURCE_NAME" ]]; then
        echo "ERROR: Provisioning hostname does not match this Phoenix run."
        exit 1
    fi
    if [[ -n "${STAGING_HOST:-}" ]] &&
       { [[ ! "$STAGING_HOST" =~ ^openclaw-staging-[0-9]+-[0-9]+\.[a-zA-Z0-9.-]+$ ]] ||
         [[ "${STAGING_HOST%%.*}" != "$PHOENIX_RESOURCE_NAME" ]]; }; then
        echo "ERROR: STAGING_HOST does not match this Phoenix run."
        exit 1
    fi
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
claude_oauth_credentials=$(read_env PROVISION_CLAUDE_OAUTH_CREDENTIALS)
if [ -z "$claude_setup_token" ] && [ -z "$claude_oauth_credentials" ]; then
    echo "ERROR: neither claudeSetupToken nor claudeOAuthCredentials is set. At least one is required."
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
if [ -n "$agent_ids_str" ]; then
    for id in "${agent_ids[@]}"; do
        [ -z "$id" ] && continue
        upper=$(echo "$id" | tr '[:lower:]' '[:upper:]')
        validate_deploy_key "workspace ($id)" \
            "$(read_env "PROVISION_WORKSPACE_${upper}_REPO_URL")" \
            "$(read_env "PROVISION_WORKSPACE_${upper}_DEPLOY_KEY")"
    done
fi

# Status summary
echo "  gateway_token: set"
echo "  claude_setup_token: $([ -n "$claude_setup_token" ] && echo "set" || echo "skipped")"
echo "  claude_oauth: $([ -n "$claude_oauth_credentials" ] && echo "set" || echo "skipped")"
echo "  telegram: $([ -n "$(read_env PROVISION_TELEGRAM_BOT_TOKEN)" ] && echo "configured" || echo "skipped")"
echo "  discord: $([ -n "$(read_env PROVISION_DISCORD_BOT_TOKEN)" ] && echo "configured" || echo "skipped")"
echo "  workspace_sync (main): $([ -n "$(read_env PROVISION_WORKSPACE_REPO_URL)" ] && echo "configured" || echo "skipped")"
echo "  grok_search: $([ -n "$(read_env PROVISION_XAI_API_KEY)" ] && echo "configured" || echo "skipped")"
echo "  groq_voice: $([ -n "$(read_env PROVISION_GROQ_API_KEY)" ] && echo "configured" || echo "skipped")"
echo "  gemini_image: $([ -n "$(read_env PROVISION_GEMINI_API_KEY)" ] && echo "configured" || echo "skipped")"
echo "  github_mcp (main): $([ -n "$(read_env PROVISION_GITHUB_TOKEN)" ] && echo "configured" || echo "skipped")"
echo "  obsidian_headless: $([ -n "$(read_env PROVISION_OBSIDIAN_AUTH_TOKEN)" ] && echo "configured" || echo "skipped")"
if [ -n "$agent_ids_str" ]; then
    for id in "${agent_ids[@]}"; do
        [ -z "$id" ] && continue
        upper=$(echo "$id" | tr '[:lower:]' '[:upper:]')
        [ -n "$(read_env "PROVISION_TELEGRAM_${upper}_USER_ID")" ] && echo "  telegram_${id}: configured"
        [ -n "$(read_env "PROVISION_TELEGRAM_${upper}_GROUP_ID")" ] && echo "  telegram_${id}_group: configured"
        [ -n "$(read_env "PROVISION_WHATSAPP_${upper}_PHONE")" ] && echo "  whatsapp_${id}: configured"
        echo "  workspace_sync ($id): $([ -n "$(read_env "PROVISION_WORKSPACE_${upper}_REPO_URL")" ] && echo "configured" || echo "skipped")"
        echo "  github_mcp ($id): $([ -n "$(read_env "PROVISION_GITHUB_TOKEN_${upper}")" ] && echo "configured" || echo "skipped")"
    done
fi

# Ansible accepts JSON extra-vars; one serializer handles all credentials.
SECRETS_FILE="$SECRETS_DIR/secrets.json"
install -m 600 /dev/null "$SECRETS_FILE"
python3 -c "
import json, sys, os
from pathlib import Path

# Static keys: (variable, env_var) — main agent + global config
static = [
    ('gateway_token', 'PROVISION_GATEWAY_TOKEN'),
    ('claude_setup_token', 'PROVISION_CLAUDE_SETUP_TOKEN'),
    ('claude_oauth_credentials', 'PROVISION_CLAUDE_OAUTH_CREDENTIALS'),
    ('telegram_bot_token', 'PROVISION_TELEGRAM_BOT_TOKEN'),
    ('telegram_user_id', 'PROVISION_TELEGRAM_USER_ID'),
    ('telegram_group_id', 'PROVISION_TELEGRAM_GROUP_ID'),
    ('workspace_repo_url', 'PROVISION_WORKSPACE_REPO_URL'),
    ('workspace_deploy_key', 'PROVISION_WORKSPACE_DEPLOY_KEY'),
    ('xai_api_key', 'PROVISION_XAI_API_KEY'),
    ('groq_api_key', 'PROVISION_GROQ_API_KEY'),
    ('gemini_api_key', 'PROVISION_GEMINI_API_KEY'),
    ('github_token', 'PROVISION_GITHUB_TOKEN'),
    ('obsidian_auth_token', 'PROVISION_OBSIDIAN_AUTH_TOKEN'),
    ('obsidian_vault_password', 'PROVISION_OBSIDIAN_VAULT_PASSWORD'),
    ('discord_bot_token', 'PROVISION_DISCORD_BOT_TOKEN'),
    ('discord_guild_id', 'PROVISION_DISCORD_GUILD_ID'),
    ('discord_user_id', 'PROVISION_DISCORD_USER_ID'),
]

# Per-agent keys (derived from PROVISION_AGENT_IDS)
agent_ids = [a.strip() for a in os.environ.get('PROVISION_AGENT_IDS', '').split(',') if a.strip()]
for aid in agent_ids:
    upper = aid.upper()
    static.extend([
        (f'github_token_{aid}', f'PROVISION_GITHUB_TOKEN_{upper}'),
        (f'telegram_{aid}_user_id', f'PROVISION_TELEGRAM_{upper}_USER_ID'),
        (f'telegram_{aid}_group_id', f'PROVISION_TELEGRAM_{upper}_GROUP_ID'),
        (f'whatsapp_{aid}_phone', f'PROVISION_WHATSAPP_{upper}_PHONE'),
        (f'workspace_{aid}_repo_url', f'PROVISION_WORKSPACE_{upper}_REPO_URL'),
        (f'workspace_{aid}_deploy_key', f'PROVISION_WORKSPACE_{upper}_DEPLOY_KEY'),
    ])
secrets = {key: os.environ.get(env_var, '') for key, env_var in static}

# Run codex login locally to provide these optional credentials.
codex_path = Path.home() / '.codex/auth.json'
secrets['codex_auth_json'] = codex_path.read_text() if codex_path.exists() else ''
if codex_path.exists():
    try:
        json.loads(secrets['codex_auth_json'])
    except ValueError:
        sys.exit('ERROR: ~/.codex/auth.json is not valid JSON. Run codex login to regenerate.')
print('  codex_auth: ' + ('found (~/.codex/auth.json)' if codex_path.exists() else 'skipped (run codex login to enable)'))
with open(sys.argv[1], 'w') as f:
    json.dump(secrets, f)
    f.write('\n')
" "$SECRETS_FILE"

echo "=== Resolving authenticated Tailscale peer ==="

tailscale_hostname=$(read_env PROVISION_TAILSCALE_HOSTNAME)
[[ "$tailscale_hostname" =~ ^[a-zA-Z0-9][a-zA-Z0-9-]*$ ]] || {
    echo "ERROR: Missing or invalid provisioning hostname; refusing a default host."
    exit 1
}

# Resolve exactly one peer for production and staging alike. Only an absent or
# offline peer may still be booting; malformed/ambiguous data fails immediately.
# SSH readiness itself belongs to the playbook's wait_for_connection task.
MAX_RETRIES=30
for i in $(seq 1 "$MAX_RETRIES"); do
    PEER=$(tailscale status --json | jq -ces --arg name "$tailscale_hostname" '
        if length != 1 then error("Expected one Tailscale status") else .[0] end |
        if type != "object" or (.Peer | type != "object") or
           (.MagicDNSSuffix | type != "string" or length == 0)
        then error("Invalid Tailscale status") else . end |
        .MagicDNSSuffix as $suffix | [.Peer[] | select(.HostName == $name)] |
        if length == 0 then {} elif length != 1 then error("Ambiguous peer")
        elif .[0].DNSName != ($name + "." + $suffix + ".") then error("Peer identity mismatch")
        elif .[0].Online == false then {}
        elif .[0].Online == true then
            if .[0].sshHostKeys == null or .[0].sshHostKeys == [] then {} else .[0] end
        else error("Missing peer online state") end') || {
        echo "ERROR: Could not resolve the exact authenticated Tailscale peer."
        exit 1
    }
    if [[ "$PEER" != '{}' ]]; then break; fi
    if [[ "$i" -eq "$MAX_RETRIES" ]]; then
        echo "ERROR: Exact Tailscale peer did not come online after $MAX_RETRIES attempts."
        exit 1
    fi
    echo "Waiting for Tailscale peer... (attempt $i/$MAX_RETRIES)"
    sleep 10
done
OPENCLAW_SSH_HOST=$(jq -er '.DNSName | rtrimstr(".")' <<< "$PEER")
if [[ -n "${STAGING_HOST:-}" && "$OPENCLAW_SSH_HOST" != "$STAGING_HOST" ]]; then
    echo "ERROR: Resolved peer differs from the pinned staging host."
    exit 1
fi
OPENCLAW_SSH_KNOWN_HOSTS="$SECRETS_DIR/known_hosts"
install -m 600 /dev/null "$OPENCLAW_SSH_KNOWN_HOSTS"
jq -er --arg host "$OPENCLAW_SSH_HOST" '
    .sshHostKeys | if type == "array" and length > 0 and
       all(.[]; type == "string" and test("^(ssh-ed25519|ssh-rsa|ecdsa-sha2-[a-zA-Z0-9-]+) [A-Za-z0-9+/=]+$"))
    then .[] | $host + " " + . else error("Missing or invalid authenticated SSH host keys") end
' <<< "$PEER" > "$OPENCLAW_SSH_KNOWN_HOSTS" || {
    echo "ERROR: Could not authenticate Ansible SSH host keys; provisioning was not started."
    exit 1
}
export OPENCLAW_SSH_HOST OPENCLAW_SSH_KNOWN_HOSTS

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

# Collection installation must not inherit deployment or backend credentials.
env -i HOME="$HOME" PATH="$PATH" ansible-galaxy collection install -r requirements.yml --upgrade || {
    echo "ERROR: Failed to install Ansible Galaxy collections."
    exit 1
}

ansible-playbook playbook.yml \
    -e "@$SECRETS_FILE" \
    "$@"

echo "=== Provisioning complete ==="
