# OpenClaw Infrastructure

> AI assistant guide for deploying and managing OpenClaw on Hetzner Cloud with Tailscale.

## What This Project Is

OpenClaw is a self-hosted Anthropic Computer Use gateway deployed on a Hetzner VPS with zero-trust networking via Tailscale. All access is through Tailscale—no public ports exposed.

## Architecture

```
┌─────────────────┐     ┌─────────────────────────────────────┐
│  Your Machine   │     │         Hetzner VPS (€7.79/mo)      │
│  (Tailscale)    │────▶│                                     │
│                 │     │  ┌─────────────────────────────┐    │
│  No public IP   │     │  │   OpenClaw Gateway          │    │
│  exposure       │     │  │   localhost:18789           │    │
│                 │     │  │   (systemd user service)    │    │
└─────────────────┘     │  └─────────────────────────────┘    │
                        │                                     │
                        │  Hetzner Firewall: No inbound       │
                        │  Tailscale Serve → localhost:18789  │
                        └─────────────────────────────────────┘
```

## Security Model

| Layer | Measure |
|-------|---------|
| Network (infrastructure) | Hetzner cloud firewall blocks ALL inbound |
| Network (host) | UFW: deny incoming, allow only tailscale0 interface |
| Access | Tailscale-only (no public SSH, no public ports) |
| Process | Runs as unprivileged `ubuntu` user via systemd user service |
| Auth | Tailscale identity + device pairing |
| Secrets | Pulumi encrypted config (never in git) |
| Gateway | Binds localhost only, proxied via Tailscale Serve |

### Why Two Auth Layers?

This deployment uses **Tailscale identity auth** with device pairing:

1. **Tailscale network auth** - Only devices on your tailnet can reach the gateway
2. **Device pairing** - Each browser/device must be explicitly approved

**Q: Do I need to enter a token?**
**A: No.** Tailscale identity replaces token auth. Just approve your device once.

**Q: What if I share my Tailscale network?**
**A: Device pairing prevents unauthorized access.** Others on your tailnet would need you to approve their device before they could use OpenClaw.

**Q: What if device pairing doesn't work?**
**A: Use the tokenized URL as fallback:** `pulumi stack output tailscaleUrlWithToken --show-secrets`

## Prerequisites

Before deploying, you need:

1. **Hetzner Cloud API Token**
   - Go to https://console.hetzner.cloud/
   - **Create a dedicated project for OpenClaw** (don't reuse existing projects)
   - Inside the project: Security → API Tokens → Generate (Read & Write)
   - Why separate? OpenClaw runs autonomous AI agents - if compromised, a shared token could affect your other infrastructure

2. **Tailscale Account & Auth Key**
   - If new to Tailscale, see [First-Time Tailscale Setup](#first-time-tailscale-setup) below
   - If you have Tailscale: https://login.tailscale.com/admin/settings/keys
   - Generate auth key with: **Reusable**, **Ephemeral**
   - Note your tailnet name (e.g., `tail12345.ts.net`)

3. **Claude Max Setup Token**
   - Uses your Claude Max subscription (flat-fee) instead of pay-per-token
   - Run `claude setup-token` in your terminal
   - Authorize in browser, get token starting with `sk-ant-oat01-...`
   - **Note**: Setup tokens only have `user:inference` scope (missing `user:profile`), so `/status` won't show usage tracking. See [GitHub issue #4614](https://github.com/openclaw/openclaw/issues/4614).

4. **Local Tools**
   - Node.js 18+
   - Pulumi CLI (`curl -fsSL https://get.pulumi.com | sh`)
   - Tailscale app (for accessing your server after deployment)

## First-Time Tailscale Setup

If you've never used Tailscale before:

1. **Create account**: Go to https://tailscale.com/start
   - Sign up with GitHub (recommended for infra projects), Google, or email
   - Free tier supports up to 100 devices

2. **Install on your Mac**:
   ```bash
   brew install --cask tailscale
   ```
   - Open Tailscale from Applications
   - Click "Allow" for System Extension and VPN Configuration prompts
   - Click menu bar icon → Log in → Authorize in browser

3. **Generate auth key for server**:
   - Go to https://login.tailscale.com/admin/settings/keys
   - Click "Generate auth key"
   - Enable: **Reusable**, **Ephemeral**
   - Copy the key (starts with `tskey-auth-...`)

## First-Time Setup

```bash
# 1. Clone and install dependencies
cd ~/projects/openclaw-infra
npm install

# 2. Initialize Pulumi stack (you'll be prompted to set a passphrase)
cd pulumi
pulumi stack init prod
# SAVE YOUR PASSPHRASE - you'll need it for all future pulumi commands

# 3. Configure Hetzner token
pulumi config set hcloud:token --secret

# 4. Configure secrets
pulumi config set tailscaleAuthKey --secret
pulumi config set claudeSetupToken --secret

# 5. Preview deployment
pulumi preview

# 6. Deploy
pulumi up

# 7. Wait ~5 minutes for cloud-init, then verify
cd ..
./scripts/verify.sh

# 8. SECURITY: Clean up cloud-init log (contains secrets)
ssh ubuntu@openclaw-vps.<tailnet>.ts.net "sudo shred -u /var/log/cloud-init-openclaw.log"
```

### First-Time Access (Device Pairing)

After deployment, you need to approve your browser as a trusted device (one-time):

1. **Open the gateway URL** in your browser:
   ```
   https://openclaw-vps.<tailnet>.ts.net/chat
   ```

2. **You'll see "pairing required"** - this is expected for first-time access

3. **Approve the device** via SSH:
   ```bash
   # List pending devices
   ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'source ~/.nvm/nvm.sh && openclaw devices list'

   # Approve the pending request (use the Request ID from the list)
   ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'source ~/.nvm/nvm.sh && openclaw devices approve <request-id>'
   ```

4. **Refresh the browser** - you're now authenticated via Tailscale identity

**For subsequent access:** Just use the plain URL. Your device is paired and Tailscale identity handles auth.

**New browser/device?** Repeat the pairing process above.

**Fallback (if pairing doesn't work):** Use the tokenized URL:
```bash
pulumi stack output tailscaleUrlWithToken --show-secrets
```

### Pulumi Passphrase

Pulumi encrypts your secrets locally using a passphrase. You must set the `PULUMI_CONFIG_PASSPHRASE` environment variable for every Pulumi command:

```bash
# Option 1: Set per-command
PULUMI_CONFIG_PASSPHRASE="your-passphrase" pulumi up

# Option 2: Export for session
export PULUMI_CONFIG_PASSPHRASE="your-passphrase"
pulumi up

# Option 3: Use a password manager / .envrc (don't commit!)
```

**Store your passphrase securely** - without it, you cannot manage or destroy your infrastructure.

### Pulumi State Backend

By default, this project uses **local state storage** (in `.pulumi/` directory, gitignored).

| Backend | Pros | Cons |
|---------|------|------|
| **Local** (current) | Simple, no account needed, free | State only on this machine, no CI/CD |
| **Pulumi Cloud** | CI/CD ready, team collaboration, free tier | Requires account |
| **S3/GCS** | Self-hosted, CI/CD ready | Extra setup, ~$1/mo |

**For CI/CD deployment**, migrate to Pulumi Cloud:
```bash
pulumi login                           # Switch to Pulumi Cloud
pulumi stack export --file state.json  # Export current state
pulumi stack import --file state.json  # Import to cloud backend
```

For a single personal server, local state is fine.

### Pulumi ESC (for CI/CD)

For CI/CD pipelines or team deployments, consider using **Pulumi ESC** (Environments, Secrets, and Configuration) instead of local passphrase encryption:

| Approach | Best For | How Secrets Are Stored |
|----------|----------|----------------------|
| **Local passphrase** (current) | Personal use, single machine | Encrypted locally with passphrase |
| **Pulumi ESC** | CI/CD, teams, multiple machines | Cloud-hosted, accessed via Pulumi Cloud |

**Benefits of ESC:**
- No passphrase to manage in CI/CD pipelines
- Centralized secrets management across environments
- Audit logging for secret access
- Dynamic secrets (e.g., short-lived cloud credentials)

**Migration to ESC:**
```bash
# Create ESC environment
pulumi env init <your-org>/openclaw-secrets

# Add secrets to environment (via Pulumi Cloud console or CLI)
# Then reference in Pulumi.prod.yaml:
# environment:
#   - <your-org>/openclaw-secrets
```

See [Pulumi ESC documentation](https://www.pulumi.com/docs/esc/) for details.

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

# Check service (on server)
XDG_RUNTIME_DIR=/run/user/1000 systemctl --user status openclaw-gateway

# Check logs (on server)
XDG_RUNTIME_DIR=/run/user/1000 journalctl --user -u openclaw-gateway -f
```

### Update OpenClaw

```bash
ssh ubuntu@openclaw-vps.<tailnet>.ts.net
source ~/.nvm/nvm.sh
npm update -g openclaw
XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart openclaw-gateway
```

### View Cloud-Init Logs

```bash
ssh ubuntu@openclaw-vps.<tailnet>.ts.net
sudo cat /var/log/cloud-init-openclaw.log
```

### Run Security Audit

OpenClaw includes a built-in security audit tool:

```bash
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'source ~/.nvm/nvm.sh && openclaw security audit --deep'
```

**Expected output:** `0 critical · 0 warn · 1 info` - This deployment uses Tailscale identity auth with device pairing, which passes all security checks.

### Destroy Infrastructure

```bash
cd pulumi
pulumi destroy
```

### Clean Up Stale Tailscale Devices

After destroying and redeploying, old Tailscale devices show as "offline" in your admin console. Each redeploy creates a new device with a numeric suffix (openclaw-vps-1, openclaw-vps-2, etc.).

**To clean up:**
1. Go to https://login.tailscale.com/admin/machines
2. Find devices named `openclaw-vps*` that show "offline"
3. Click the device → Remove

**Why not automate this?** Tailscale's API key has broad permissions (manage all devices). We deliberately use only an auth key (limited scope) for security. The tradeoff is manual cleanup of stale entries—a minor inconvenience for better security.

**Alternative:** Reduce ephemeral node expiry in Tailscale settings (Settings → Device Management) to auto-delete offline devices faster. However, this affects all ephemeral devices and risks removing devices during temporary outages.

## Security DO's and DON'Ts

### DO

- Use a **dedicated Hetzner project** for OpenClaw (isolation from other infra)
- Keep all access through Tailscale
- Use `pulumi config set --secret` for sensitive values
- Run `./scripts/verify.sh` after deployment
- Check that no public ports are exposed
- Store your Pulumi passphrase in a password manager
- **Clean up cloud-init log after deployment** (contains secrets)
- **Monitor Tailscale admin console** for unauthorized devices: https://login.tailscale.com/admin/machines
- **Rotate Tailscale auth keys periodically** (see [Key Rotation](#key-rotation) below)
- **Review paired OpenClaw devices** regularly: `openclaw devices list`

### DON'T

- Never share Hetzner tokens between high-risk and production projects
- Never add inbound firewall rules
- Never bind OpenClaw to 0.0.0.0
- Never commit `.env` files, API keys, or Pulumi passphrase
- Never use password SSH authentication

### Key Rotation

**Tailscale auth key:**
1. Generate new key at https://login.tailscale.com/admin/settings/keys
2. Update Pulumi config: `pulumi config set tailscaleAuthKey --secret`
3. Redeploy: `pulumi up` (or update manually on server)

**Claude setup token:**
1. Run `claude setup-token` locally
2. Update Pulumi config: `pulumi config set claudeSetupToken --secret`
3. Redeploy or update on server: `openclaw auth login`

**Gateway token** (auto-generated, rarely needs rotation):
1. Redeploy with `pulumi up` (generates new token)
2. Re-pair browser devices after rotation

## Secrets Reference

You'll need to keep track of these secrets (store in a password manager):

| Secret | Purpose | Where to regenerate |
|--------|---------|---------------------|
| Pulumi passphrase | Encrypts Pulumi state | Cannot recover - must redeploy if lost |
| Hetzner API token | Creates/manages VPS | console.hetzner.cloud → Project → API Tokens |
| Tailscale auth key | Joins server to your network | login.tailscale.com/admin/settings/keys |
| Claude setup token | Powers OpenClaw (flat fee) | `claude setup-token` in terminal |
| Gateway token | Authenticates browser sessions (cached after first use) | Auto-generated by Pulumi, view with `pulumi stack output openclawGatewayToken --show-secrets` |

## Troubleshooting

### Can't reach server via Tailscale

1. Wait 5 minutes for cloud-init to complete
2. Check Tailscale admin console for the device
3. SSH via Hetzner console and check `tailscale status`

### Service not running

```bash
ssh ubuntu@openclaw-vps.<tailnet>.ts.net
XDG_RUNTIME_DIR=/run/user/1000 systemctl --user status openclaw-gateway
XDG_RUNTIME_DIR=/run/user/1000 journalctl --user -u openclaw-gateway -n 100
```

### Tailscale Serve not working

```bash
ssh ubuntu@openclaw-vps.<tailnet>.ts.net
tailscale serve status
# If not configured:
tailscale serve --bg 18789
```

### "Pairing required" error

This means your browser/device hasn't been approved yet. Approve it via SSH:

```bash
# List pending devices (look for your IP in the pending list)
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'source ~/.nvm/nvm.sh && openclaw devices list'

# Approve the pending request
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'source ~/.nvm/nvm.sh && openclaw devices approve <request-id>'
```

Then refresh the browser.

**Common causes:**
- First time accessing from this browser/device
- Browser localStorage was cleared
- Using incognito/private mode

**Fallback:** Use the tokenized URL to bypass pairing:
```bash
cd pulumi && pulumi stack output tailscaleUrlWithToken --show-secrets
```

## Cost Breakdown

| Resource | Cost |
|----------|------|
| Hetzner CAX21 (ARM, 4 vCPU, 8GB) | €6.49/mo |
| Hetzner Backups | €1.30/mo |
| Tailscale | Free (personal) |
| **Total** | **~€7.79/mo** |
