import * as pulumi from "@pulumi/pulumi";

/**
 * Generates cloud-init user-data script for bootstrapping the OpenClaw server.
 *
 * This script runs on first boot and:
 * 1. Installs Docker with security best practices
 * 2. Installs and authenticates Tailscale
 * 3. Sets up OpenClaw as a systemd service
 * 4. Configures Tailscale Serve to proxy the gateway
 *
 * Security considerations:
 * - Docker socket not exposed to network
 * - OpenClaw binds to localhost only
 * - Tailscale Serve handles external access
 * - No passwords, SSH key only (provided by Pulumi)
 */
export function generateUserData(config: {
    tailscaleAuthKey: pulumi.Output<string>;
    anthropicApiKey: pulumi.Output<string>;
    hostname: string;
}): pulumi.Output<string> {
    return pulumi.all([config.tailscaleAuthKey, config.anthropicApiKey]).apply(
        ([tsKey, anthropicKey]) => `#!/bin/bash
set -euo pipefail

# Logging for debugging
exec > >(tee /var/log/cloud-init-openclaw.log) 2>&1
echo "=== OpenClaw Bootstrap Started: $(date) ==="

# System updates
apt-get update
apt-get upgrade -y
apt-get install -y curl apt-transport-https ca-certificates gnupg lsb-release jq

# ============================================
# Docker Installation
# ============================================
echo "=== Installing Docker ==="

# Add Docker's official GPG key
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

# Add Docker repository
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  tee /etc/apt/sources.list.d/docker.list > /dev/null

apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Enable Docker service
systemctl enable docker
systemctl start docker

# ============================================
# Tailscale Installation
# ============================================
echo "=== Installing Tailscale ==="

curl -fsSL https://tailscale.com/install.sh | sh

# Authenticate Tailscale with the auth key
tailscale up --authkey=${tsKey} --hostname=${config.hostname}

echo "Waiting for Tailscale to connect..."
sleep 10
tailscale status

# ============================================
# OpenClaw Setup
# ============================================
echo "=== Setting up OpenClaw ==="

# Create directory for OpenClaw
mkdir -p /opt/openclaw
cd /opt/openclaw

# Create docker-compose.yml
cat > docker-compose.yml << 'COMPOSE_EOF'
services:
  openclaw:
    image: ghcr.io/anthropics/anthropic-quickstarts:computer-use-demo-latest
    container_name: openclaw
    restart: unless-stopped
    ports:
      # CRITICAL: Bind to localhost ONLY - Tailscale Serve handles external access
      - "127.0.0.1:18789:8080"
    environment:
      - ANTHROPIC_API_KEY=\${ANTHROPIC_API_KEY}
    volumes:
      - openclaw-data:/data
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    read_only: false
    tmpfs:
      - /tmp:mode=1777

volumes:
  openclaw-data:
    driver: local
COMPOSE_EOF

# Create .env file with API key
cat > .env << ENV_EOF
ANTHROPIC_API_KEY=${anthropicKey}
ENV_EOF

# Secure the .env file
chmod 600 .env

# ============================================
# Systemd Service
# ============================================
echo "=== Creating systemd service ==="

cat > /etc/systemd/system/openclaw.service << 'SERVICE_EOF'
[Unit]
Description=OpenClaw Gateway
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/openclaw
ExecStartPre=/usr/bin/docker compose pull
ExecStart=/usr/bin/docker compose up --remove-orphans
ExecStop=/usr/bin/docker compose down
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SERVICE_EOF

systemctl daemon-reload
systemctl enable openclaw
systemctl start openclaw

# ============================================
# Tailscale Serve Configuration
# ============================================
echo "=== Configuring Tailscale Serve ==="

# Wait for OpenClaw to start
echo "Waiting for OpenClaw to start..."
sleep 30

# Configure Tailscale Serve to proxy localhost:18789
# This makes the service available at https://<hostname>.<tailnet>/
tailscale serve --bg https / http://127.0.0.1:18789

echo "=== OpenClaw Bootstrap Complete: $(date) ==="
echo "Access via: https://${config.hostname}.<your-tailnet>.ts.net/"
`
    );
}
