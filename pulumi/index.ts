import * as pulumi from "@pulumi/pulumi";
import * as random from "@pulumi/random";
import { createFirewall } from "./firewall";
import { createServer } from "./server";
import { generateUserData } from "./user-data";

// Load configuration
const config = new pulumi.Config();

// Required secrets (set via `pulumi config set --secret`)
const tailscaleAuthKey = config.requireSecret("tailscaleAuthKey");
const claudeSetupToken = config.requireSecret("claudeSetupToken");

// Tailscale configuration
// Find your tailnet name at: https://login.tailscale.com/admin/dns
const tailnetDnsName = config.get("tailnetDnsName") || "";

// Server configuration (with defaults)
const serverName = config.get("serverName") || "openclaw-vps";
const serverType = config.get("serverType") || "cax21"; // ARM, 8GB RAM, €6.49/mo
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
// Infrastructure Resources
// ============================================

// 1. Create firewall (no inbound, all outbound)
const firewall = createFirewall("openclaw-firewall");

// 2. Generate cloud-init user-data script
const userData = generateUserData({
    tailscaleAuthKey,
    claudeSetupToken,
    gatewayToken: gatewayToken.result,
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

// Gateway token (for client configuration)
export const openclawGatewayToken = pulumi.secret(gatewayToken.result);

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
║  ⚠️  Wait ~5 minutes for cloud-init to complete                  ║
║                                                                  ║
║  Access Methods:                                                 ║
║  1. Web UI: https://${serverName}.<tailnet>.ts.net/              ║
║  2. SSH: ssh ubuntu@${serverName}.<tailnet>.ts.net               ║
║                                                                  ║
║  Verification:                                                   ║
║  ./scripts/verify.sh                                             ║
║                                                                  ║
║  View logs on server:                                            ║
║  systemctl --user status openclaw                                ║
║  sudo cat /var/log/cloud-init-openclaw.log                       ║
║                                                                  ║
║  SECURITY: After verifying, clean up the cloud-init log:         ║
║  ssh ubuntu@<host> "sudo shred -u /var/log/cloud-init-openclaw.log" ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
`;
