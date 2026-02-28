# OpenClaw Infrastructure

> AI assistant guide for deploying and managing OpenClaw on Hetzner Cloud with Tailscale.

## What This Project Is

OpenClaw is a self-hosted AI Agent gateway deployed on a Hetzner VPS with zero-trust networking via Tailscale. All access is through Tailscale—no public ports exposed.

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

Your Machine (Tailscale) → Hetzner VPS → Gateway (systemd, localhost:18789) via Tailscale Serve. No public ports. Hetzner firewall + UFW block all inbound except Tailscale.

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

Gateway runs via systemd (not Docker) as unprivileged user. Docker is used only for sandbox sessions (`openclaw-sandbox-custom:latest`). Auth: Tailscale identity + device pairing; no token needed. Fallback tokenized URL: `pulumi stack output tailscaleUrlWithToken --show-secrets`.

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
│   ├── group_vars/all.yml  # Non-secret defaults (model, agent types, server templates)
│   ├── group_vars/openclaw.yml        # Deployment-specific overrides (gitignored)
│   ├── group_vars/openclaw.yml.example  # Template for openclaw.yml
│   ├── inventory/
│   │   └── pulumi_inventory.py  # Dynamic inventory (Tailscale IP from Pulumi)
│   └── roles/
│       ├── system/    # apt packages, unattended-upgrades
│       ├── docker/    # Docker install, ubuntu→docker group
│       ├── ufw/       # Firewall rules
│       ├── openclaw/  # Binary install, onboard, daemon
│       ├── config/    # All `openclaw config set` commands
│       ├── agents/    # Create non-default agents, set bindings (conditional)
│       ├── telegram/  # Telegram channel config, cron jobs (conditional)
│       ├── whatsapp/  # WhatsApp channel config (conditional)
│       ├── obsidian/  # Clone/update Obsidian vaults in workspaces (conditional)
│       ├── obsidian-headless/  # Obsidian Sync daemon per workspace (conditional)
│       ├── qmd/       # qmd semantic search: install, per-agent watchers
│       ├── plugins/   # MCP adapter, Codex/Claude Code/Pi/qmd servers, deny rules
│       ├── sandbox/   # Pull base image, build custom Docker image
│       └── workspace/ # Deploy key, git sync timer (conditional)
│
├── scripts/
│   ├── provision.sh          # Ansible wrapper (reads secrets from Pulumi)
│   ├── setup-mac-node.sh     # One-time Mac node host installation
│   ├── setup-workspace.sh    # Create workspace repo + deploy key + Pulumi config
│   ├── get-telegram-id.sh    # Discover Telegram user/group IDs
│   ├── verify.sh             # Post-deployment checks
│   └── backup.sh             # Data backup
│
└── docs/
    ├── AUTONOMOUS-SAFETY.md         # Multi-agent safety architecture design
    ├── BROWSER-CONTROL-PLANNING.md  # Future browser automation approaches
    ├── DOCS-REVIEW.md               # Official docs review tracking
    ├── SECURITY.md                  # Threat model
    └── TROUBLESHOOTING.md
```

### Ansible Tags

Use `./scripts/provision.sh --tags <tag>` to run specific roles:

| Tag | Role(s) | Day-2 use case |
|-----|---------|----------------|
| `system` | system | Update system packages |
| `docker` | docker | Docker upgrade or group changes |
| `ufw` | ufw | Firewall rule changes |
| `openclaw` | openclaw | Reinstall/update OpenClaw binary |
| `config` | config | Change model, sandbox mode, tool allowlist, elevated tools, auth settings, node exec |
| `agents` | agents | Add/remove non-default agents, update Telegram bindings |
| `telegram` | telegram | Update cron prompts or Telegram channel config |
| `whatsapp` | whatsapp | Configure WhatsApp channel for agents using `deliver_channel: whatsapp` |
| `obsidian` | obsidian | Clone/update Obsidian vaults in agent workspaces |
| `obsidian-headless` | obsidian-headless | Update Obsidian Sync daemon config |
| `qmd` | qmd | Reinstall qmd, update watchers, force reindex |
| `plugins` | plugins | MCP adapter, Codex/Claude Code/Pi containers, GitHub MCP, deny rules |
| `sandbox` | sandbox | Rebuild custom Docker image |
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

**Important:** Always keep the local CLI, Mac node host, and VPS gateway on the same version. Version mismatches cause protocol errors (e.g., `system.run.prepare` not supported). After upgrading the gateway, upgrade local too:

```bash
# 1. Update VPS gateway (via Ansible — preferred)
./scripts/provision.sh --tags openclaw

# Or via SSH (manual)
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'OPENCLAW_NO_ONBOARD=1 OPENCLAW_NO_PROMPT=1 curl -fsSL https://openclaw.ai/install.sh | bash'
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart openclaw-gateway'

# 2. Update local CLI + node host to match
brew upgrade openclaw-cli
openclaw node restart   # if node exec is enabled
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

Default server type is **CX33** (4 vCPU, 8 GB RAM). Upgrade to **CX43** (8 vCPU, 16 GB RAM) when qmd semantic search is enabled — `deep_search` loads ~2.1 GB of GGUF models and the reranker needs CPU headroom (~2 min per query across 8 vCPUs). Change with `pulumi config set serverType cx43`.

| Resource | CX33 (default) | CX43 (recommended for qmd) |
|----------|---------------|---------------------------|
| Hetzner VPS | ~€5.49/mo | ~€9.49/mo |
| Hetzner Backups | ~€1.10/mo | ~€1.90/mo |
| Tailscale | Free (personal) | Free (personal) |
| **Total** | **~€6.59/mo** | **~€11.39/mo** |

## Secrets Reference

| Secret | Purpose | Where to regenerate |
|--------|---------|---------------------|
| Pulumi passphrase | Encrypts Pulumi state | Cannot recover — must redeploy if lost |
| Hetzner API token | Creates/manages VPS | console.hetzner.cloud → Project → API Tokens |
| Tailscale auth key | Joins server to your network | login.tailscale.com/admin/settings/keys |
| Claude setup token | Powers OpenClaw (flat fee) | `claude setup-token` in terminal |
| Gateway token | Authenticates browser and CLI sessions (cached after first use) | Auto-generated by Pulumi, view with `pulumi stack output openclawGatewayToken --show-secrets` |
| Telegram bot token | (Optional) Sends messages via Telegram | @BotFather on Telegram |
| Telegram user/group ID | (Optional) Your Telegram recipient ID | `./scripts/get-telegram-id.sh` or @userinfobot |
| WhatsApp phone number | (Optional) Agent's WhatsApp number (E.164) | `pulumi config set whatsappNiciPhone "+491234567890"` |
| Workspace deploy key | (Optional) Pushes workspace to GitHub | Auto-generated by Pulumi, view public key with `pulumi stack output workspaceDeployPublicKey` |
| xAI API key | (Optional) Enables web search via Grok | x.ai/api → API Keys |
| Groq API key | (Optional) Enables voice transcription via Whisper | console.groq.com → API Keys |
| Codex auth (`~/.codex/auth.json`) | (Optional) Powers Codex MCP servers for coding assistance | Run `codex login` locally, auto-deployed by provision.sh |
| Obsidian auth token | (Optional) Authenticates with Obsidian Sync API | `ob login` locally, copy from `~/.obsidian-headless/auth_token` |
| Obsidian vault password | (Optional) E2EE encryption for Obsidian Sync vaults | User-chosen password |

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

Update secret via `pulumi config set <key> --secret`, then `pulumi up`. Tailscale key: `tailscaleAuthKey`. Claude token: `claudeSetupToken` + `openclaw auth login` on server. Gateway token: redeploy + re-pair devices. Telegram bot: revoke via @BotFather, update `telegramBotToken`, redeploy.

## First-Time Setup

```bash
cd pulumi
pulumi stack init prod   # set a passphrase and save it — required for all future pulumi commands

# Required secrets
pulumi config set hcloud:token --secret
pulumi config set tailscaleAuthKey --secret
pulumi config set claudeSetupToken --secret

# Optional features
pulumi config set xaiApiKey --secret               # web search via Grok
pulumi config set telegramBotToken --secret        # Telegram integration
pulumi config set telegramUserId "YOUR_USER_ID"
pulumi config set workspaceRepoUrl "git@github.com:YOU/openclaw-workspace.git"

pulumi up          # creates server + auto-runs Ansible
cd ..
./scripts/verify.sh

# Connect local CLI
openclaw onboard --non-interactive --accept-risk --flow quickstart --mode remote \
  --remote-url "wss://openclaw-vps.<tailnet>.ts.net" \
  --remote-token "$(pulumi stack output openclawGatewayToken --show-secrets)" \
  --skip-channels --skip-skills --skip-health --skip-ui --skip-daemon
```

### Device Pairing

New browser or CLI client requires one-time approval:

1. Open `https://openclaw-vps.<tailnet>.ts.net/chat` — you'll see "pairing required"
2. Approve via SSH (required for very first device) or paired CLI:
   ```bash
   ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'openclaw devices list'
   ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'openclaw devices approve <request-id>'
   ```
3. Refresh browser — authenticated via Tailscale identity

Fallback if pairing fails: `pulumi stack output tailscaleUrlWithToken --show-secrets`

### Pulumi Passphrase

`PULUMI_CONFIG_PASSPHRASE` env var must be set for every `pulumi` command (encrypts local state). Export for session or set per-command: `PULUMI_CONFIG_PASSPHRASE="..." pulumi up`. Without it, all Pulumi commands fail.

## Workspace Git Sync (Optional)

The agent's workspace (`~/.openclaw/workspace`) contains memories, notes, skills, and prompts. Syncing it to a private GitHub repo gives you version history, visibility into agent changes, and continuous backup.

**Multi-agent note:** Workspace definitions are auto-generated from `openclaw_agents` (see [Multi-Agent Setup](#multi-agent-setup-optional)). Each agent gets a workspace at `~/.openclaw/workspace-<id>` (or `~/.openclaw/workspace` for main). Run `setup-workspace.sh <agent-id>` for each agent that needs git sync.

### Setup

```bash
./scripts/setup-workspace.sh <agent-id>   # creates repo, deploy key, Pulumi config
pulumi up   # or: ./scripts/provision.sh --tags workspace
```

Hourly systemd timer commits workspace changes and pushes. Deploy key: `pulumi stack output workspaceDeployPublicKey`.

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

Configured via Pulumi secrets `telegramBotToken` and `telegramUserId`. If not set, deployment proceeds without Telegram. For bot creation, see [README.md](./README.md#telegram-bot-setup).

```bash
cd pulumi
pulumi config set telegramBotToken --secret   # From @BotFather
pulumi config set telegramUserId "123456789"  # Your numeric user ID
pulumi up
```

### Getting Telegram IDs

Use `./scripts/get-telegram-id.sh` to discover user IDs and group IDs. The script briefly pauses the gateway (~10s), polls the Telegram API for a message you send, displays the IDs, and restarts the gateway.

```bash
# Discover IDs (prints chat ID, user ID, group title)
./scripts/get-telegram-id.sh

# Discover and set a Pulumi config key in one step
./scripts/get-telegram-id.sh --set-config telegramPhGroupId
```

Alternatively, message **@userinfobot** on Telegram to get a user ID manually.

### Scheduled Tasks

When Telegram is configured, these default cron jobs are created for the main agent:

| Job | Schedule | Purpose |
|-----|----------|---------|
| **Daily Standup** | 09:30 daily | Summarize what needs attention today |
| **Night Shift** | 23:00 daily | Review notes, organize, triage tasks, prepare morning summary |

All times are in **Europe/Berlin** timezone. Each job runs in an isolated session for fresh context. Override in `openclaw.yml` for additional agents or custom schedules (see `openclaw.yml.example`).

### Verify Telegram

```bash
# Via local CLI (preferred)
openclaw channels status
openclaw cron list
openclaw cron run --force <job-id>
```

### Customizing Schedules

Edit `ansible/group_vars/openclaw.yml` to change cron job prompts or schedules, then re-provision:

```bash
# After editing group_vars/openclaw.yml:
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

## WhatsApp Integration (Optional)

Agents can use WhatsApp instead of Telegram by setting `deliver_channel: "whatsapp"` in their agent definition. WhatsApp is a bundled OpenClaw plugin using the Baileys/WhatsApp Web protocol.

### Setup

1. **Configure the agent** in `openclaw.yml`:
   ```yaml
   - id: "nici"
     deliver_channel: "whatsapp"
     deliver_to: "{{ whatsapp_nici_phone | default('') }}"  # E.164 format
   ```

2. **Set the phone number** in Pulumi:
   ```bash
   cd pulumi
   pulumi config set whatsappNiciPhone "+491234567890"
   ```

3. **Provision**:
   ```bash
   ./scripts/provision.sh --tags config,agents,telegram,whatsapp
   ```

4. **Scan QR code** (required after first provision and every ~14 days):
   ```bash
   ssh ubuntu@openclaw-vps 'XDG_RUNTIME_DIR=/run/user/1000 openclaw channels login --channel whatsapp --qr-terminal'
   ```

### Session Expiry

WhatsApp Web sessions expire approximately every 14 days. A health-check cron job on the main agent monitors WhatsApp status every 30 minutes and alerts via Telegram when re-authentication is needed.

### Verify WhatsApp

```bash
# Check channel status
ssh ubuntu@openclaw-vps 'XDG_RUNTIME_DIR=/run/user/1000 openclaw channels status --probe'

# Check bindings
ssh ubuntu@openclaw-vps 'XDG_RUNTIME_DIR=/run/user/1000 openclaw agents list --json --bindings'
```

## Obsidian Headless Sync (Optional)

Near-real-time two-way sync between agent workspaces and Obsidian Sync, enabling mobile access to agent notes via the Obsidian app. Requires an Obsidian Sync subscription and the `obsidian-headless` npm package. If secrets are not set, deployment proceeds without Obsidian Sync.

### Setup

1. **Install `obsidian-headless` locally** and authenticate:
   ```bash
   npm install -g obsidian-headless
   ob login    # creates ~/.obsidian-headless/auth_token
   ```

2. **Set Pulumi secrets:**
   ```bash
   cd pulumi
   pulumi config set obsidianAuthToken --secret    # from ~/.obsidian-headless/auth_token
   pulumi config set obsidianVaultPassword --secret # E2E encryption password (your choice)
   ```

3. **Enable in `openclaw.yml`:**
   ```yaml
   obsidian_headless_enabled: true
   obsidian_headless_agents:
     - main
   ```

4. **Provision:**
   ```bash
   ./scripts/provision.sh --tags obsidian-headless
   ```

### Verify

```bash
# Check daemon status (one service per agent)
ssh ubuntu@openclaw-vps 'XDG_RUNTIME_DIR=/run/user/1000 systemctl --user status obsidian-headless-main'

# View sync logs
ssh ubuntu@openclaw-vps 'XDG_RUNTIME_DIR=/run/user/1000 journalctl --user -u obsidian-headless-main -f'
```

### Token Expiry

The Obsidian auth token may expire if the Obsidian Sync subscription lapses or is renewed. Re-run `ob login` locally, update the Pulumi secret, and re-provision with `--tags obsidian-headless`.

## Multi-Agent Setup (Optional)

By default, a single `main` agent is configured. To add more agents, define `openclaw_agents` in `openclaw.yml` (see `openclaw.yml.example`).

### How It Works

`openclaw_agents` is the **single source of truth**. The `playbook.yml` pre_tasks automatically derive:

| Derived variable | Generated from | Used by |
|---|---|---|
| `_openclaw_mcp_servers` | `openclaw_agents` x `openclaw_mcp_server_types` | plugins role (MCP server config, deny rules) |
| `_openclaw_workspaces` | `openclaw_agents` + provision.sh secrets | workspace, qmd, obsidian, plugins roles |
| `_openclaw_obsidian_github_tokens` | `openclaw_agents` + GitHub PATs | obsidian role |

**Naming conventions** (mechanical, from agent ID):

| Resource | main | other (e.g., `bob`) |
|---|---|---|
| MCP server | `github`, `codex`, `claude` | `github-bob`, `codex-bob`, `claude-bob` |
| Workspace dir | `~/.openclaw/workspace` | `~/.openclaw/workspace-bob` |
| Deploy key var | `workspace_deploy_key` | `workspace_bob_deploy_key` |
| GitHub token var | `github_token` | `github_token_bob` |

### Adding an Agent

1. Add the agent to `openclaw_agents` in `openclaw.yml`
2. Wire per-agent secrets through `scripts/provision.sh` (Pulumi config or env vars)
3. Run `./scripts/provision.sh`

MCP servers, workspaces, deny rules, and token mappings are generated automatically. Cron jobs and Obsidian vaults remain manual (personal config — add to `openclaw.yml`).

### Role Ordering

`config` -> `agents` -> `telegram` -> `whatsapp` -> `obsidian` -> `obsidian-headless` -> `qmd` -> `plugins` -> `sandbox` -> `workspace`

Telegram must run immediately after agents (prevents message misrouting). Obsidian before qmd (vaults must exist before watchers start). Plugins after qmd (qmd binary needed for MCP registration).

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

**Network:** Bridge (outbound internet for web research/git push). MCP containers (Codex, Claude Code, Pi) use a separate `codex-proxy-net`. Sandbox containers can't reach the credential proxy.

**Custom image:** Two layers built locally: base (`openclaw-sandbox:trixie`, Debian 13) + custom (`openclaw-sandbox-custom:latest`). Neither pulled from registry. Rebuild: `./scripts/provision.sh --tags sandbox -e force_sandbox_rebuild=true`.

**Config:**
```
agents.defaults.sandbox.mode: all
agents.defaults.sandbox.workspaceAccess: rw
agents.defaults.sandbox.docker.network: bridge
agents.defaults.sandbox.docker.image: openclaw-sandbox-custom:latest
agents.defaults.sandbox.docker.readOnlyRoot: false
```

**Writable rootfs** (`readOnlyRoot: false`): UID 1000 + `--cap-drop ALL` blocks writes to system dirs; only `/home/node/` writable. Runtime installs (`pip install`, `npm install -g`) persist for the container's lifetime. Persistent installs: `/workspace/.venv/` or `/workspace/.packages/`. See [docs/SECURITY.md](./docs/SECURITY.md#writable-rootfs-rationale).

**Tool access:** All standard tool groups enabled; elevated tools enabled (with Telegram approval gate if configured). Change via `./scripts/provision.sh --tags config`.

## Remote Node Control (Mac)

> **Disabled by default.** Node exec lets agents run arbitrary shell commands on your local machine with your user's full permissions — no sandbox. Enable with `node_exec_enabled: true` in `ansible/group_vars/all.yml` only after reading the security warnings there and in [docs/SECURITY.md](./docs/SECURITY.md) section 5.

Agents can run shell commands on your Mac via the node host feature. This enables tmux-based workflows where a VPS agent controls a Claude Code session on your local machine.

Agents access node exec via the `mac_run` MCP tool (provided by `node-exec-mcp`), not the built-in exec tool. Each agent gets its own scoped tool: `mac_run` (main), `mac-manon_run`, etc.

```
┌──────────────────────┐     ┌────────────────────────┐     ┌──────────────────────┐
│  VPS Agent           │     │  MCP Adapter           │     │  Mac (Node Host)     │
│  (sandbox)           │────▶│  node-exec-mcp (stdio) │────▶│  openclaw node run   │
│                      │     │                        │     │  (LaunchAgent)       │
│  calls mac_run tool  │     │  OPENCLAW_TOKEN auth   │     │  tmux, claude        │
│  (cwd defaults /tmp) │     │  Tailscale Serve       │     │  /opt/homebrew/bin   │
└──────────────────────┘     └────────────────────────┘     └──────────────────────┘
```

### Setup

**1. Enable in config** (edit `ansible/group_vars/all.yml`):
```yaml
node_exec_enabled: true
```

**2. One-time Mac setup:**
```bash
./scripts/setup-mac-node.sh

# Then approve pairing on the VPS:
ssh ubuntu@openclaw-vps 'openclaw devices list'
ssh ubuntu@openclaw-vps 'openclaw devices approve <request-id>'

# Re-provision to install node-exec-mcp and auto-discover the node ID:
./scripts/provision.sh --tags config,plugins
```

**What `setup-mac-node.sh` does:**
1. Resolves gateway hostname from Tailscale
2. Installs a persistent LaunchAgent (`ai.openclaw.node.plist`)
3. Patches LaunchAgent to use stable Homebrew symlink (survives `brew upgrade`)
4. Sets node-side exec approvals to auto-approve all commands (`defaults.security: full`)

### Config

Gateway-side (set by Ansible):
```
tools.exec.host: sandbox        # Built-in exec stays sandboxed (agents use mac_run MCP tool instead)
tools.exec.security: full       # Tighten to "allowlist" after testing
tools.exec.ask: off             # Tighten to "on-miss" after testing
tools.exec.node: <auto>         # Auto-discovered during provisioning; used by node-exec-mcp
```

Node-side (set by `setup-mac-node.sh`):
- `~/.openclaw/exec-approvals.json` — `defaults.security: full` (auto-approve all commands)

**How auth works:** The `node-exec-mcp` server receives `OPENCLAW_TOKEN` (the gateway token) as an env var, which `openclaw nodes run` uses to authenticate with the gateway. Without this token, the connection fails with "pairing required".

**Two approval layers:** Both the gateway (`tools.exec.security/ask`) AND the node (`exec-approvals.json`) must allow a command. Configure both.

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

Each agent has a **qmd** instance providing local hybrid search (BM25 + vector + LLM reranking) over their workspace. Uses GGUF models (~1.5GB, auto-downloaded) — no API keys needed. Replaces the built-in `memorySearch` with 6 MCP tools per agent (6 × N_agents total).

**Collections per agent:**
- `workspace` — all `.md`, `.txt`, `.csv` files in the workspace
- `memory` — memory directory (`.md` files only)
- `extracted-content` — text extracted from PDFs, images, `.docx`, `.xlsx`

**Tool count:** `N_agents × Σ(tools_per_server_type)`. Per agent: github: 26, codex: 2, claude-code: 2, pi: 2, qmd: 6. Check `openclaw_mcp_server_types` in `group_vars/all.yml`.

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

**RAM:** `deep_search` loads ~2.1GB GGUF models on-demand. CX43 (16 GB) recommended for multi-agent; CX33 (8 GB) works for single-agent. 2 GB swap configured.

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
