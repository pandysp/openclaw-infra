import * as pulumi from "@pulumi/pulumi";
import * as command from "@pulumi/command";
import * as random from "@pulumi/random";
import * as tls from "@pulumi/tls";
import { createFirewall } from "./firewall";
import { createServer } from "./server";
import { generateUserData } from "./user-data";

// Load configuration
const config = new pulumi.Config();

// Required secrets (set via `pulumi config set --secret`)
const tailscaleAuthKey = config.requireSecret("tailscaleAuthKey");
const claudeSetupToken = config.requireSecret("claudeSetupToken");

// Optional Telegram configuration (set via `pulumi config set`)
const telegramBotToken = config.getSecret("telegramBotToken");
const telegramUserId = config.get("telegramUserId");

// Optional workspace git sync (set via `pulumi config set`)
const workspaceRepoUrl = config.get("workspaceRepoUrl");

// Tailscale configuration
// Find your tailnet name at: https://login.tailscale.com/admin/dns
const tailnetDnsName = config.get("tailnetDnsName") || "";

// Server configuration (with defaults)
const serverName = config.get("serverName") || "openclaw-vps";
const serverType = config.get("serverType") || "cx33"; // x86, 4 vCPU, 8GB RAM, ~€5.49/mo
const serverLocation = config.get("serverLocation") || "fsn1"; // Frankfurt
const serverImage = config.get("serverImage") || "ubuntu-24.04";

// ============================================
// Security: Generate Gateway Token
// ============================================

// Generate a random gateway token (simpler than ED25519 key derivation, equally secure)
const gatewayToken = new random.RandomPassword("openclaw-gateway-token", {
    length: 48,
    special: false,
});

// ============================================
// Workspace Git Sync: Deploy Key
// ============================================

// Generate ED25519 deploy key for pushing workspace to a private GitHub repo
// Only meaningful if workspaceRepoUrl is configured, but always generated
// so the public key can be retrieved before the first deploy
const workspaceDeployKey = new tls.PrivateKey("workspace-deploy-key", {
    algorithm: "ED25519",
});

// ============================================
// Infrastructure Resources
// ============================================

// 1. Create firewall (no inbound, all outbound)
const firewall = createFirewall("openclaw-firewall");

// 2. Generate cloud-init user-data (Tailscale-only bootstrap)
const userData = generateUserData({
    tailscaleAuthKey,
    hostname: serverName,
});

// 3. Create the server with attached firewall
const { server, sshKey, privateKey } = createServer({
    name: serverName,
    serverType,
    location: serverLocation,
    image: serverImage,
    userData,
    firewallId: firewall.id.apply((id) => parseInt(id)),
});

// ============================================
// Ansible Provisioning (auto-triggered on server replacement)
// ============================================

const ansibleProvision = new command.local.Command(
    "ansible-provision",
    {
        create: pulumi.interpolate`cd ${__dirname}/.. && ./scripts/provision.sh`,
        environment: {
            PULUMI_CONFIG_PASSPHRASE:
                process.env.PULUMI_CONFIG_PASSPHRASE || "",
        },
        // Re-run Ansible whenever the server is replaced
        triggers: [server.id],
    },
    { dependsOn: [server] }
);

// ============================================
// Exports
// ============================================

// Server details
export const serverId = server.id;
export const serverIpv4 = server.ipv4Address;
export const serverIpv6 = server.ipv6Address;
export const serverStatus = server.status;

// SSH key (for Tailscale SSH fallback)
export const sshKeyId = sshKey.id;
export const sshPrivateKey = pulumi.secret(privateKey);

// Firewall
export const firewallId = firewall.id;

// Gateway token (for client configuration)
export const openclawGatewayToken = pulumi.secret(gatewayToken.result);

// Workspace deploy keys
export const workspaceDeployPublicKey = workspaceDeployKey.publicKeyOpenssh;
export const workspaceDeployPrivateKey = pulumi.secret(
    workspaceDeployKey.privateKeyOpenssh
);

// Access information
export const tailscaleHostname = pulumi.interpolate`${serverName}`;

// If tailnetDnsName is configured, output a ready-to-use URL with token
// Otherwise show placeholder for manual substitution
export const accessUrl = tailnetDnsName
    ? pulumi.interpolate`https://${serverName}.${tailnetDnsName}/`
    : pulumi.interpolate`https://${serverName}.<your-tailnet>.ts.net/`;

// One-click URL with embedded token (only if tailnetDnsName is configured)
export const tailscaleUrlWithToken = tailnetDnsName
    ? pulumi.interpolate`https://${serverName}.${tailnetDnsName}/?token=${gatewayToken.result}`
    : pulumi.interpolate`https://${serverName}.<your-tailnet>.ts.net/?token=<run: pulumi stack output openclawGatewayToken --show-secrets>`;

// Instructions
export const postDeploymentInstructions = pulumi.interpolate`
╔══════════════════════════════════════════════════════════════════╗
║                    OpenClaw Deployment Complete                  ║
╠══════════════════════════════════════════════════════════════════╣
║                                                                  ║
║  Server: ${serverName}                                           ║
║  IPv4: ${server.ipv4Address}                                     ║
║                                                                  ║
║  Cloud-init installs Tailscale only (~1 minute).                 ║
║  Ansible provisioning runs automatically after.                  ║
║                                                                  ║
║  Day-2 operations:                                               ║
║  ./scripts/provision.sh                     # Full provision     ║
║  ./scripts/provision.sh --tags config       # Config only        ║
║  ./scripts/provision.sh --check --diff      # Dry run            ║
║                                                                  ║
║  Access:                                                         ║
║  Web UI: https://${serverName}.<tailnet>.ts.net/                 ║
║  SSH: ssh ubuntu@${serverName}.<tailnet>.ts.net                  ║
║                                                                  ║
║  Verification:                                                   ║
║  ./scripts/verify.sh                                             ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
`;
