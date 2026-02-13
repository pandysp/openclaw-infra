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
| Personal Pulumi config values | Bug fixes in Ansible roles |
| Custom cron prompt tweaks (`group_vars/all.yml`) | New features (e.g., sandbox improvements) |
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
│  Your Machine   │     │         Hetzner VPS (~€6.59/mo)     │
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
| Process | Runs as unprivileged `ubuntu` user; all sessions [sandboxed](#sandboxing) in Docker (custom image with dev toolchain) |
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

Docker is installed on the server for **sandbox support** — all sessions run in Docker containers with bridge networking and a custom image (`openclaw-sandbox-custom:latest`) with a dev toolchain (Python 3, Node.js, git, git-lfs, ripgrep, fd, jq, yq, just, uv, pnpm, sqlite3, pandoc, build-essential, ffmpeg, imagemagick, tmux, htop, tree, curl, wget, openssh-client). The gateway itself runs natively.

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
│   ├── index.ts        # Main entrypoint (infra + Ansible trigger)
│   ├── server.ts       # Hetzner server resource
│   ├── firewall.ts     # Security rules (no inbound!)
│   └── user-data.ts    # Cloud-init (Tailscale-only bootstrap)
│
├── ansible/
│   ├── ansible.cfg         # Ansible config (pipelining, no host key check)
│   ├── requirements.yml    # Ansible Galaxy collections
│   ├── playbook.yml        # Main playbook
│   ├── group_vars/all.yml  # Non-secret defaults (model, cron prompts, etc.)
│   ├── inventory/
│   │   └── pulumi_inventory.py  # Dynamic inventory (Tailscale IP from Pulumi)
│   └── roles/
│       ├── system/    # apt packages, unattended-upgrades
│       ├── docker/    # Docker install, ubuntu→docker group
│       ├── ufw/       # Firewall rules
│       ├── openclaw/  # Binary install, onboard, daemon
│       ├── sandbox/   # Pull base image, build custom Docker image
│       ├── config/    # All `openclaw config set` commands
│       ├── telegram/  # Channel config, cron jobs (conditional)
│       ├── qmd/       # qmd semantic search: install, per-agent watchers
│       └── workspace/ # Deploy key, git sync timer (conditional)
│
├── scripts/
│   ├── provision.sh       # Ansible wrapper (reads secrets from Pulumi)
│   ├── setup-mac-node.sh  # One-time Mac node host installation
│   ├── verify.sh          # Post-deployment checks
│   └── backup.sh          # Data backup
│
└── docs/
    ├── BROWSER-CONTROL-PLANNING.md  # Future browser automation approaches
    ├── DOCS-REVIEW.md               # Official docs review tracking
    ├── SECURITY.md                  # Threat model
    └── TROUBLESHOOTING.md
```

### Pulumi vs Ansible Responsibilities

| Pulumi (infrastructure) | Ansible (configuration) |
|---|---|
| Hetzner VPS + firewall | System packages, Docker, UFW |
| SSH keys, gateway token | OpenClaw install + onboard |
| Workspace deploy key | Sandbox image build |
| Cloud-init (Tailscale only) | Gateway config, Telegram, cron |
| Triggers Ansible on server replacement | Workspace git sync |

### Ansible Tags

Use `./scripts/provision.sh --tags <tag>` to run specific roles:

| Tag | Role(s) | Day-2 use case |
|-----|---------|----------------|
| `system` | system | Update system packages |
| `docker` | docker | Docker upgrade or group changes |
| `ufw` | ufw | Firewall rule changes |
| `openclaw` | openclaw | Reinstall/update OpenClaw binary |
| `sandbox` | sandbox | Rebuild custom Docker image |
| `config` | config | Change model, sandbox mode, tool allowlist, elevated tools, auth settings, node exec |
| `telegram` | telegram | Update cron prompts or channel config |
| `obsidian` | obsidian | Clone/update Obsidian vaults in agent workspaces |
| `qmd` | qmd | Reinstall qmd, update watchers, force reindex |
| `workspace` | workspace | Deploy key rotation, sync changes |

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

### Deploy Infrastructure (Fresh Server)

```bash
cd pulumi
pulumi up    # Creates server + auto-triggers Ansible provisioning
```

### Provision / Re-provision (Day-2 Operations)

```bash
# Full provision
./scripts/provision.sh

# Config only (model, sandbox, auth settings)
./scripts/provision.sh --tags config

# Rebuild sandbox image
./scripts/provision.sh --tags sandbox -e force_sandbox_rebuild=true

# Update cron prompts (edit ansible/group_vars/all.yml first)
./scripts/provision.sh --tags telegram

# Dry run — see what would change
./scripts/provision.sh --check --diff
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
# Via Ansible (preferred)
./scripts/provision.sh --tags openclaw

# Or via SSH (manual)
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'OPENCLAW_NO_ONBOARD=1 OPENCLAW_NO_PROMPT=1 curl -fsSL https://openclaw.ai/install.sh | bash'
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart openclaw-gateway'
```

### View Cloud-Init Logs

Cloud-init now only handles Tailscale bootstrap (~1 minute). For provisioning details, check Ansible output.

```bash
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'sudo cat /var/log/cloud-init-openclaw.log'
```

### Run Security Audit

```bash
openclaw security audit --deep
```

**Expected output:** `0 critical · 0 warn · 1 info` (after applying permission fixes) — this deployment uses Tailscale identity auth with device pairing, which passes all security checks.

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
| Hetzner CX33 (x86, 4 vCPU, 8GB) | ~€5.49/mo |
| Hetzner Backups | ~€1.10/mo |
| Tailscale | Free (personal) |
| **Total** | **~€6.59/mo** |

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
| xAI API key | (Optional) Enables web search via Grok | x.ai/api → API Keys |
| Codex auth (`~/.codex/auth.json`) | (Optional) Powers Codex MCP servers for coding assistance | Run `codex login` locally, auto-deployed by provision.sh |

## Security DO's and DON'Ts

### DO

- Use a **dedicated Hetzner project** for OpenClaw (isolation from other infra)
- Keep all access through Tailscale
- Use `pulumi config set --secret` for sensitive values
- Run `./scripts/verify.sh` after deployment
- Check that no public ports are exposed
- Store your Pulumi passphrase in a password manager
- **Cloud-init log is minimal** (Tailscale bootstrap only, no secrets beyond auth key)
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
4. **Local Tools** — Node.js 18+, Pulumi CLI (`curl -fsSL https://get.pulumi.com | sh`), Ansible (`pip install ansible`), Tailscale app, OpenClaw CLI (`brew install openclaw-cli`)

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

# 5. (Optional) Enable web search, Telegram, or workspace sync
pulumi config set xaiApiKey --secret               # xAI API key for Grok web search
pulumi config set telegramBotToken --secret       # From @BotFather
pulumi config set telegramUserId "YOUR_USER_ID"   # From @userinfobot
pulumi config set workspaceRepoUrl "git@github.com:YOU/openclaw-workspace.git"

# 6. Preview deployment
pulumi preview

# 7. Deploy (creates server + auto-runs Ansible provisioning)
pulumi up

# 8. Verify deployment
cd ..
./scripts/verify.sh

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

## Web Search (Optional)

Configured via Pulumi secret `xaiApiKey`. If not set, deployment proceeds without web search. Uses Grok (xAI) for agentic search — search + read + synthesize in one API call. Get an API key at [x.ai/api](https://x.ai/api) (only needs `/v1/responses` endpoint + Language models).

```bash
cd pulumi
pulumi config set xaiApiKey --secret   # From x.ai/api → API Keys
pulumi up                               # Or: ./scripts/provision.sh --tags config
```

### Verify Web Search

```bash
# Via local CLI
openclaw health   # Should show web search as enabled
```

To disable, remove the key and re-provision:

```bash
cd pulumi
pulumi config rm xaiApiKey
./scripts/provision.sh --tags config
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

Edit `ansible/group_vars/all.yml` to change cron job prompts or schedules, then re-provision:

```bash
# After editing group_vars/all.yml:
./scripts/provision.sh --tags telegram

# Or via CLI for ad-hoc changes:
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

All sessions (including web chat) run in Docker containers with bridge networking and a custom sandbox image with a dev toolchain.

| | All sessions (web chat, cron, Telegram) |
|---|---|
| Runtime | Docker container (`openclaw-sandbox-custom:latest`) |
| Network | Bridge (outbound internet via Docker NAT) |
| Workspace | Read-write (mounted at `/workspace`) |
| Host filesystem | No access |
| Gateway config | Isolated (can't read `~/.openclaw/`) |
| Privilege escalation | Blocked (setuid bits stripped) |
| Dev toolchain | Python 3, Node.js, git, git-lfs, ripgrep, fd, jq, yq, just, uv, pnpm, sqlite3, pandoc, build-essential, ffmpeg, imagemagick, tmux, htop, tree, curl, wget, openssh-client |

**Why bridge networking:** Sessions need outbound internet for web research and git push (creating PRs). The default `none` network breaks this. Bridge gives outbound access while keeping the container isolated from the host's network stack.

**What the sandbox protects against:** A prompt-injected session can't read gateway tokens, modify its own config, access session transcripts, escalate to root, or reach host-only services on localhost. It can still exfiltrate workspace data via HTTP or git push — see [Autonomous Agent Safety](docs/AUTONOMOUS-SAFETY.md) for a multi-agent design that would address this.

**Custom image:** Built in two layers by the Ansible `sandbox` role: a base image (`openclaw-sandbox:trixie`) from `Dockerfile.base.j2`, then a custom image (`openclaw-sandbox-custom:latest`) from `Dockerfile.sandbox.j2` adding the dev toolchain. Neither image is pulled from a registry — both are built locally on the server. Rebuild with `./scripts/provision.sh --tags sandbox -e force_sandbox_rebuild=true`.

**Config:**
```
agents.defaults.sandbox.mode: all
agents.defaults.sandbox.workspaceAccess: rw
agents.defaults.sandbox.docker.network: bridge
agents.defaults.sandbox.docker.image: openclaw-sandbox-custom:latest
agents.defaults.sandbox.docker.readOnlyRoot: false
```

**Writable rootfs:** Sandbox containers have a writable rootfs (`readOnlyRoot: false`) so agents can install tools at runtime (`pip install`, `npm install -g`, `curl | bash`). The container runs as UID 1000 with `--cap-drop ALL`, so system directories (`/usr/bin/`, `/etc/`) remain unwritable — only `/home/node/` is writable via the overlay layer. Installs persist for the container's lifetime (hours) but are destroyed on container recreation. For persistent installs, agents can use `/workspace/.venv/` or `/workspace/.packages/`. See [docs/SECURITY.md](./docs/SECURITY.md#writable-rootfs-rationale) for the full security analysis.

**Tool access:** Sandbox sessions have access to all standard tool groups (openclaw, runtime, fs, sessions, memory, web, ui, automation, messaging, nodes). Elevated tools (shell, system commands) are enabled. When Telegram is configured, sensitive actions require approval from the configured Telegram user. Without Telegram, elevated tools are enabled without an approval gate. Change via `./scripts/provision.sh --tags config`.

## Remote Node Control (Mac)

Agents can run shell commands on your Mac via the node host feature. This enables tmux-based workflows where a VPS agent controls a Claude Code session on your local machine.

```
┌──────────────────────┐     ┌────────────────────────┐     ┌──────────────────────┐
│  VPS Agent           │     │  OpenClaw Gateway      │     │  Mac (Node Host)     │
│  (sandbox)           │────▶│  tools.exec.host=node  │────▶│  openclaw node run   │
│                      │     │                        │     │  (LaunchAgent)       │
│  uses workdir=/tmp   │     │  WebSocket via         │     │  tmux, claude        │
│  for node commands   │     │  Tailscale Serve       │     │  /opt/homebrew/bin   │
└──────────────────────┘     └────────────────────────┘     └──────────────────────┘
```

### Setup

**One-time Mac setup:**
```bash
./scripts/setup-mac-node.sh

# Then approve pairing on the VPS:
ssh ubuntu@openclaw-vps 'openclaw devices list'
ssh ubuntu@openclaw-vps 'openclaw devices approve <request-id>'

# Re-provision to auto-discover and pin the node ID:
./scripts/provision.sh --tags config
```

**What `setup-mac-node.sh` does:**
1. Resolves gateway hostname from Tailscale
2. Installs a persistent LaunchAgent (`ai.openclaw.node.plist`)
3. Configures node-side allowlists (broad `*` patterns)

### Config

Gateway-side (set by Ansible):
```
tools.exec.host: node           # Route to connected node (not sandbox)
tools.exec.security: full       # Tighten to "allowlist" after testing
tools.exec.ask: off             # Tighten to "on-miss" after testing
tools.exec.node: <auto>         # Auto-discovered during provisioning
```

Node-side (set by `setup-mac-node.sh`):
- `~/.openclaw/exec-approvals.json` — allowlist of permitted commands
- Managed via `openclaw approvals allowlist add/remove`

**Two approval layers:** Both the gateway (`tools.exec.security/ask`) AND the node (`exec-approvals.json`) must allow a command. Configure both.

### Known Issue: CWD Bug

The gateway sends the agent's VPS workspace path (e.g., `/home/ubuntu/.openclaw/workspace`) as the working directory for node commands. This path doesn't exist on macOS, causing `spawn /bin/sh ENOENT`.

**Workaround:** Agents must pass `workdir=/tmp` or `workdir=/Users/<user>` in every command targeting a node. Tracked in [openclaw/openclaw#15441](https://github.com/openclaw/openclaw/issues/15441).

### Operations

```bash
# Check node status
ssh ubuntu@openclaw-vps 'openclaw nodes status'

# Test from VPS
ssh ubuntu@openclaw-vps 'openclaw nodes run --cwd /tmp echo hello'

# Manage Mac node host
openclaw node status          # Check LaunchAgent
openclaw node restart         # Restart after updates
openclaw node stop            # Stop the service

# Reset node ID pin (e.g., after re-pairing)
ssh ubuntu@openclaw-vps 'openclaw config unset tools.exec.node'
./scripts/provision.sh --tags config   # Re-discovers and pins

# View node host logs
tail -f ~/.openclaw/logs/node.log
```

**Note:** The node host disconnects on gateway restarts but auto-reconnects (LaunchAgent handles restarts). If the node ID changes (re-pairing), re-run `./scripts/provision.sh --tags config` to update the pin.

## Semantic Search (qmd)

Each agent has a **qmd** instance providing local hybrid search (BM25 + vector + LLM reranking) over their workspace. Uses GGUF models (~1.5GB, auto-downloaded) — no API keys needed. Replaces the built-in `memorySearch` with 6 MCP tools per agent (18 total).

**Collections per agent:**
- `workspace` — all `.md`, `.txt`, `.csv` files in the workspace
- `memory` — memory directory (`.md` files only)
- `extracted-content` — text extracted from PDFs, images, `.docx`, `.xlsx`

**How it works:**
- A `qmd-watch-<agent_id>` systemd service watches each workspace with `inotifywait -r`
- On file changes: debounce → extract text from binaries → `qmd update` (BM25) → `qmd embed` (vectors)
- Embedding is serialized across agents via `flock` (memory-intensive: ~1.5GB model)
- Initial sync runs on service startup

**Tool count:** 114 total (96 existing + 18 qmd: 6 tools × 3 agents)

**Operations:**
```bash
# Rebuild qmd index (force re-embed all documents)
./scripts/provision.sh --tags qmd -e force_qmd_reindex=true

# Check watcher status
ssh ubuntu@openclaw-vps 'XDG_RUNTIME_DIR=/run/user/1000 systemctl --user status qmd-watch-main'

# View watcher logs
ssh ubuntu@openclaw-vps 'XDG_RUNTIME_DIR=/run/user/1000 journalctl --user -u qmd-watch-main -f'

# Verify qmd MCP servers in plugin config
ssh ubuntu@openclaw-vps 'openclaw config get plugins.entries.openclaw-mcp-adapter.config' | jq '.servers[] | select(.name | startswith("qmd"))'
```

**RAM consideration:** 3 qmd servers on 8GB (CX33). Models load on-demand per query, not resident. Concurrent heavy queries across 3 agents are unlikely. If RAM is tight, upgrade to CX43 (16GB, +€2.40/mo) via `pulumi config set hcloud:serverType cx43`.

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
