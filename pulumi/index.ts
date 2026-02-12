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

// Optional xAI API key for Grok web search (set via `pulumi config set --secret`)
const xaiApiKey = config.getSecret("xaiApiKey");

// Optional GitHub PATs for MCP adapter (set via `pulumi config set --secret`)
const githubToken = config.getSecret("githubToken");
const githubTokenManon = config.getSecret("githubTokenManon");
const githubTokenTl = config.getSecret("githubTokenTl");

// Optional multi-agent Telegram configuration
const telegramManonUserId = config.get("telegramManonUserId");
const telegramGroupId = config.get("telegramGroupId");

// Optional workspace git sync (set via `pulumi config set`)
const workspaceRepoUrl = config.get("workspaceRepoUrl");
const workspaceManonRepoUrl = config.get("workspaceManonRepoUrl");
const workspaceTlRepoUrl = config.get("workspaceTlRepoUrl");

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

// Deploy keys for additional agent workspaces
const workspaceDeployKeyManon = new tls.PrivateKey("workspace-deploy-key-manon", {
    algorithm: "ED25519",
});
const workspaceDeployKeyTl = new tls.PrivateKey("workspace-deploy-key-tl", {
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
            // Pass all secrets directly so provision.sh doesn't need to
            // call `pulumi stack output` (which can't read in-flight state)
            PULUMI_CONFIG_PASSPHRASE:
                process.env.PULUMI_CONFIG_PASSPHRASE || "",
            PROVISION_GATEWAY_TOKEN: gatewayToken.result,
            PROVISION_CLAUDE_SETUP_TOKEN: claudeSetupToken,
            PROVISION_TELEGRAM_BOT_TOKEN: telegramBotToken || "",
            PROVISION_TELEGRAM_USER_ID: telegramUserId || "",
            PROVISION_WORKSPACE_REPO_URL: workspaceRepoUrl || "",
            PROVISION_WORKSPACE_DEPLOY_KEY:
                workspaceDeployKey.privateKeyOpenssh,
            PROVISION_TELEGRAM_MANON_USER_ID: telegramManonUserId || "",
            PROVISION_TELEGRAM_GROUP_ID: telegramGroupId || "",
            PROVISION_WORKSPACE_MANON_REPO_URL: workspaceManonRepoUrl || "",
            PROVISION_WORKSPACE_MANON_DEPLOY_KEY:
                workspaceDeployKeyManon.privateKeyOpenssh,
            PROVISION_WORKSPACE_TL_REPO_URL: workspaceTlRepoUrl || "",
            PROVISION_WORKSPACE_TL_DEPLOY_KEY:
                workspaceDeployKeyTl.privateKeyOpenssh,
            PROVISION_TAILSCALE_HOSTNAME: serverName,
            PROVISION_XAI_API_KEY: xaiApiKey || "",
            PROVISION_GITHUB_TOKEN: githubToken || "",
            PROVISION_GITHUB_TOKEN_MANON: githubTokenManon || "",
            PROVISION_GITHUB_TOKEN_TL: githubTokenTl || "",
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

// Workspace deploy keys (andy/main)
export const workspaceDeployPublicKey = workspaceDeployKey.publicKeyOpenssh;
export const workspaceDeployPrivateKey = pulumi.secret(
    workspaceDeployKey.privateKeyOpenssh
);

// Workspace deploy keys (manon)
export const workspaceManonDeployPublicKey =
    workspaceDeployKeyManon.publicKeyOpenssh;
export const workspaceManonDeployPrivateKey = pulumi.secret(
    workspaceDeployKeyManon.privateKeyOpenssh
);

// Workspace deploy keys (tl)
export const workspaceTlDeployPublicKey =
    workspaceDeployKeyTl.publicKeyOpenssh;
export const workspaceTlDeployPrivateKey = pulumi.secret(
    workspaceDeployKeyTl.privateKeyOpenssh
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
