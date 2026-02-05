import * as pulumi from "@pulumi/pulumi";

/**
 * Generates cloud-init user-data for minimal bootstrap.
 *
 * Only handles what must happen before Ansible can connect:
 * 1. Creates ubuntu user with sudo (Hetzner defaults to root)
 * 2. Installs Tailscale
 * 3. Joins the tailnet with SSH enabled
 *
 * All other provisioning (Docker, OpenClaw, config, Telegram, workspace)
 * is handled by Ansible via scripts/provision.sh.
 */
export function generateUserData(config: {
    tailscaleAuthKey: pulumi.Output<string>;
    hostname: string;
}): pulumi.Output<string> {
    return config.tailscaleAuthKey.apply(
        (tsKey) => `#!/bin/bash
set -euo pipefail

exec > >(tee /var/log/cloud-init-openclaw.log) 2>&1
echo "=== OpenClaw Bootstrap Started: $(date) ==="

# ============================================
# Create ubuntu user (Hetzner defaults to root)
# ============================================
echo "=== Creating ubuntu user ==="

if ! id -u ubuntu >/dev/null 2>&1; then
    useradd -m -s /bin/bash -G sudo ubuntu
    mkdir -p /home/ubuntu/.ssh
    cp /root/.ssh/authorized_keys /home/ubuntu/.ssh/
    chown -R ubuntu:ubuntu /home/ubuntu/.ssh
    chmod 700 /home/ubuntu/.ssh
    chmod 600 /home/ubuntu/.ssh/authorized_keys
    echo "ubuntu ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/ubuntu
fi

# ============================================
# Install Tailscale
# ============================================
echo "=== Installing Tailscale ==="

curl -fsSL https://tailscale.com/install.sh | sh

set +x
echo "${tsKey}" > /tmp/ts-authkey
chmod 600 /tmp/ts-authkey
set -x

tailscale up --authkey=$(cat /tmp/ts-authkey) --hostname=${config.hostname} --ssh

rm -f /tmp/ts-authkey

echo "Waiting for Tailscale to connect..."
sleep 10
tailscale status

echo "=== Bootstrap Complete: $(date) ==="
echo "Server is ready for Ansible provisioning via Tailscale SSH."
`
    );
}
