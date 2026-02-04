# OpenClaw Infrastructure

> AI assistant guide for deploying and managing OpenClaw on Hetzner Cloud with Tailscale.

## What This Project Is

OpenClaw is a self-hosted Anthropic Computer Use gateway deployed on a Hetzner VPS with zero-trust networking via Tailscale. All access is through Tailscale—no public ports exposed.

## Repository Layout (Public Template + Private Fork)

This repo (`pandysp/openclaw-infra-private`) is the private deployment fork. The public template lives at `pandysp/openclaw-infra`.

**Git remotes:**
- `origin` → `pandysp/openclaw-infra-private` (private — your deployment)
- `upstream` → `pandysp/openclaw-infra` (public — shared template)

**Pushing changes:**
- `git push` — pushes to private repo (default, safe for personal config)
- `git push upstream main` — updates public template (only for generic improvements)

**What goes where:**

| Private only (`origin`) | Both repos (`upstream` too) |
|---|---|
| Personal Pulumi config values | Bug fixes in cloud-init script |
| Custom cron prompt tweaks | New features (e.g., sandbox improvements) |
| Personal Telegram settings | Security model updates |
| Tailnet-specific config | Documentation improvements |

**Pulling template updates into private:**
```bash
git fetch upstream
git merge upstream/main
```

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
| Process | Runs as unprivileged `ubuntu` user; all sessions [sandboxed](#sandboxing) in Docker (custom image with Claude Code) |
| Auth | Tailscale identity + device pairing |
| Secrets | Pulumi encrypted config (never in git) |
| Gateway | Binds localhost only, proxied via Tailscale Serve |

For the full threat model, see [docs/SECURITY.md](./docs/SECURITY.md).

### Why Not Docker?

The [official Hetzner guide](https://docs.openclaw.ai/platforms/hetzner) runs the gateway in Docker. We use systemd instead because:

- **Smaller attack surface** — Docker daemon runs as root. Our gateway runs as an unprivileged user.
- **Simpler operations** — `systemctl --user` and `journalctl` are easier to debug than container logs.
- **No persistence problem** — Docker requires baking binaries into images (they're lost on restart). With systemd, files on disk stay on disk.
- **Same restart guarantees** — systemd `Restart=on-failure` does what `restart: unless-stopped` does.

Docker is installed on the server for **sandbox support** — all sessions run in Docker containers with bridge networking and a custom image (`openclaw-sandbox-custom:latest`) that includes Claude Code. The gateway itself runs natively.

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
│   ├── Pulumi.prod.yaml  # Stack config (non-secrets)
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
    ├── BROWSER-CONTROL-PLANNING.md  # Future browser automation approaches
    ├── DOCS-REVIEW.md               # Official docs review tracking
    ├── SECURITY.md                  # Threat model
    └── TROUBLESHOOTING.md
```

## Local CLI

The OpenClaw CLI is installed locally and configured to talk to the remote gateway over Tailscale. **Prefer `openclaw` commands over SSH** for gateway operations — it's faster and avoids the SSH round-trip.

```bash
# Install
brew install openclaw-cli

# Configure for remote gateway (one-time)
openclaw onboard --non-interactive --accept-risk --flow quickstart --mode remote \
  --remote-url "wss://openclaw-vps.<tailnet>.ts.net" \
  --remote-token "$(pulumi stack output openclawGatewayToken --show-secrets)" \
  --skip-channels --skip-skills --skip-health --skip-ui --skip-daemon

# Approve the CLI as a paired device (on first connect, via SSH)
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'openclaw devices list'   # find the pending request ID
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'openclaw devices approve <request-id>'
```

After pairing, CLI commands work directly:

```bash
openclaw health              # Gateway health check
openclaw doctor              # Diagnostics and quick fixes
openclaw devices list        # List paired devices
openclaw cron list           # List scheduled jobs
openclaw security audit      # Run security audit (add --deep for thorough scan)
openclaw status              # Session health
```

**When to still use SSH:** systemd service management (`systemctl`, `journalctl`), system-level operations (`sudo`), updating the OpenClaw binary on the server.

## Common Operations

### Deploy Changes

```bash
cd pulumi
pulumi up
```

### Check Server Status

```bash
# Via local CLI (preferred)
openclaw health
openclaw status

# Via Tailscale ping
tailscale ping openclaw-vps

# Via SSH (for systemd-level details)
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'XDG_RUNTIME_DIR=/run/user/1000 systemctl --user status openclaw-gateway'
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'XDG_RUNTIME_DIR=/run/user/1000 journalctl --user -u openclaw-gateway -f'
```

### Update OpenClaw

```bash
# Requires SSH (system-level operation)
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'OPENCLAW_NO_ONBOARD=1 OPENCLAW_NO_PROMPT=1 curl -fsSL https://openclaw.ai/install.sh | bash'
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart openclaw-gateway'
```

### View Cloud-Init Logs

```bash
# Requires SSH (system-level operation)
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'sudo cat /var/log/cloud-init-openclaw.log'
```

### Run Security Audit

```bash
openclaw security audit --deep
```

**Expected output:** `0 critical · 0 warn · 1 info` — this deployment uses Tailscale identity auth with device pairing, which passes all security checks.

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

## Cost Breakdown

| Resource | Cost |
|----------|------|
| Hetzner CAX21 (ARM, 4 vCPU, 8GB) | €6.49/mo |
| Hetzner Backups | €1.30/mo |
| Tailscale | Free (personal) |
| **Total** | **~€7.79/mo** |

## Secrets Reference

| Secret | Purpose | Where to regenerate |
|--------|---------|---------------------|
| Pulumi passphrase | Encrypts Pulumi state | Cannot recover — must redeploy if lost |
| Hetzner API token | Creates/manages VPS | console.hetzner.cloud → Project → API Tokens |
| Tailscale auth key | Joins server to your network | login.tailscale.com/admin/settings/keys |
| Claude setup token | Powers OpenClaw (flat fee) | `claude setup-token` in terminal |
| Gateway token | Authenticates browser and CLI sessions (cached after first use) | Auto-generated by Pulumi, view with `pulumi stack output openclawGatewayToken --show-secrets` |
| Telegram bot token | (Optional) Sends messages via Telegram | @BotFather on Telegram |
| Telegram user ID | (Optional) Your Telegram recipient ID | @userinfobot on Telegram |
| Workspace deploy key | (Optional) Pushes workspace to GitHub | Auto-generated by Pulumi, view public key with `pulumi stack output workspaceDeployPublicKey` |

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
- **Review paired OpenClaw devices** regularly: `openclaw devices list` (via local CLI)

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
3. Re-run local CLI onboard with new token (see [Local CLI](#local-cli))

**Telegram bot token** (if compromised):
1. Revoke old token: Message @BotFather, send `/revoke`, select your bot
2. Get new token: `/token` in @BotFather
3. Update Pulumi config: `pulumi config set telegramBotToken --secret`
4. Redeploy: `pulumi up`

## Prerequisites

Before deploying, you need:

1. **Hetzner Cloud API Token** — Create a **dedicated project** for OpenClaw at [console.hetzner.cloud](https://console.hetzner.cloud/), then generate a Read & Write token (Security → API Tokens)
2. **Tailscale Auth Key** — Generate at [login.tailscale.com/admin/settings/keys](https://login.tailscale.com/admin/settings/keys) with **Reusable** and **Ephemeral** enabled. New to Tailscale? See [README.md](./README.md#first-time-tailscale-setup).
3. **Claude Max Setup Token** — Run `claude setup-token` in your terminal. Token starts with `sk-ant-oat01-...`. Note: setup tokens only have `user:inference` scope (missing `user:profile`), so `/status` won't show usage tracking. See [GitHub issue #4614](https://github.com/openclaw/openclaw/issues/4614).
4. **Local Tools** — Node.js 18+, Pulumi CLI (`curl -fsSL https://get.pulumi.com | sh`), Tailscale app, OpenClaw CLI (`brew install openclaw-cli`)

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

# 9. Install local CLI and connect to remote gateway (see "Local CLI" section above)
brew install openclaw-cli
openclaw onboard --non-interactive --accept-risk --flow quickstart --mode remote \
  --remote-url "wss://openclaw-vps.<tailnet>.ts.net" \
  --remote-token "$(pulumi stack output openclawGatewayToken --show-secrets)" \
  --skip-channels --skip-skills --skip-health --skip-ui --skip-daemon
# Then approve the CLI device via SSH (see "First-Time Access" below)
```

### First-Time Access (Device Pairing)

After deployment, you need to approve your browser as a trusted device (one-time):

1. **Open the gateway URL** in your browser:
   ```
   https://openclaw-vps.<tailnet>.ts.net/chat
   ```

2. **You'll see "pairing required"** — this is expected for first-time access

3. **Approve the device** (requires an already-paired device — CLI or SSH):
   ```bash
   # Via SSH (required for first-ever device)
   ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'openclaw devices list'
   ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'openclaw devices approve <request-id>'

   # Via local CLI (for subsequent devices, if CLI is already paired)
   openclaw devices list
   openclaw devices approve <request-id>
   ```

4. **Refresh the browser** — you're now authenticated via Tailscale identity

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

**Store your passphrase securely** — without it, you cannot manage or destroy your infrastructure.

### Pulumi State Backend

This project uses **local state storage** (`.pulumi/` directory, gitignored). For CI/CD or team use, consider migrating to [Pulumi Cloud](https://www.pulumi.com/docs/pulumi-cloud/) or [Pulumi ESC](https://www.pulumi.com/docs/esc/).

## Workspace Git Sync (Optional)

The agent's workspace (`~/.openclaw/workspace`) contains memories, notes, skills, and prompts. Syncing it to a private GitHub repo gives you version history, visibility into agent changes, and continuous backup.

### Setup Steps

1. **Create a private GitHub repo** (e.g., `openclaw-workspace`)

2. **Get the deploy key** (generated by Pulumi):
   ```bash
   cd pulumi
   pulumi stack output workspaceDeployPublicKey
   ```

3. **Add the deploy key to your GitHub repo**:
   ```bash
   # Via CLI (if gh is installed):
   pulumi stack output workspaceDeployPublicKey | gh repo deploy-key add --repo YOUR_USER/openclaw-workspace --title "OpenClaw VPS" -w -

   # Or via GitHub UI:
   # Go to your repo → Settings → Deploy keys → Add deploy key
   # Paste the public key, check "Allow write access"
   ```

4. **Configure the repo URL**:
   ```bash
   pulumi config set workspaceRepoUrl "git@github.com:YOUR_USER/openclaw-workspace.git"
   ```

5. **Deploy**:
   ```bash
   pulumi up
   ```

### How It Works

- A systemd timer runs every hour on the VPS
- It commits any workspace changes and pushes to the remote
- Commits are automatic with timestamps (e.g., `Auto-sync: 2026-01-15T14:00:00Z`)
- If nothing changed, no commit is created
- Uses an ED25519 deploy key (scoped to this single repo)

### Verify Workspace Sync

```bash
# Requires SSH (systemd timer management)
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'XDG_RUNTIME_DIR=/run/user/1000 systemctl --user status workspace-git-sync.timer'
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'XDG_RUNTIME_DIR=/run/user/1000 systemctl --user start workspace-git-sync.service'
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'cd ~/.openclaw/workspace && git log --oneline -5'
```

## Telegram Integration (Optional)

Configured via Pulumi secrets `telegramBotToken` and `telegramUserId`. If not set, deployment proceeds without Telegram. For bot creation and user ID setup, see [README.md](./README.md#telegram-bot-setup).

```bash
cd pulumi
pulumi config set telegramBotToken --secret   # From @BotFather
pulumi config set telegramUserId "123456789"  # Your numeric user ID
pulumi up
```

### Scheduled Tasks

When Telegram is configured, these cron jobs are automatically created:

| Job | Schedule | Purpose |
|-----|----------|---------|
| **Morning Digest** | 09:30 daily | Summarize what needs your attention today |
| **Evening Review** | 19:30 daily | Review accomplishments and pending items |
| **Night Shift** | 23:00 daily | Deep work: review notes, organize, triage tasks |
| **Weekly Planning** | 18:00 Sunday | Review past week, plan upcoming priorities |

All times are in **Europe/Berlin** timezone. Each job runs in an isolated session for fresh context.

### Verify Telegram

```bash
# Via local CLI (preferred)
openclaw channels status
openclaw cron list
openclaw cron run --force <job-id>
```

### Customizing Schedules

```bash
# All cron commands work via local CLI
openclaw cron list
openclaw cron remove "Night Shift"
openclaw cron add \
    --name "Custom Task" \
    --cron "0 14 * * *" \
    --tz "Europe/Berlin" \
    --session isolated \
    --message "Your custom prompt here" \
    --deliver --channel telegram --to "YOUR_USER_ID"
```

## Sandboxing

All sessions (including web chat) run in Docker containers with bridge networking and a custom sandbox image that includes Claude Code.

| | All sessions (web chat, cron, Telegram) |
|---|---|
| Runtime | Docker container (`openclaw-sandbox-custom:latest`) |
| Network | Bridge (outbound internet via Docker NAT) |
| Workspace | Read-write (mounted at `/workspace`) |
| Host filesystem | No access |
| Gateway config | Isolated (can't read `~/.openclaw/`) |
| Privilege escalation | Blocked |
| Claude Code | Pre-installed in custom image |

**Why bridge networking:** Sessions need outbound internet for web research and git push (creating PRs). The default `none` network breaks this. Bridge gives outbound access while keeping the container isolated from the host's network stack.

**What the sandbox protects against:** A prompt-injected session can't read gateway tokens, modify its own config, access session transcripts, escalate to root, or reach host-only services on localhost. It can still exfiltrate workspace data via HTTP or git push — see [Autonomous Agent Safety](docs/AUTONOMOUS-SAFETY.md) for a multi-agent design that would address this.

**Custom image:** Built during cloud-init from `openclaw-sandbox:bookworm-slim` with Claude Code and git config added. Rebuilt on every deploy.

**Config:**
```
agents.defaults.sandbox.mode: all
agents.defaults.sandbox.workspaceAccess: rw
agents.defaults.sandbox.docker.network: bridge
agents.defaults.sandbox.docker.image: openclaw-sandbox-custom:latest
```

## Troubleshooting

See [docs/TROUBLESHOOTING.md](./docs/TROUBLESHOOTING.md) for all troubleshooting procedures.

Quick diagnostics:
```bash
# Via local CLI (preferred)
openclaw health
openclaw doctor

# Via SSH (for systemd-level details)
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'XDG_RUNTIME_DIR=/run/user/1000 systemctl --user status openclaw-gateway'
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'XDG_RUNTIME_DIR=/run/user/1000 journalctl --user -u openclaw-gateway -n 50'

# Verify deployment
./scripts/verify.sh
```
