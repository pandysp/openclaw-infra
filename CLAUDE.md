# OpenClaw Infrastructure

> AI assistant guide for deploying and managing OpenClaw on Hetzner Cloud with Tailscale.

## What This Project Is

OpenClaw is a self-hosted Anthropic Computer Use gateway deployed on a €5/month Hetzner VPS with zero-trust networking via Tailscale. All access is through Tailscale—no public ports exposed.

## Architecture

```
┌─────────────────┐     ┌─────────────────────────────────────┐
│  Your Machine   │     │         Hetzner VPS (€4.51/mo)      │
│  (Tailscale)    │────▶│  ┌─────────────────────────────┐    │
│                 │     │  │      Docker Container       │    │
│  No public IP   │     │  │  ┌─────────────────────┐    │    │
│  exposure       │     │  │  │   OpenClaw Gateway  │    │    │
└─────────────────┘     │  │  │   localhost:18789   │    │    │
                        │  │  └─────────────────────┘    │    │
                        │  └─────────────────────────────┘    │
                        │                                     │
                        │  Hetzner Firewall: No inbound       │
                        │  Tailscale Serve → localhost:18789  │
                        └─────────────────────────────────────┘
```

## Security Model

| Layer | Measure |
|-------|---------|
| Network | Hetzner cloud firewall blocks ALL inbound |
| Access | Tailscale-only (no public SSH, no public ports) |
| Isolation | Docker container with `no-new-privileges` |
| Secrets | Pulumi encrypted config (never in git) |
| Gateway | Binds localhost only, proxied via Tailscale Serve |

## Prerequisites

Before deploying, you need:

1. **Hetzner Cloud API Token**
   - Go to https://console.hetzner.cloud/
   - Create project → Security → API Tokens → Generate

2. **Tailscale Auth Key**
   - Go to https://login.tailscale.com/admin/settings/keys
   - Generate auth key (reusable, ephemeral recommended)
   - Note your tailnet name (e.g., `tail12345.ts.net`)

3. **Anthropic API Key**
   - Go to https://console.anthropic.com/
   - Settings → API Keys → Create Key

4. **Local Tools**
   - Node.js 18+
   - Pulumi CLI (`curl -fsSL https://get.pulumi.com | sh`)
   - Tailscale CLI (for verification)

## First-Time Setup

```bash
# 1. Clone and install dependencies
cd ~/projects/openclaw-infra
npm install

# 2. Initialize Pulumi stack
cd pulumi
pulumi stack init prod

# 3. Configure Hetzner token
pulumi config set hcloud:token --secret

# 4. Configure secrets
pulumi config set tailscaleAuthKey --secret
pulumi config set anthropicApiKey --secret

# 5. Preview deployment
pulumi preview

# 6. Deploy
pulumi up

# 7. Wait ~3 minutes for cloud-init, then verify
cd ..
./scripts/verify.sh
```

## Directory Structure

```
openclaw-infra/
├── CLAUDE.md           # This file - AI assistant guide
├── README.md           # Human overview
├── package.json        # Node.js dependencies
├── tsconfig.json       # TypeScript config
│
├── pulumi/
│   ├── Pulumi.yaml     # Project definition
│   ├── Pulumi.prod.yaml# Stack config (non-secrets)
│   ├── index.ts        # Main entrypoint
│   ├── server.ts       # Hetzner server resource
│   ├── firewall.ts     # Security rules (no inbound!)
│   └── user-data.ts    # Cloud-init bootstrap script
│
├── docker/
│   ├── docker-compose.yml  # Reference compose file
│   └── env-template.txt    # API key template
│
├── scripts/
│   ├── verify.sh       # Post-deployment checks
│   └── backup.sh       # Data backup
│
└── docs/
    ├── SECURITY.md     # Threat model
    └── TROUBLESHOOTING.md
```

## Common Operations

### Deploy Changes

```bash
cd pulumi
pulumi up
```

### Check Server Status

```bash
# Via Tailscale
tailscale ping openclaw-vps

# SSH to server
ssh ubuntu@openclaw-vps.<tailnet>.ts.net

# Check container
docker ps
docker logs openclaw

# Check service
sudo systemctl status openclaw
sudo journalctl -u openclaw -f
```

### Update OpenClaw Image

```bash
ssh ubuntu@openclaw-vps.<tailnet>.ts.net
cd /opt/openclaw
docker compose pull
docker compose up -d
```

### View Cloud-Init Logs

```bash
ssh ubuntu@openclaw-vps.<tailnet>.ts.net
sudo cat /var/log/cloud-init-openclaw.log
```

### Destroy Infrastructure

```bash
cd pulumi
pulumi destroy
```

## Security DO's and DON'Ts

### DO ✅

- Keep all access through Tailscale
- Use `pulumi config set --secret` for sensitive values
- Run `./scripts/verify.sh` after deployment
- Check that no public ports are exposed

### DON'T ❌

- Never add inbound firewall rules
- Never expose the Docker socket
- Never commit `.env` files or API keys
- Never use password SSH authentication
- Never bind OpenClaw to 0.0.0.0

## Troubleshooting

### Can't reach server via Tailscale

1. Wait 3-5 minutes for cloud-init to complete
2. Check Tailscale admin console for the device
3. SSH via Hetzner console and check `tailscale status`

### Container not running

```bash
ssh ubuntu@openclaw-vps.<tailnet>.ts.net
sudo systemctl status openclaw
sudo journalctl -u openclaw -n 100
docker logs openclaw
```

### Tailscale Serve not working

```bash
ssh ubuntu@openclaw-vps.<tailnet>.ts.net
tailscale serve status
# If not configured:
tailscale serve --bg https / http://127.0.0.1:18789
```

## Cost Breakdown

| Resource | Cost |
|----------|------|
| Hetzner CAX11 (ARM, 2 vCPU, 4GB) | €4.51/mo |
| Hetzner Backups | €0.90/mo |
| Tailscale | Free (personal) |
| **Total** | **~€5.41/mo** |
