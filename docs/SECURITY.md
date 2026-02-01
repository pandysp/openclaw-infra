# Security Model

This document describes the threat model and mitigations for the OpenClaw deployment.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        INTERNET                                 │
│                                                                 │
│  Hetzner Firewall: BLOCK ALL INBOUND                           │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │                                                         │   │
│  │                   Hetzner VPS                           │   │
│  │                                                         │   │
│  │  ┌─────────────────────────────────────────────────┐   │   │
│  │  │              Docker Container                    │   │   │
│  │  │                                                  │   │   │
│  │  │  ┌──────────────────────────────────────────┐   │   │   │
│  │  │  │          OpenClaw Gateway                │   │   │   │
│  │  │  │          (localhost:18789)               │   │   │   │
│  │  │  │                                          │   │   │   │
│  │  │  │  - no-new-privileges                     │   │   │   │
│  │  │  │  - all capabilities dropped              │   │   │   │
│  │  │  └──────────────────────────────────────────┘   │   │   │
│  │  │                     ▲                           │   │   │
│  │  └─────────────────────│───────────────────────────┘   │   │
│  │                        │                               │   │
│  │           Tailscale Serve (proxy)                      │   │
│  │                        │                               │   │
│  └────────────────────────│───────────────────────────────┘   │
│                           │                                    │
│           Tailscale (NAT traversal, encrypted)                │
│                           │                                    │
└───────────────────────────│────────────────────────────────────┘
                            │
                            ▼
                    Your Machine
                   (Tailscale client)
```

## Threat Model

### Threat 1: Exposed Gateway

**Attack**: Attacker discovers and accesses the OpenClaw gateway from the internet.

**Mitigations**:
- Hetzner cloud firewall blocks ALL inbound traffic
- OpenClaw binds to `127.0.0.1` only (not `0.0.0.0`)
- Tailscale Serve proxies access through encrypted tunnel
- No DNS records point to the server's public IP

**Residual Risk**: Low. Multiple layers must fail simultaneously.

### Threat 2: Compromised Server

**Attack**: Attacker gains shell access to the VPS.

**Mitigations**:
- No SSH password authentication (key-only)
- No public SSH port (Tailscale SSH only)
- Docker container isolation
- `no-new-privileges` prevents privilege escalation
- All capabilities dropped in container
- Regular OS updates via cloud-init

**Residual Risk**: Medium. Container escape is possible but difficult.

### Threat 3: API Key Leak

**Attack**: Anthropic API key is exposed.

**Mitigations**:
- API key stored in Pulumi encrypted state
- Never committed to git (`.gitignore`)
- Injected at runtime via environment variable
- `.env` file has `600` permissions

**Residual Risk**: Low if practices followed. Rotate key if suspected leak.

### Threat 4: Lateral Movement

**Attack**: Attacker uses compromised server to attack other systems.

**Mitigations**:
- Server is isolated (single-purpose VPS)
- No access to internal networks
- Tailscale device has minimal ACL permissions
- Outbound-only firewall prevents hosting attack infrastructure

**Residual Risk**: Low. Limited attack surface.

### Threat 5: Supply Chain Attack

**Attack**: Malicious Docker image or dependency.

**Mitigations**:
- Use official Anthropic image from GHCR
- Pin image versions in production
- Docker Content Trust (optional)
- Pulumi dependencies from npm registry

**Residual Risk**: Medium. Trust in upstream is required.

## Security Checklist

### Deployment

- [ ] Hetzner firewall has NO inbound rules
- [ ] SSH keys are Pulumi-generated (no password auth)
- [ ] Tailscale auth key is ephemeral/reusable
- [ ] API keys are set via `pulumi config set --secret`
- [ ] `.env` files are not committed to git

### Verification

- [ ] `./scripts/verify.sh` passes all checks
- [ ] No ports are publicly accessible
- [ ] Tailscale Serve is configured correctly
- [ ] Container runs with security options

### Ongoing

- [ ] Monitor Tailscale admin console for unexpected devices
- [ ] Rotate API keys periodically
- [ ] Apply OS updates (auto via unattended-upgrades)
- [ ] Review Docker image updates

## Incident Response

### Suspected Compromise

1. **Isolate**: Remove server from Tailscale (`tailscale logout`)
2. **Revoke**: Rotate Anthropic API key immediately
3. **Preserve**: Take snapshot of server for forensics
4. **Destroy**: `pulumi destroy` the infrastructure
5. **Rebuild**: Fresh deployment with new credentials

### API Key Rotation

1. Generate new key at console.anthropic.com
2. Update Pulumi config: `pulumi config set anthropicApiKey --secret`
3. Redeploy: `pulumi up`
4. Revoke old key in Anthropic console
