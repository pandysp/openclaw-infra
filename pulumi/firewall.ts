import * as hcloud from "@pulumi/hcloud";

/**
 * Creates a Hetzner Cloud Firewall with zero-trust inbound policy.
 *
 * Security model:
 * - INBOUND: Block everything. No SSH, no HTTP, nothing.
 *   Tailscale uses NAT traversal (STUN/TURN), so no inbound rules needed.
 * - OUTBOUND: Allow everything. Required for:
 *   - Docker image pulls
 *   - Tailscale coordination
 *   - Anthropic API calls
 *   - System updates
 */
export function createFirewall(name: string): hcloud.Firewall {
    return new hcloud.Firewall(name, {
        name: name,
        rules: [
            // Allow all outbound TCP (Docker, APIs, updates)
            {
                direction: "out",
                protocol: "tcp",
                port: "1-65535",
                destinationIps: ["0.0.0.0/0", "::/0"],
                description: "Allow all outbound TCP",
            },
            // Allow all outbound UDP (Tailscale STUN, DNS)
            {
                direction: "out",
                protocol: "udp",
                port: "1-65535",
                destinationIps: ["0.0.0.0/0", "::/0"],
                description: "Allow all outbound UDP",
            },
            // Allow outbound ICMP (network diagnostics)
            {
                direction: "out",
                protocol: "icmp",
                destinationIps: ["0.0.0.0/0", "::/0"],
                description: "Allow outbound ICMP",
            },
            // NO INBOUND RULES - This is intentional!
            // Tailscale uses NAT traversal (hole punching) which doesn't
            // require inbound firewall rules. All connections are established
            // outbound first.
        ],
        labels: {
            project: "openclaw",
            managed_by: "pulumi",
        },
    });
}
