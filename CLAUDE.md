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
    ├── INTEGRATIONS.md              # Telegram, WhatsApp, Discord, Obsidian setup detail
    ├── NODE-EXEC.md                 # Remote Mac node host: setup, config, operations
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
| `discord` | discord | Configure Discord channel (bot token, guild allowlist) |
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

After redeploy, old devices appear as `openclaw-vps-N` (offline) in your Tailscale admin console.

1. Go to https://login.tailscale.com/admin/machines
2. Find offline `openclaw-vps*` devices
3. Click the device → Remove

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
| Discord bot token | (Optional) Connects to Discord | Discord Developer Portal → Bot → Token |
| Discord guild/user ID | (Optional) Guild and user IDs for allowlist | Discord Developer Mode → right-click → Copy ID |
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

Pulumi secrets: `telegramBotToken` (from @BotFather) + `telegramUserId`. Use `./scripts/get-telegram-id.sh` to discover user/group IDs. Creates two default cron jobs for the main agent (Europe/Berlin timezone):

| Job | Schedule | Purpose |
|-----|----------|---------|
| **Daily Standup** | 09:30 daily | Summarize what needs attention today |
| **Night Shift** | 23:00 daily | Review notes, organize, triage tasks, prepare morning summary |

```bash
pulumi config set telegramBotToken --secret && pulumi config set telegramUserId "123456789"
./scripts/provision.sh --tags telegram   # after editing group_vars/openclaw.yml for custom schedules
openclaw channels status && openclaw cron list
```

**Read [docs/INTEGRATIONS.md#telegram-integration](./docs/INTEGRATIONS.md#telegram-integration) in full when:** first-time Telegram setup, adding group chat routing, customizing cron schedules, or using `get-telegram-id.sh`.

## WhatsApp Integration (Optional)

Uses Baileys/WhatsApp Web protocol (not official Business API). **Sessions expire every ~14 days** — a health-check cron alerts via Telegram when re-authentication is needed. Set `deliver_channel: "whatsapp"` in the agent's `openclaw.yml` entry.

```bash
pulumi config set whatsappNiciPhone "+491234567890"
./scripts/provision.sh --tags config,agents,telegram,whatsapp
ssh ubuntu@openclaw-vps 'XDG_RUNTIME_DIR=/run/user/1000 openclaw channels login --channel whatsapp --qr-terminal'
ssh ubuntu@openclaw-vps 'XDG_RUNTIME_DIR=/run/user/1000 openclaw channels status --probe'
```

**Read [docs/INTEGRATIONS.md#whatsapp-integration](./docs/INTEGRATIONS.md#whatsapp-integration) in full when:** first-time WhatsApp setup (agent config, phone format) or re-scanning QR after session expiry.

## Discord Integration (Optional)

Built-in channel with automatic **per-channel session isolation** — each Discord channel gets its own session context with no extra config. No QR code; persistent bot token with no session expiry. Pulumi secrets: `discordBotToken`, `discordGuildId`, `discordUserId`.

```bash
pulumi config set discordBotToken --secret && pulumi config set discordGuildId "ID" && pulumi config set discordUserId "ID"
./scripts/provision.sh --tags discord
ssh ubuntu@openclaw-vps 'XDG_RUNTIME_DIR=/run/user/1000 openclaw channels status'
```

**Read [docs/INTEGRATIONS.md#discord-integration](./docs/INTEGRATIONS.md#discord-integration) in full when:** first-time Discord setup (bot creation, required intents, invite scopes) or troubleshooting Discord connection.

## Obsidian Headless Sync (Optional)

Two-way sync between agent workspaces and Obsidian Sync for mobile access. Requires Obsidian Sync subscription. **Auth token may expire if subscription lapses** — re-run `ob login` locally, update the Pulumi secret, and re-provision.

```bash
ob login   # locally, creates ~/.obsidian-headless/auth_token
pulumi config set obsidianAuthToken --secret && pulumi config set obsidianVaultPassword --secret
# Enable in openclaw.yml: obsidian_headless_enabled: true, obsidian_headless_agents: [main]
./scripts/provision.sh --tags obsidian-headless
ssh ubuntu@openclaw-vps 'XDG_RUNTIME_DIR=/run/user/1000 systemctl --user status obsidian-headless-main'
```

**Read [docs/INTEGRATIONS.md#obsidian-headless-sync](./docs/INTEGRATIONS.md#obsidian-headless-sync) in full when:** first-time Obsidian setup or diagnosing token expiry.

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

`config` -> `agents` -> `telegram` -> `whatsapp` -> `discord` -> `obsidian` -> `obsidian-headless` -> `qmd` -> `plugins` -> `sandbox` -> `workspace`

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
| Dev toolchain | Python 3, Node.js, git, git-lfs, ripgrep, fd, jq, yq, just, uv, pnpm, bd, sqlite3, pandoc, build-essential, ffmpeg, imagemagick, tmux, htop, tree, curl, wget, openssh-client |

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

> **Disabled by default.** Node exec runs arbitrary shell commands on your Mac with full user permissions — no sandbox. Enable with `node_exec_enabled: true` in `group_vars/all.yml`. Read [docs/SECURITY.md](./docs/SECURITY.md) section 5 first.

Architecture: VPS sandbox → `node-exec-mcp` (OPENCLAW_TOKEN auth, Tailscale Serve) → LaunchAgent on Mac. Each agent gets a scoped `mac_run` tool (`mac-<id>_run` for non-main agents).

**Key gotchas:**
- Two approval layers: gateway (`tools.exec.security/ask`) AND node (`~/.openclaw/exec-approvals.json`, must have `defaults.security: full`) — both must allow the command
- CWD defaults to `/tmp` — VPS workspace path doesn't exist on Mac; pass `workdir=/Users/<you>` explicitly
- LaunchAgent plist patched to `/opt/homebrew/bin/openclaw` symlink (survives `brew upgrade`)

```bash
./scripts/setup-mac-node.sh                     # one-time Mac setup (installs LaunchAgent, sets approvals)
./scripts/provision.sh --tags config,plugins    # install node-exec-mcp, pin node ID
openclaw node status / restart / stop           # manage Mac LaunchAgent
ssh ubuntu@openclaw-vps 'openclaw nodes status' # check from VPS side
```

**Read [docs/NODE-EXEC.md](./docs/NODE-EXEC.md) in full when:** first-time setup, debugging connection failures, resetting node ID after re-pairing, or changing exec approval settings.

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
