import * as pulumi from "@pulumi/pulumi";
import * as command from "@pulumi/command";
import * as random from "@pulumi/random";
import * as tls from "@pulumi/tls";
import { createFirewall } from "./firewall";
import { createServer } from "./server";
import { generateUserData } from "./user-data";

// Load configuration
const config = new pulumi.Config();

// ============================================
// Agent configuration
// ============================================

// Additional agent IDs (comma-separated). Main agent is always present.
// Example: pulumi config set agentIds "manon,tl,henning,ph,nici"
const agentIds = (config.get("agentIds") || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);

// Helper: convert agent ID to PascalCase for config key derivation
// e.g., "manon" -> "Manon", "tl" -> "Tl", "ph" -> "Ph"
function toPascal(id: string): string {
    return id.charAt(0).toUpperCase() + id.slice(1);
}

// ============================================
// Required secrets
// ============================================

const tailscaleAuthKey = config.requireSecret("tailscaleAuthKey");
const claudeSetupToken = config.requireSecret("claudeSetupToken");

// ============================================
// Global optional config
// ============================================

const telegramBotToken = config.getSecret("telegramBotToken");
const discordBotToken = config.getSecret("discordBotToken");
const xaiApiKey = config.getSecret("xaiApiKey");
const groqApiKey = config.getSecret("groqApiKey");

// Tailscale configuration
const tailnetDnsName = config.get("tailnetDnsName") || "";

// Server configuration (with defaults)
const serverName = config.get("serverName") || "openclaw-vps";
const serverType = config.get("serverType") || "cx33"; // Default cx33 (4 vCPU, 8GB); use cx43 (8 vCPU, 16GB) for qmd
const serverLocation = config.get("serverLocation") || "nbg1"; // Nuremberg (alternatives: fsn1=Falkenstein, hel1=Helsinki)
const serverImage = config.get("serverImage") || "ubuntu-24.04";

// ============================================
// Main agent config (no suffix in key names)
// ============================================

const telegramUserId = config.get("telegramUserId");
const telegramGroupId = config.get("telegramGroupId");
const discordGuildId = config.get("discordGuildId");
const discordUserId = config.get("discordUserId");
const githubToken = config.getSecret("githubToken");
const workspaceRepoUrl = config.get("workspaceRepoUrl");
// Obsidian vault for main agent uses "Andy" (person name, not agent ID)
const obsidianAndyVaultRepoUrl = config.get("obsidianAndyVaultRepoUrl");

// ============================================
// Per-agent config (derived from agentIds)
// ============================================

// Read per-agent config keys using PascalCase convention.
// Missing keys return undefined — not all agents have all features.
const agentConfig: Record<
    string,
    {
        githubToken: pulumi.Output<string> | undefined;
        telegramUserId: string | undefined;
        telegramGroupId: string | undefined;
        whatsappPhone: string | undefined;
        workspaceRepoUrl: string | undefined;
        obsidianVaultRepoUrl: string | undefined;
    }
> = {};

for (const id of agentIds) {
    const p = toPascal(id);
    agentConfig[id] = {
        githubToken: config.getSecret(`githubToken${p}`),
        telegramUserId: config.get(`telegram${p}UserId`),
        telegramGroupId: config.get(`telegram${p}GroupId`),
        whatsappPhone: config.get(`whatsapp${p}Phone`),
        workspaceRepoUrl: config.get(`workspace${p}RepoUrl`),
        obsidianVaultRepoUrl: config.get(`obsidian${p}VaultRepoUrl`),
    };
}

// ============================================
// Security: Generate Gateway Token
// ============================================

const gatewayToken = new random.RandomPassword("openclaw-gateway-token", {
    length: 48,
    special: false,
});

// ============================================
// Workspace Git Sync: Deploy Keys
// ============================================

// Main agent deploy key (always generated so public key can be retrieved before first deploy)
const workspaceDeployKey = new tls.PrivateKey("workspace-deploy-key", {
    algorithm: "ED25519",
});

// Per-agent deploy keys — logical names match existing resources (URN-safe, no regeneration)
const agentDeployKeys: Record<string, tls.PrivateKey> = {};
for (const id of agentIds) {
    agentDeployKeys[id] = new tls.PrivateKey(`workspace-deploy-key-${id}`, {
        algorithm: "ED25519",
    });
}

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

// Build environment variables dynamically
const provisionEnv: Record<string, pulumi.Input<string>> = {
    // Pass all secrets directly so provision.sh doesn't need to
    // call `pulumi stack output` (which can't read in-flight state)
    PULUMI_CONFIG_PASSPHRASE: process.env.PULUMI_CONFIG_PASSPHRASE || "",
    PROVISION_AGENT_IDS: agentIds.join(","),
    PROVISION_GATEWAY_TOKEN: gatewayToken.result,
    PROVISION_CLAUDE_SETUP_TOKEN: claudeSetupToken,
    PROVISION_TELEGRAM_BOT_TOKEN: telegramBotToken || "",
    PROVISION_TELEGRAM_USER_ID: telegramUserId || "",
    PROVISION_TELEGRAM_GROUP_ID: telegramGroupId || "",
    PROVISION_WORKSPACE_REPO_URL: workspaceRepoUrl || "",
    PROVISION_WORKSPACE_DEPLOY_KEY: workspaceDeployKey.privateKeyOpenssh,
    PROVISION_TAILSCALE_HOSTNAME: serverName,
    PROVISION_XAI_API_KEY: xaiApiKey || "",
    PROVISION_GROQ_API_KEY: groqApiKey || "",
    PROVISION_GITHUB_TOKEN: githubToken || "",
    PROVISION_OBSIDIAN_ANDY_VAULT_REPO_URL: obsidianAndyVaultRepoUrl || "",
    PROVISION_DISCORD_BOT_TOKEN: discordBotToken || "",
    PROVISION_DISCORD_GUILD_ID: discordGuildId || "",
    PROVISION_DISCORD_USER_ID: discordUserId || "",
};

// Add per-agent env vars
for (const id of agentIds) {
    const upper = id.toUpperCase();
    const cfg = agentConfig[id];
    provisionEnv[`PROVISION_GITHUB_TOKEN_${upper}`] = cfg.githubToken || "";
    provisionEnv[`PROVISION_TELEGRAM_${upper}_USER_ID`] =
        cfg.telegramUserId || "";
    provisionEnv[`PROVISION_TELEGRAM_${upper}_GROUP_ID`] =
        cfg.telegramGroupId || "";
    provisionEnv[`PROVISION_WHATSAPP_${upper}_PHONE`] =
        cfg.whatsappPhone || "";
    provisionEnv[`PROVISION_WORKSPACE_${upper}_REPO_URL`] =
        cfg.workspaceRepoUrl || "";
    provisionEnv[`PROVISION_WORKSPACE_${upper}_DEPLOY_KEY`] =
        agentDeployKeys[id].privateKeyOpenssh;
    provisionEnv[`PROVISION_OBSIDIAN_${upper}_VAULT_REPO_URL`] =
        cfg.obsidianVaultRepoUrl || "";
}

const ansibleProvision = new command.local.Command(
    "ansible-provision",
    {
        create: pulumi.interpolate`cd ${__dirname}/.. && ./scripts/provision.sh`,
        environment: provisionEnv,
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

// Main workspace deploy keys (kept for backward compatibility)
export const workspaceDeployPublicKey = workspaceDeployKey.publicKeyOpenssh;
export const workspaceDeployPrivateKey = pulumi.secret(
    workspaceDeployKey.privateKeyOpenssh
);

// All agent workspace deploy keys as a structured JSON export (includes main)
// Usage: pulumi stack output agentWorkspaceKeys --json --show-secrets | jq '."manon".publicKey'
const allKeyIds = ["main", ...agentIds];
const allKeys = [
    workspaceDeployKey,
    ...agentIds.map((id) => agentDeployKeys[id]),
];

export const agentWorkspaceKeys = pulumi.secret(
    pulumi
        .all(
            allKeys.map((k) =>
                pulumi.all([k.publicKeyOpenssh, k.privateKeyOpenssh])
            )
        )
        .apply((resolved) => {
            const result: Record<
                string,
                { publicKey: string; privateKey: string }
            > = {};
            resolved.forEach(([pub, priv], i) => {
                result[allKeyIds[i]] = { publicKey: pub, privateKey: priv };
            });
            return result;
        })
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
