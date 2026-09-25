# OpenClaw Infrastructure

Self-hosted [OpenClaw](https://openclaw.ai) gateway on a Hetzner VPS with zero-trust Tailscale networking. No public ports exposed. ~€11.39/month.

**This is a reference template.** Clone it and adapt for your own deployment — the config values (timezone, model, cron prompts) are working examples you'll customize.

## Features

- **Cheap**: Hetzner CX43 x86 (8 vCPU, 16 GB) ~€9.49/mo + backups (~€11.39/mo total)
- **Secure**: Hetzner firewall + UFW + Tailscale-only access + device pairing
- **Simple**: Pulumi IaC, single command deploy, systemd user service
- **Safe automation**: Heartbeats and cron jobs are off until explicitly enabled per agent
- **Telegram**: Optional scheduled tasks with reversible, ID-preserving pause/resume
- **Workspace sync**: Optional hourly Git synchronization through per-agent one-shot containers; existing timers remain in charge

## Prerequisites

- Node.js 18+
- [Pulumi CLI](https://www.pulumi.com/docs/install/)
- [Ansible](https://docs.ansible.com/ansible/latest/installation_guide/) (`pip install ansible`)
- [Tailscale](https://tailscale.com/start) installed and connected on your machine
- Hetzner Cloud API token ([console.hetzner.cloud](https://console.hetzner.cloud/))
- Tailscale auth key ([login.tailscale.com/admin/settings/keys](https://login.tailscale.com/admin/settings/keys))
- Tailscale MagicDNS and HTTPS enabled ([login.tailscale.com/admin/dns](https://login.tailscale.com/admin/dns)) — required for Tailscale Serve
- Claude setup token (run `claude setup-token`)

See [CLAUDE.md](./CLAUDE.md#first-time-setup) for detailed setup instructions.

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

## Pulumi Backend

The current deployment and CI use Cloudflare R2, not Pulumi Cloud. Keep these values in the project's ignored `.env` (mode `0600`); use `.env.example` for initial setup. Run `direnv allow` once, then `direnv exec . <command>` from this repository. The hook clears inherited deployment credentials before loading this project's file. Routine commands do not query 1Password.

| Variable | Purpose |
|----------|---------|
| `PULUMI_BACKEND_URL` | `s3://<bucket>?endpoint=https://<account-id>.r2.cloudflarestorage.com&region=auto` |
| `AWS_ACCESS_KEY_ID` | R2 access key ID, scoped to the state bucket |
| `AWS_SECRET_ACCESS_KEY` | R2 secret access key |
| `PULUMI_CONFIG_PASSPHRASE` | Passphrase used to encrypt this stack's secrets |

Pulumi encrypts values marked as secrets. The current production checkpoint's obsolete plaintext passphrase fields were removed and independently checked on 2026-09-18, without provisioning or changing other resource values. Historical checkpoints, the backend backup, and earlier private exports still contain that passphrase; it has not been rotated. Do not treat bucket access and secret decryption as separate protections. Rotation and historical cleanup are outside this reconciliation's scope; the exposure remains. Exports, including those without `--show-secrets`, must stay private.

A Pulumi Cloud access token is not needed for this backend. Require non-empty values; do not print them or fall back to interactive credential lookups. Production PATs remain in encrypted `pulumi/Pulumi.prod.yaml`. Keep `PULUMI_BACKEND_URL` explicit so another project's cached backend does not get selected.

## Quick Start

```bash
npm install
cd pulumi
pulumi login "${PULUMI_BACKEND_URL:?Load the backend environment first}"
pulumi stack init prod

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

Stack state is stored in R2; secret decryption requires the stack's passphrase. See [Pulumi Backend](#pulumi-backend).

## Access

Wait ~5 minutes after deployment for cloud-init + Ansible to finish, then open:
```
https://openclaw-vps.<tailnet>.ts.net/chat
```

### First-Time Device Pairing

OpenClaw requires **device pairing** for all connections — including the server's own CLI. On a fresh install:

1. **Open the chat URL above** over Tailscale. Do not print or share token-bearing URLs.

2. **Approve only the matching device request** over Tailscale SSH:
   ```bash
   ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'openclaw devices list'
   ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'openclaw devices approve <request-id>'
   ```

3. **If Ansible failed because CLI authorization was unavailable**, resolve the matching request, then re-run:
   ```bash
   ./scripts/provision.sh --tags telegram
   ```

> No public SSH port is exposed. SSH works over Tailscale only.

See [CLAUDE.md](./CLAUDE.md#device-pairing) for details and [docs/TROUBLESHOOTING.md](./docs/TROUBLESHOOTING.md) for common issues.

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
