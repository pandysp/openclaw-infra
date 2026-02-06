# OpenClaw Infrastructure

Self-hosted [OpenClaw](https://openclaw.ai) gateway on a Hetzner VPS with zero-trust Tailscale networking. No public ports exposed. ~€6.59/month.

**This is a reference template.** Clone it and adapt for your own deployment — the config values (timezone, model, cron prompts) are working examples you'll customize.

## Features

- **Cheap**: Hetzner CX33 x86 (4 vCPU, 8 GB) ~€5.49/mo + backups
- **Secure**: Hetzner firewall + UFW + Tailscale-only access + device pairing
- **Simple**: Pulumi IaC, single command deploy, systemd user service
- **Telegram**: Optional scheduled tasks (morning digest, evening review, weekly planning)
- **Workspace sync**: Optional hourly git backup of the agent's workspace to GitHub

## Prerequisites

- Node.js 18+
- [Pulumi CLI](https://www.pulumi.com/docs/install/)
- Ansible (`pip install ansible`)
- [Tailscale](https://tailscale.com/start) installed and connected on your machine
- Hetzner Cloud API token ([console.hetzner.cloud](https://console.hetzner.cloud/))
- Tailscale auth key ([login.tailscale.com/admin/settings/keys](https://login.tailscale.com/admin/settings/keys))
- **One of:**
  - Claude setup token (run `claude setup-token`) — default
  - Kimi API key ([platform.moonshot.ai](https://platform.moonshot.ai)) — alternative

See [CLAUDE.md](./CLAUDE.md#prerequisites) for detailed setup instructions.

### First-Time Tailscale Setup

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

### Telegram Bot Setup

To enable optional Telegram notifications:

1. **Create a bot**: Open Telegram, search for **@BotFather**, send `/newbot`
   - Choose a display name (e.g., "OpenClaw Assistant")
   - Choose a username (must end in "bot", e.g., `openclaw_assistant_bot`)
   - Copy the bot token (format: `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`)

2. **Get your user ID**: Search for **@userinfobot** on Telegram, send `/start`, copy your numeric user ID

3. **Configure**: See [Quick Start](#quick-start) below for the Pulumi commands.

## Quick Start

```bash
npm install
cd pulumi
pulumi stack init prod  # Save this passphrase — you need it for every pulumi command

# Required
pulumi config set hcloud:token --secret       # Hetzner API token
pulumi config set tailscaleAuthKey --secret   # Tailscale auth key

# Provider token (Claude by default)
pulumi config set claudeSetupToken --secret   # From `claude setup-token`

# Optional: Telegram notifications (daily digests, weekly planning)
pulumi config set telegramBotToken --secret   # From @BotFather
pulumi config set telegramUserId "YOUR_ID"    # From @userinfobot (not secret, numeric only)

# Optional: hourly workspace backup to a private GitHub repo
pulumi config set workspaceRepoUrl "git@github.com:YOU/openclaw-workspace.git"

# Deploy
pulumi up

# Verify (wait ~5 min for cloud-init)
cd ..
./scripts/verify.sh
```

> **Using Kimi instead of Claude?** Replace the token line with:
> ```bash
> pulumi config set provider kimi
> pulumi config set kimiApiKey --secret  # From platform.moonshot.ai
> ```

> After verifying, clean up the cloud-init log (contains secrets):
> `ssh ubuntu@openclaw-vps.<tailnet>.ts.net "sudo shred -u /var/log/cloud-init-openclaw.log"`

See [CLAUDE.md](./CLAUDE.md#pulumi-passphrase) for passphrase management options.

## Access

Wait ~5 minutes after deployment for cloud-init to finish, then open:
```
https://openclaw-vps.<tailnet>.ts.net/chat
```

First visit requires **device pairing** (one-time). Approve via SSH (over Tailscale):
```bash
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'openclaw devices list'
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'openclaw devices approve <request-id>'
```

> No public SSH port is exposed. SSH works over Tailscale only.

See [CLAUDE.md](./CLAUDE.md#first-time-access-device-pairing) for details.

## Switching Providers

Switch between Claude and Kimi on an existing deployment:

```bash
# Switch to Kimi
pulumi config set provider kimi
pulumi config set kimiApiKey --secret
./scripts/provision.sh --tags openclaw,config

# Switch to Claude
pulumi config set provider claude
pulumi config set claudeSetupToken --secret
./scripts/provision.sh --tags openclaw,config
```

See [CLAUDE.md](./CLAUDE.md#switching-providers) for details on provider defaults and model overrides.

## Architecture

```
Your Machine ──(Tailscale)──> Hetzner VPS ──> OpenClaw Gateway
                               Hetzner FW: no inbound
                               UFW: tailscale0 only
                               Gateway: localhost:18789 (systemd --user)
                               Tailscale Serve: HTTPS proxy
```

## Documentation

- [CLAUDE.md](./CLAUDE.md) — Setup, operations, security, and troubleshooting
- [docs/SECURITY.md](./docs/SECURITY.md) — Threat model and mitigations
- [docs/TROUBLESHOOTING.md](./docs/TROUBLESHOOTING.md) — Common issues
- [docs/BROWSER-CONTROL-PLANNING.md](./docs/BROWSER-CONTROL-PLANNING.md) — Future browser automation approaches

## License

[MIT](./LICENSE)
