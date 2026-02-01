import * as pulumi from "@pulumi/pulumi";
import { createFirewall } from "./firewall";
import { createServer } from "./server";
import { generateUserData } from "./user-data";

// Load configuration
const config = new pulumi.Config();

// Required secrets (set via `pulumi config set --secret`)
const tailscaleAuthKey = config.requireSecret("tailscaleAuthKey");
const anthropicApiKey = config.requireSecret("anthropicApiKey");

// Server configuration (with defaults)
const serverName = config.get("serverName") || "openclaw-vps";
const serverType = config.get("serverType") || "cax11"; // ARM, €4.51/mo
const serverLocation = config.get("serverLocation") || "fsn1"; // Frankfurt
const serverImage = config.get("serverImage") || "ubuntu-24.04";

// ============================================
// Infrastructure Resources
// ============================================

// 1. Create firewall (no inbound, all outbound)
const firewall = createFirewall("openclaw-firewall");

// 2. Generate cloud-init user-data script
const userData = generateUserData({
    tailscaleAuthKey,
    anthropicApiKey,
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

// Access information
export const tailscaleHostname = pulumi.interpolate`${serverName}`;
export const accessUrl = pulumi.interpolate`https://${serverName}.<your-tailnet>.ts.net/`;

// Instructions
export const postDeploymentInstructions = pulumi.interpolate`
╔══════════════════════════════════════════════════════════════════╗
║                    OpenClaw Deployment Complete                  ║
╠══════════════════════════════════════════════════════════════════╣
║                                                                  ║
║  Server: ${serverName}                                           ║
║  IPv4: ${server.ipv4Address}                                     ║
║                                                                  ║
║  ⚠️  Wait ~3 minutes for cloud-init to complete                  ║
║                                                                  ║
║  Access Methods:                                                 ║
║  1. Web UI: https://${serverName}.<tailnet>.ts.net/              ║
║  2. SSH: ssh ubuntu@${serverName}.<tailnet>.ts.net               ║
║                                                                  ║
║  Verification:                                                   ║
║  ./scripts/verify.sh                                             ║
║                                                                  ║
║  View logs on server:                                            ║
║  sudo journalctl -u openclaw -f                                  ║
║  sudo cat /var/log/cloud-init-openclaw.log                       ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
`;
