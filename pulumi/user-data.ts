import * as pulumi from "@pulumi/pulumi";

/**
 * Generates cloud-init user-data script for bootstrapping the OpenClaw server.
 *
 * This script runs on first boot and:
 * 1. Creates ubuntu user (Hetzner defaults to root)
 * 2. Installs OpenClaw via official install script (includes Node.js)
 * 3. Installs unattended-upgrades for automatic security patches
 * 4. Installs and authenticates Tailscale
 * 5. Configures OpenClaw as a systemd user service (with built-in Tailscale Serve)
 * 6. Optionally configures Telegram channel and cron jobs
 *
 * Security considerations:
 * - Secrets written to temp files (600 permissions), not CLI args
 * - OpenClaw binds to localhost only
 * - Tailscale Serve handles external access
 * - No passwords, SSH key only (provided by Pulumi)
 * - Runs as unprivileged ubuntu user via systemd user service
 */
export function generateUserData(config: {
    tailscaleAuthKey: pulumi.Output<string>;
    claudeSetupToken: pulumi.Output<string>;
    gatewayToken: pulumi.Output<string>;
    hostname: string;
    telegramBotToken?: pulumi.Output<string>;
    telegramUserId?: string;
    workspaceDeployKey?: pulumi.Output<string>;
    workspaceRepoUrl?: string;
}): pulumi.Output<string> {
    // Handle optional config - use empty strings if not provided
    const telegramBotTokenOutput = config.telegramBotToken || pulumi.output("");
    const telegramUserIdOutput = pulumi.output(config.telegramUserId || "");
    const workspaceDeployKeyOutput = config.workspaceDeployKey || pulumi.output("");
    const workspaceRepoUrlOutput = pulumi.output(config.workspaceRepoUrl || "");

    return pulumi
        .all([config.tailscaleAuthKey, config.claudeSetupToken, config.gatewayToken, telegramBotTokenOutput, telegramUserIdOutput, workspaceDeployKeyOutput, workspaceRepoUrlOutput])
        .apply(
            ([tsKey, setupToken, gatewayToken, telegramBotToken, telegramUserId, workspaceDeployKey, workspaceRepoUrl]) => `#!/bin/bash
set -euo pipefail

# Logging for debugging
exec > >(tee /var/log/cloud-init-openclaw.log) 2>&1
echo "=== OpenClaw Bootstrap Started: $(date) ==="

# ============================================
# System Setup
# ============================================
echo "=== System Updates ==="

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get upgrade -y
apt-get install -y curl apt-transport-https ca-certificates gnupg lsb-release jq python3 git

# Install unattended-upgrades for automatic security patches
DEBIAN_FRONTEND=noninteractive apt-get install -y unattended-upgrades
# Enable automatic updates non-interactively
echo 'Unattended-Upgrade::Automatic-Reboot "false";' > /etc/apt/apt.conf.d/51unattended-upgrades-custom

# ============================================
# Docker Installation (for sandbox support)
# ============================================
echo "=== Installing Docker ==="

curl -fsSL https://get.docker.com | sh
systemctl enable docker
systemctl start docker

# ============================================
# Create ubuntu user (Hetzner defaults to root)
# ============================================
echo "=== Creating ubuntu user ==="

if ! id -u ubuntu >/dev/null 2>&1; then
    useradd -m -s /bin/bash -G sudo,docker ubuntu
    # Copy authorized_keys from root to ubuntu
    mkdir -p /home/ubuntu/.ssh
    cp /root/.ssh/authorized_keys /home/ubuntu/.ssh/
    chown -R ubuntu:ubuntu /home/ubuntu/.ssh
    chmod 700 /home/ubuntu/.ssh
    chmod 600 /home/ubuntu/.ssh/authorized_keys
    # Allow passwordless sudo for ubuntu
    echo "ubuntu ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/ubuntu
fi

# ============================================
# Tailscale Installation
# ============================================
echo "=== Installing Tailscale ==="

curl -fsSL https://tailscale.com/install.sh | sh

# Write auth key to temp file (not CLI argument, not in ps output)
set +x  # Disable command logging
echo "${tsKey}" > /tmp/ts-authkey
chmod 600 /tmp/ts-authkey
set -x

# Authenticate Tailscale using file
tailscale up --authkey=\$(cat /tmp/ts-authkey) --hostname=${config.hostname} --ssh

# Clean up auth key
rm -f /tmp/ts-authkey

echo "Waiting for Tailscale to connect..."
sleep 10
tailscale status

# ============================================
# UFW Firewall (Defense in Depth)
# ============================================
echo "=== Configuring UFW firewall ==="

# Default policies: deny incoming, allow outgoing
ufw default deny incoming
ufw default allow outgoing

# Allow all traffic on Tailscale interface (required for Tailscale access)
ufw allow in on tailscale0

# Enable UFW (--force skips confirmation prompt)
ufw --force enable

# Verify UFW status
ufw status verbose

# ============================================
# OpenClaw Installation (official installer)
# ============================================
echo "=== Installing OpenClaw ==="

# Write setup token to temp file (secure)
set +x  # Disable command logging
echo "${setupToken}" > /tmp/setup-token
chmod 600 /tmp/setup-token
chown ubuntu:ubuntu /tmp/setup-token
set -x

# Install OpenClaw using official install script
# This handles Node.js installation, npm setup, and openclaw binary
sudo -u ubuntu bash << 'OPENCLAW_INSTALL_EOF'
set -euo pipefail
export OPENCLAW_NO_ONBOARD=1
export OPENCLAW_NO_PROMPT=1
curl -fsSL https://openclaw.ai/install.sh | bash
OPENCLAW_INSTALL_EOF

# Add npm global bin dir to PATH for all subsequent commands
# The official installer puts openclaw at ~/.npm-global/bin/openclaw
# but doesn't add it to PATH automatically
# Using /etc/profile.d/ ensures all login shells get the PATH (including non-interactive)
cat > /etc/profile.d/openclaw-path.sh << 'PATHEOF'
export PATH="$HOME/.npm-global/bin:$PATH"
PATHEOF

# Enable user lingering so systemd user services persist
loginctl enable-linger ubuntu

# Start user systemd
systemctl start user@1000.service

# Wait for user systemd to be ready
sleep 5

# ============================================
# OpenClaw Onboarding
# ============================================
echo "=== Running OpenClaw onboard ==="

# Run onboarding as ubuntu user with proper environment
sudo -u ubuntu bash -l << 'ONBOARD_EOF'
set -euo pipefail
export XDG_RUNTIME_DIR=/run/user/1000

# Read token from file (not in process list)
set +x
CLAUDE_SETUP_TOKEN=$(cat /tmp/setup-token)
set -x

# Run onboarding
# Note: setup-token only has user:inference scope (missing user:profile),
# so /status won't show usage tracking. See GitHub issue #4614.
ONBOARD_OUTPUT=\$(openclaw onboard --non-interactive --accept-risk \\
    --mode local \\
    --auth-choice token \\
    --token "\$CLAUDE_SETUP_TOKEN" \\
    --token-provider anthropic \\
    --gateway-port 18789 \\
    --gateway-bind loopback \\
    --skip-daemon \\
    --skip-skills 2>&1) || ONBOARD_EXIT=\$?

# Check if onboarding succeeded or failed with expected "gateway closed" error
if [ -n "\${ONBOARD_EXIT:-}" ] && [ "\$ONBOARD_EXIT" -ne 0 ]; then
    if echo "\$ONBOARD_OUTPUT" | grep -q "gateway closed"; then
        echo "Note: Ignoring expected 'gateway closed' error (daemon not running yet)"
    else
        echo "ERROR: OpenClaw onboarding failed with exit code \$ONBOARD_EXIT"
        echo "\$ONBOARD_OUTPUT"
        exit 1
    fi
fi

# Verify config was created
if [ ! -f ~/.openclaw/openclaw.json ]; then
    echo "ERROR: Onboarding failed - config file not created"
    echo "\$ONBOARD_OUTPUT"
    exit 1
fi
echo "Onboarding completed successfully"
ONBOARD_EOF

# Clean up setup token
rm -f /tmp/setup-token

# ============================================
# Install OpenClaw Daemon
# ============================================
echo "=== Installing OpenClaw daemon ==="

sudo -u ubuntu bash -l << 'DAEMON_EOF'
set -euo pipefail
export XDG_RUNTIME_DIR=/run/user/1000

openclaw daemon install
DAEMON_EOF

# ============================================
# Gateway Configuration
# ============================================
echo "=== Configuring OpenClaw gateway ==="

# Write gateway token to file
set +x
echo "${gatewayToken}" > /tmp/gateway-token
chmod 600 /tmp/gateway-token
chown ubuntu:ubuntu /tmp/gateway-token
set -x

sudo -u ubuntu bash -l << 'GATEWAY_EOF'
set -euo pipefail

# Read gateway token from file
set +x
GATEWAY_TOKEN=$(cat /tmp/gateway-token)
set -x

# Tailscale Serve: let OpenClaw manage the tailscale serve lifecycle
# resetOnExit undoes the serve config when the gateway shuts down
openclaw config set gateway.tailscale.mode serve
openclaw config set gateway.tailscale.resetOnExit true

# Configure security settings for Tailscale Serve access
# trustedProxies: localhost + Tailscale CGNAT range (100.64.0.0/10)
openclaw config set gateway.trustedProxies '["127.0.0.1", "100.64.0.0/10"]'

# Control UI: allowInsecureAuth=false because we use Tailscale identity auth
openclaw config set gateway.controlUi.enabled true
openclaw config set gateway.controlUi.allowInsecureAuth false

# Auth: token mode + allowTailscale for Tailscale identity auth
# allowTailscale lets Tailscale Serve users skip token auth (requires device pairing)
openclaw config set gateway.auth.mode token
set +x
openclaw config set gateway.auth.token "\$GATEWAY_TOKEN"
set -x
openclaw config set gateway.auth.allowTailscale true

# CLI needs remote.token to manage devices (approve pairings, etc.)
set +x
openclaw config set gateway.remote.token "\$GATEWAY_TOKEN"
set -x

# Default model and extended thinking for all sessions and cron jobs
openclaw config set agents.defaults.model.primary "anthropic/claude-opus-4-5"
openclaw config set agents.defaults.thinkingDefault high

echo "Gateway config updated via openclaw config set"
GATEWAY_EOF

# Clean up gateway token
rm -f /tmp/gateway-token

# ============================================
# Start OpenClaw Service
# ============================================
echo "=== Starting OpenClaw service ==="

sudo -u ubuntu bash << 'START_EOF'
set -euo pipefail
export XDG_RUNTIME_DIR=/run/user/1000

# Start the daemon
systemctl --user start openclaw-gateway
systemctl --user enable openclaw-gateway

# Verify service actually started
sleep 5
if ! systemctl --user is-active openclaw-gateway > /dev/null 2>&1; then
    echo "ERROR: openclaw-gateway service failed to start"
    systemctl --user status openclaw-gateway || true
    journalctl --user -u openclaw-gateway -n 50 --no-pager || true
    exit 1
fi
echo "OpenClaw gateway service started successfully"
START_EOF

# ============================================
# Telegram Channel Configuration
# ============================================
echo "=== Configuring Telegram channel ==="

# Write Telegram credentials to temp files (secure)
set +x  # Disable command logging
TELEGRAM_BOT_TOKEN="${telegramBotToken}"
TELEGRAM_USER_ID="${telegramUserId}"
if [ -n "\$TELEGRAM_BOT_TOKEN" ]; then
    echo "\$TELEGRAM_BOT_TOKEN" > /tmp/telegram-bot-token
    chmod 600 /tmp/telegram-bot-token
    chown ubuntu:ubuntu /tmp/telegram-bot-token
fi
echo "\$TELEGRAM_USER_ID" > /tmp/telegram-user-id
chmod 600 /tmp/telegram-user-id
chown ubuntu:ubuntu /tmp/telegram-user-id
set -x

sudo -u ubuntu bash -l << 'TELEGRAM_EOF'
set -euo pipefail

# Read user ID from file
TELEGRAM_USER_ID=""
if [ -f /tmp/telegram-user-id ]; then
    TELEGRAM_USER_ID=$(cat /tmp/telegram-user-id)
fi

# Configure Telegram (only if bot token is provided)
if [ -f /tmp/telegram-bot-token ]; then
    set +x
    BOT_TOKEN=$(cat /tmp/telegram-bot-token)
    set -x

    openclaw config set channels.telegram.enabled true
    set +x
    openclaw config set channels.telegram.botToken "$BOT_TOKEN"
    set -x
    openclaw config set channels.telegram.dmPolicy "allowlist"
    if [ -n "$TELEGRAM_USER_ID" ]; then
        openclaw config set channels.telegram.allowFrom "[$TELEGRAM_USER_ID]"
    fi
    echo "Telegram channel configured"
else
    echo "Skipping Telegram (no bot token provided)"
fi
TELEGRAM_EOF

# Clean up Telegram token
rm -f /tmp/telegram-bot-token

# ============================================
# Cron Jobs Configuration
# ============================================
echo "=== Configuring scheduled tasks ==="

sudo -u ubuntu bash -l << 'CRON_EOF'
set -euo pipefail
export XDG_RUNTIME_DIR=/run/user/1000

# Read user ID from file
TELEGRAM_USER_ID=""
if [ -f /tmp/telegram-user-id ]; then
    TELEGRAM_USER_ID=$(cat /tmp/telegram-user-id)
fi

# Remove existing jobs by name to avoid duplicates on redeploy
# (openclaw cron add always creates a new job, even if the name exists)
remove_cron_by_name() {
    local name="$1"
    local ids
    ids=$(openclaw cron list --json 2>/dev/null | jq -r --arg n "$name" '.jobs[] | select(.name == $n) | .id' 2>/dev/null)
    for id in $ids; do
        openclaw cron remove "$id" || true
    done
}

# Only add cron jobs if Telegram user ID is configured
if [ -n "$TELEGRAM_USER_ID" ]; then
    remove_cron_by_name "Morning Digest"
    remove_cron_by_name "Evening Review"
    remove_cron_by_name "Night Shift"
    remove_cron_by_name "Weekly Planning"

    # Morning Digest at 09:30
    openclaw cron add \
        --name "Morning Digest" \
        --cron "30 9 * * *" \
        --tz "Europe/Berlin" \
        --session isolated \
        --message "Good morning! Summarize what needs my attention today." \
        --deliver --channel telegram --to "$TELEGRAM_USER_ID" || true

    # Evening Review at 19:30
    openclaw cron add \
        --name "Evening Review" \
        --cron "30 19 * * *" \
        --tz "Europe/Berlin" \
        --session isolated \
        --message "Evening review. What was accomplished? What is pending?" \
        --deliver --channel telegram --to "$TELEGRAM_USER_ID" || true

    # Night Shift at 23:00
    openclaw cron add \
        --name "Night Shift" \
        --cron "0 23 * * *" \
        --tz "Europe/Berlin" \
        --session isolated \
        --message "Night shift. Review notes, organize, triage, work through tasks. Prepare morning summary." \
        --deliver --channel telegram --to "$TELEGRAM_USER_ID" || true

    # Weekly Planning Sunday 18:00
    openclaw cron add \
        --name "Weekly Planning" \
        --cron "0 18 * * 0" \
        --tz "Europe/Berlin" \
        --session isolated \
        --message "Weekly planning. Review past week, plan upcoming week priorities." \
        --deliver --channel telegram --to "$TELEGRAM_USER_ID" || true

    echo "Cron jobs configured"
    openclaw cron list
else
    echo "Skipping cron jobs (no Telegram user ID)"
fi
CRON_EOF

# Clean up telegram user ID file
rm -f /tmp/telegram-user-id

# ============================================
# Workspace Git Sync
# ============================================
echo "=== Configuring workspace git sync ==="

WORKSPACE_REPO_URL="${workspaceRepoUrl}"
if [ -n "\$WORKSPACE_REPO_URL" ]; then
    # Write deploy key and repo URL to temp files
    set +x
    echo "${workspaceDeployKey}" > /tmp/workspace-deploy-key
    chmod 600 /tmp/workspace-deploy-key
    chown ubuntu:ubuntu /tmp/workspace-deploy-key
    echo "\$WORKSPACE_REPO_URL" > /tmp/workspace-repo-url
    chmod 600 /tmp/workspace-repo-url
    chown ubuntu:ubuntu /tmp/workspace-repo-url
    set -x

    sudo -u ubuntu bash -l << 'GITSYNC_EOF'
set -euo pipefail

WORKSPACE_DIR="$HOME/.openclaw/workspace"
DEPLOY_KEY="$HOME/.ssh/workspace-deploy-key"

# Install deploy key
mkdir -p ~/.ssh
cp /tmp/workspace-deploy-key "$DEPLOY_KEY"
chmod 600 "$DEPLOY_KEY"

# Configure SSH to use deploy key for github.com
# Uses a Host alias to avoid conflicts with other github.com keys
cat >> ~/.ssh/config << 'SSHCONF'
Host github-workspace
    HostName github.com
    User git
    IdentityFile ~/.ssh/workspace-deploy-key
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
SSHCONF
chmod 600 ~/.ssh/config

# Configure git identity
git config --global user.name "OpenClaw Agent"
git config --global user.email "openclaw@localhost"

# Initialize workspace as git repo
if [ -d "$WORKSPACE_DIR" ]; then
    cd "$WORKSPACE_DIR"

    if [ ! -d ".git" ]; then
        git init -b main
    fi

    # Rewrite remote origin to use the SSH alias
    REPO_URL=$(cat /tmp/workspace-repo-url)
    # Replace github.com with github-workspace alias
    ALIAS_URL=$(echo "$REPO_URL" | sed 's/github\.com/github-workspace/')
    git remote remove origin 2>/dev/null || true
    git remote add origin "$ALIAS_URL"

    # Create .gitignore
    cat > .gitignore << 'GITIGNORE'
# OS files
.DS_Store
Thumbs.db

# Temp files
*.tmp
*.swp
*~
GITIGNORE

    # Initial commit and push
    git add -A
    git commit -m "Initial workspace sync" || true
    git push -u origin main || echo "Warning: initial push failed (repo may not exist yet)"

    echo "Workspace git repo initialized"
else
    echo "Warning: workspace directory not found at $WORKSPACE_DIR"
fi

# Install sync script
mkdir -p ~/.local/bin
cat > ~/.local/bin/workspace-git-sync.sh << 'SYNCSCRIPT'
#!/bin/bash
set -euo pipefail
cd ~/.openclaw/workspace
git add -A
if ! git diff --cached --quiet; then
    git commit -m "Auto-sync: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    git push origin main
fi
SYNCSCRIPT
chmod +x ~/.local/bin/workspace-git-sync.sh

# Install systemd timer for hourly sync
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/workspace-git-sync.service << 'SVCUNIT'
[Unit]
Description=Sync OpenClaw workspace to git

[Service]
Type=oneshot
ExecStart=%h/.local/bin/workspace-git-sync.sh
Environment=SSH_AUTH_SOCK=
SVCUNIT

cat > ~/.config/systemd/user/workspace-git-sync.timer << 'TIMERUNIT'
[Unit]
Description=Hourly workspace git sync

[Timer]
OnCalendar=hourly
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
TIMERUNIT

export XDG_RUNTIME_DIR=/run/user/1000
systemctl --user daemon-reload
systemctl --user enable workspace-git-sync.timer
systemctl --user start workspace-git-sync.timer

echo "Workspace git sync timer installed (hourly)"
GITSYNC_EOF

    # Clean up temp files
    rm -f /tmp/workspace-deploy-key /tmp/workspace-repo-url
else
    echo "Skipping workspace git sync (no repo URL configured)"
fi

echo "=== OpenClaw Bootstrap Complete: $(date) ==="
echo "Access via: https://${config.hostname}.<your-tailnet>.ts.net/"
echo ""
echo "SECURITY NOTE: After verifying deployment works, clean up cloud-init log:"
echo "  sudo shred -u /var/log/cloud-init-openclaw.log"
`
        );
}
