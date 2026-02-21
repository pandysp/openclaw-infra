# OpenClaw Infrastructure

Self-hosted [OpenClaw](https://openclaw.ai) gateway on a Hetzner VPS with zero-trust Tailscale networking. No public ports exposed. ~€6.59/month.

**This is a reference template.** Clone it and adapt for your own deployment — the config values (timezone, model, cron prompts) are working examples you'll customize.

## Features

- **Cheap**: Hetzner CX33 x86 (4 vCPU, 8 GB) ~€5.49/mo + backups (or CX43 ~€11.39/mo for qmd semantic search)
- **Secure**: Hetzner firewall + UFW + Tailscale-only access + device pairing
- **Simple**: Pulumi IaC, single command deploy, systemd user service
- **Telegram**: Optional scheduled tasks (morning digest, evening review, weekly planning)
- **Workspace sync**: Optional hourly git backup of the agent's workspace to GitHub

## Prerequisites

- Node.js 18+
- [Pulumi CLI](https://www.pulumi.com/docs/install/)
- [Ansible](https://docs.ansible.com/ansible/latest/installation_guide/) (`pip install ansible`)
- [Tailscale](https://tailscale.com/start) installed and connected on your machine
- Hetzner Cloud API token ([console.hetzner.cloud](https://console.hetzner.cloud/))
- Tailscale auth key ([login.tailscale.com/admin/settings/keys](https://login.tailscale.com/admin/settings/keys))
- Tailscale MagicDNS and HTTPS enabled ([login.tailscale.com/admin/dns](https://login.tailscale.com/admin/dns)) — required for Tailscale Serve
- Claude setup token (run `claude setup-token`)

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

2. **Get your user ID** (either method):
   - Run `./scripts/get-telegram-id.sh` — briefly pauses the gateway, you send a message, it shows your IDs
   - Or search for **@userinfobot** on Telegram, send `/start`, copy your numeric user ID

3. **Configure**: See [Quick Start](#quick-start) below for the Pulumi commands.

## Quick Start

```bash
npm install
cd pulumi
pulumi stack init prod  # Save this passphrase — you need it for every pulumi command

# Required
pulumi config set hcloud:token --secret       # Hetzner API token
pulumi config set tailscaleAuthKey --secret    # Tailscale auth key
pulumi config set claudeSetupToken --secret    # From `claude setup-token`

# Optional: Telegram notifications (daily digests, weekly planning)
pulumi config set telegramBotToken --secret    # From @BotFather
pulumi config set telegramUserId "YOUR_ID"     # ./scripts/get-telegram-id.sh or @userinfobot

# Optional: hourly workspace backup to a private GitHub repo
pulumi config set workspaceRepoUrl "git@github.com:YOU/openclaw-workspace.git"

# Deploy
pulumi up

# Verify (wait ~5 min for cloud-init)
cd ..
./scripts/verify.sh
```

> After verifying, clean up the cloud-init log (contains secrets):
> `ssh ubuntu@openclaw-vps.<tailnet>.ts.net "sudo shred -u /var/log/cloud-init-openclaw.log"`

See [CLAUDE.md](./CLAUDE.md#pulumi-passphrase) for passphrase management options.

## Access

Wait ~5 minutes after deployment for cloud-init + Ansible to finish, then open:
```
https://openclaw-vps.<tailnet>.ts.net/chat
```

### First-Time Device Pairing

OpenClaw requires **device pairing** for all connections — including the server's own CLI. On a fresh install:

1. **Use the tokenized URL** to access the web UI without pairing:
   ```bash
   cd pulumi && pulumi stack output tailscaleUrlWithToken --show-secrets
   ```
   Open that URL in your browser. This bypasses pairing for initial setup.

2. **Approve devices** that need pairing. The server's CLI (used by Ansible) may show as pending:
   ```bash
   ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'openclaw devices list'
   ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'openclaw devices approve <request-id>'
   ```

3. **If Ansible failed during first deploy** (cron setup skipped due to pairing), re-run after approving:
   ```bash
   ./scripts/provision.sh --tags telegram
   ```

> No public SSH port is exposed. SSH works over Tailscale only.

See [CLAUDE.md](./CLAUDE.md#first-time-access-device-pairing) for details and [docs/TROUBLESHOOTING.md](./docs/TROUBLESHOOTING.md) for common issues.

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
