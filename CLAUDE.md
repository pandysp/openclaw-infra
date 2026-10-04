# OpenClaw Infrastructure

> AI assistant guide for deploying and managing OpenClaw on Hetzner Cloud with Tailscale.

## What This Project Is

OpenClaw is a self-hosted AI Agent gateway deployed on a Hetzner VPS with zero-trust networking via Tailscale. All access is through Tailscale—no public ports exposed.

## Repository

This repo is `pandysp/openclaw-infra`. Personal deployment config (Pulumi secrets, `group_vars/openclaw.yml`) stays gitignored or in Pulumi encrypted state.

## Architecture

Your Machine (Tailscale) → Hetzner VPS → Gateway (systemd, localhost:18789) via Tailscale Serve. No public ports. Hetzner firewall + UFW block all inbound except Tailscale.

## Security Model

| Layer | Measure |
|-------|---------|
| Network (infrastructure) | Hetzner cloud firewall blocks ALL inbound |
| Network (host) | UFW: deny incoming, allow only tailscale0 interface |
| Access | Tailscale-only (no public SSH, no public ports) |
| Process | Runs as the `ubuntu` user (passwordless sudo). Agent turns run Claude Code on the host, or in a per-agent container with `openclaw_claude_cli_enabled: true`; OpenClaw's own tools run [sandboxed](#sandboxing) in Docker ([details](./docs/SECURITY.md#4-agent-host-command-abuse)) |
| Auth | Tailscale identity + device pairing |
| Secrets | Pulumi encrypted config (never in git) |
| Gateway | Binds localhost only, proxied via Tailscale Serve |

For the full threat model, see [docs/SECURITY.md](./docs/SECURITY.md).

Gateway runs via systemd (not Docker) as unprivileged user. Docker runs OpenClaw's sandboxed tools (`openclaw-sandbox-custom:latest`) and the isolated workspace Git jobs. Auth: Tailscale identity + device pairing; no token needed. Resolve pairing requests over Tailscale SSH; do not print token-bearing URLs.

## Directory Structure

```
openclaw-infra/
├── CLAUDE.md           # This file - AI assistant guide
├── README.md           # Human overview
├── package.json        # Node.js dependencies
├── tsconfig.json       # TypeScript config
│
├── pulumi/            # Hetzner server, firewall, cloud-init; triggers Ansible
│
├── ansible/
│   ├── playbook.yml        # Main playbook
│   ├── group_vars/all.yml  # Non-secret defaults (models, agent types, server templates)
│   ├── group_vars/openclaw.yml        # Deployment-specific overrides (gitignored)
│   ├── group_vars/openclaw.yml.example  # Template for openclaw.yml
│   ├── inventory/          # Inventory from authenticated provisioner inputs
│   └── roles/              # One role per tag, see Ansible Tags below
│
├── scripts/
│   ├── provision.sh        # Ansible wrapper (reads secrets from Pulumi)
│   ├── verify.sh           # Post-deployment checks
│   ├── tests/              # Unit tests: python3 -m unittest discover -s scripts/tests
│   └── ...                 # Setup, staging and check scripts; each starts with a usage comment
│
└── docs/                   # Durable topic guides only; investigations, specs,
                            # evidence and handoffs belong in scratch
```

### Ansible Tags

Use `./scripts/provision.sh --tags <tag>` to run specific roles:

| Tag | Role(s) | Day-2 use case |
|-----|---------|----------------|
| `system` | system | Update system packages |
| `docker` | docker | Docker upgrade or group changes |
| `ufw` | ufw | Firewall rule changes |
| `openclaw` | openclaw | Reinstall/update OpenClaw binary |
| `config` | config | Change model, sandbox mode, tool allowlist, elevated tools, auth settings |
| `agents` | agents | Add/remove non-default agents, update Telegram bindings |
| `telegram` | telegram | Update cron prompts or Telegram channel config |
| `whatsapp` | whatsapp | Configure WhatsApp channel for agents using `deliver_channel: whatsapp` |
| `discord` | discord | Configure Discord channel (bot token, guild allowlist) |
| `obsidian-headless` | obsidian-headless | Update Obsidian Sync daemon config |
| `qmd` | qmd | Reinstall qmd, update watchers, force reindex |
| `plugins` | plugins | MCP adapter, GitHub/qmd servers, GitHub token proxy for sandbox git, deny rules |
| `sandbox` | sandbox | Rebuild custom Docker image |
| `workspace` | workspace | Deploy key rotation, sync changes |

## Local CLI

The OpenClaw CLI is installed locally and configured to talk to the remote gateway over Tailscale. **Prefer `openclaw` commands over SSH** for gateway operations — it's faster and avoids the SSH round-trip.

```bash
# Install
brew install openclaw-cli

# Configure once, from this repo with its backend environment loaded.
# Replace <tailnet> below. Credentials travel through stdin, never command arguments.
(
  set -euo pipefail
  GATEWAY_TOKEN=$(cd pulumi && pulumi stack output openclawGatewayToken --stack prod --show-secrets)
  printf '%s' "$GATEWAY_TOKEN" |
    jq -Rse 'if length == 0 then error("Gateway token is empty")
      else {gateway: {mode: "remote", remote: {
        url: "wss://openclaw-vps.<tailnet>.ts.net", token: .}}} end' |
    openclaw config patch --stdin
)

# Trigger pairing, then approve only this CLI's matching device/request.
openclaw health
tailscale ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'openclaw devices list --json'
tailscale ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'openclaw devices approve <request-id>'
```

After pairing, CLI commands work directly:

```bash
openclaw health              # Gateway health check
openclaw doctor              # Diagnostics and quick fixes
openclaw devices list        # List paired devices
openclaw cron list --all     # List enabled and disabled scheduled jobs
openclaw security audit      # Run security audit (add --deep for thorough scan)
openclaw status              # Session health
```

**When to still use SSH:** systemd service management (`systemctl`, `journalctl`), system-level operations (`sudo`), updating the OpenClaw binary on the server.

## Common Operations

### Phoenix Safety

- `STAGING_PRIVATE_REPOSITORY` names a dedicated private fixture (`pandysp/openclaw-staging-vault`: one README, no notes, no secrets) readable by the staging PAT. Real notes never enter staging. Direct MCP checks are read-only; inference disables gateway and native Claude tools; write tests belong in the staging workspace repositories. Public reads do not prove private access.
- Staging agents are permanently read-only: `openclaw_tools_allow` limits every session (heartbeats included) to the two `*_get_file_contents` MCP reads, `tools.alsoAllow` is cleared, and `openclaw_cli_backends` passes `--tools "" --strict-mcp-config` to the Claude CLI so no native tool can write private content into the publicly backed-up staging workspaces. The smoke test verifies this policy before any MCP read.
- Obsidian Sync is out of Phoenix scope (decided 2026-09-25). The role picks the cloud vault by agent ID (`<agent>-workspace`), so a staging run would attach the public staging backup to the real vault; staging therefore never runs it and receives no Obsidian credentials. Check it read-only on production instead: `systemctl --user status obsidian-headless-main`, `ob sync-status --path ~/.openclaw/workspace`.
- Phoenix runs `scripts/test-workspace-isolation.sh` on the staging VPS: a controlled Git hook in the public staging workspace proves the sync unit executes hooks as UID 1000 with no capabilities, no host config or Docker socket, and no route to the credential proxy (an unrestricted control container proves the proxy is reachable), then confirms the push by anonymous readback and removes its fixture.
- The staging server joins the tailnet as `tag:openclaw-staging` with a key minted per run by the CI OAuth client (`TS_OAUTH_CLIENT_ID`/`TS_OAUTH_SECRET`, tag `tag:ci`): single use, ephemeral, pre-authorized, one hour. No stored auth key exists to expire.
- The tailnet policy (Tailscale admin console, not this repo) makes `tag:ci` the owner of `tag:openclaw-staging` and isolates that tag both ways: it has **no source rights** (a one-hour agent host with real credentials must not reach your devices, production or other servers), and only `tag:ci` may reach it (SSH as `ubuntu`), so it never even sees your devices; there is no owner SSH into a running Phoenix server. `tag:ci` itself reaches only `tag:server` and `tag:openclaw-staging`, never personal devices or the untagged production server. Policy tests pin all of this, and `Resolve staging host` fails if the staging server sees any peer other than a CI runner. `tag:server` stays the other project's tag.
- `staging.yml` binds the run name, authenticated Tailscale peer and created server IP. Ansible also uses the peer's advertised host keys; a separate SSH probe alone does not authenticate Ansible's connection.
- Teardown first releases the run's own stale Pulumi lock (a timed-out step kills Pulumi and leaves one), then destroys. It deletes only the proven node's API device ID, then reads it back as absent. If deploy failed before proving a node, only a device with the run's exact name **and** exactly `tag:openclaw-staging` (mintable only via `tag:ci`) counts as the run's own; a name alone never does. Unrelated tailnet changes do not invalidate cleanup. Missing ownership or failed cleanup keeps the staging checkpoint for investigation.
- Deploy retries only on Hetzner `resource_unavailable` (placement rejected before any server exists): production's `cx43` in `nbg1 → fsn1 → hel1`, then the same-class `cpx42` in the same order. Every other failure stops immediately.
- Scheduled orphan cleanup only inventories leftovers. Never restore prefix-wide deletion or remove a checkpoint before all owned cleanup checks pass.

### Deploy Infrastructure (Fresh Server)

Load the [backend environment](./README.md#pulumi-backend) before any Pulumi or provisioning command. This deployment uses R2 and a stack passphrase, not Pulumi Cloud.

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

Check mode is partial: the config role applies settings through shell steps that check mode skips, so a dry run cannot show config changes such as the default model or the model allowlist. Read the `UPDATED:` lines of a real run instead. Heartbeat changes (agents role) do show up in a dry run.

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

**Expected output:** 0 critical. Run it **on the VPS via SSH** (running locally audits your Mac instead). Accepted warnings, all deliberate: `config.insecure_or_dangerous_flags` (`dangerouslyAllowExternalBindSources` for sandbox bind mounts), `tools.exec.security_full_configured` (gateway exec gated by `elevated=false` + node-side approvals), and `security.trust_model.multi_user_heuristic` (Telegram/Discord group allowlists — personal deployment, one trusted operator). `--deep` probes occasionally add a transient warning; rerun before acting on it.

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

Default server type is **CX43** (8 vCPU, 16 GB RAM, ~€9.49/mo). Change with `pulumi config set serverType <type>`.

| Resource | Cost |
|----------|------|
| Hetzner VPS (CX43) | ~€9.49/mo |
| Hetzner Backups | ~€1.90/mo |
| Tailscale | Free (personal) |
| **Total** | **~€11.39/mo** |

## Secrets Reference

| Secret | Purpose | Where to regenerate |
|--------|---------|---------------------|
| R2 access keys | Authenticate to the Pulumi state bucket | Cloudflare → R2 → API tokens (bucket-scoped) |
| Pulumi config passphrase | Decrypts this stack's secrets | Load the current value from private `.env`; coordinate any rotation |
| Hetzner API token | Creates/manages VPS | console.hetzner.cloud → Project → API Tokens |
| Tailscale auth key | Joins server to your network | login.tailscale.com/admin/settings/keys |
| Claude setup token | Powers OpenClaw (flat fee) | `claude setup-token` in terminal |
| GitHub tokens (`githubToken`, `githubToken<Agent>`) | (Optional) Per-agent GitHub MCP tools and sandbox git push via the token proxy | GitHub → Settings → Fine-grained tokens |
| Gateway token | Authenticates browser and CLI sessions (cached after first use) | Auto-generated by Pulumi, view with `pulumi stack output openclawGatewayToken --show-secrets` |
| Telegram bot token | (Optional) Sends messages via Telegram | @BotFather on Telegram |
| Telegram user/group ID | (Optional) Your Telegram recipient ID | `./scripts/get-telegram-id.sh` or @userinfobot |
| WhatsApp phone number | (Optional) Agent's WhatsApp number (E.164) | `pulumi config set whatsappNiciPhone "+491234567890"` |
| Discord bot token | (Optional) Connects to Discord | Discord Developer Portal → Bot → Token |
| Discord guild/user ID | (Optional) Guild and user IDs for allowlist | Discord Developer Mode → right-click → Copy ID |
| Workspace deploy key | (Optional) Pushes workspace to GitHub | Auto-generated by Pulumi, view public key with `pulumi stack output workspaceDeployPublicKey` |
| xAI API key | (Optional) Enables web search via Grok | x.ai/api → API Keys |
| Groq API key | (Optional) Enables voice transcription via Whisper | console.groq.com → API Keys |
| Gemini API key | (Optional) Enables image generation via Google Gemini | aistudio.google.com → API Keys |
| Obsidian auth token | (Optional) Authenticates with Obsidian Sync API | `ob login` locally, copy from `~/.obsidian-headless/auth_token` |
| Obsidian vault password | (Optional) E2EE encryption for Obsidian Sync vaults | User-chosen password |

## Security DO's and DON'Ts

### DO

- Use a **dedicated Hetzner project** for OpenClaw (isolation from other infra)
- Keep all access through Tailscale
- Use `pulumi config set --secret` for sensitive values
- Run `./scripts/verify.sh` after deployment
- Check that no public ports are exposed
- Scope R2 access keys to the state bucket; load backend credentials from private, ignored `.env` via `direnv exec`
- Know that the Tailscale auth key stays readable on the server: [auth key exposure](./README.md#tailscale-auth-key-exposure)
- **Monitor Tailscale admin console** for unauthorized devices: https://login.tailscale.com/admin/machines
- **Rotate Tailscale auth keys periodically** (see [Key Rotation](#key-rotation) below)
- **Review paired OpenClaw devices** regularly: `openclaw devices list` (via local CLI)

### DON'T

- Never share Hetzner tokens between high-risk and production projects
- Never add inbound firewall rules
- Never bind OpenClaw to 0.0.0.0
- Never commit `.env` files or API keys
- Never use password SSH authentication

### Key Rotation

Update secret via `pulumi config set <key> --secret`, then `pulumi up`. Tailscale key: `tailscaleAuthKey`. Claude login: provisioning installs `claudeSetupToken` only on a server without a login, in `~/.claude/shared/auth/.credentials.json`, and never replaces an existing login. A setup token cannot renew itself; after a fresh install, sign in once on the VPS (`claude auth login`; the shell already points it at the shared folder) for a login that renews itself. To replace a login, sign in again, or delete that file and run `./scripts/provision.sh --tags openclaw` to fall back to the setup token. After changing `claudeSetupToken`, `--tags config` refreshes the agents' Anthropic auth profiles. Gateway token: redeploy + re-pair devices. Telegram bot: revoke via @BotFather, update `telegramBotToken`, redeploy.

## First-Time Setup

```bash
cd pulumi
pulumi login "${PULUMI_BACKEND_URL:?Load the backend environment from README.md first}"
pulumi stack init prod

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

```

Connect the local CLI using the [one-time setup above](#local-cli).

### Device Pairing

New browser or CLI client requires one-time approval:

1. Open `https://openclaw-vps.<tailnet>.ts.net/chat` — you'll see "pairing required"
2. Approve via SSH (required for very first device) or paired CLI:
   ```bash
   ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'openclaw devices list'
   ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'openclaw devices approve <request-id>'
   ```
3. Refresh browser — authenticated via Tailscale identity

If pairing fails, inspect the matching request over Tailscale SSH. Do not print or share token-bearing URLs.

## Workspace Git Sync (Optional)

The agent's workspace (`~/.openclaw/workspace`) contains memories, notes, skills, and prompts. Syncing it to a private GitHub repo gives you version history, visibility into agent changes, and continuous backup.

**Multi-agent note:** Workspace definitions are auto-generated from `openclaw_agents` (see [Multi-Agent Setup](#multi-agent-setup-optional)). Each agent gets a workspace at `~/.openclaw/workspace-<id>` (or `~/.openclaw/workspace` for main). Run `setup-workspace.sh <agent-id>` for each agent that needs git sync.

### Setup

```bash
./scripts/setup-workspace.sh <agent-id>   # creates repo, deploy key, Pulumi config
pulumi up   # or: ./scripts/provision.sh --tags workspace
```

A per-agent hourly systemd timer (`workspace-git-sync-<agent-id>`) runs Git in a one-shot isolated container (only that workspace and its deploy key mounted, UID 1000, no capabilities, outbound SSH to GitHub only). It commits local changes, merges `origin/main`, and pushes; a conflict fails the run and preserves both histories. Deploy key: `pulumi stack output workspaceDeployPublicKey`.

### Verify Workspace Sync

```bash
# Requires SSH (systemd timer management). Replace `main` with the agent ID.
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'XDG_RUNTIME_DIR=/run/user/1000 systemctl --user status workspace-git-sync-main.timer'
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'XDG_RUNTIME_DIR=/run/user/1000 systemctl --user start workspace-git-sync-main.service'
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'XDG_RUNTIME_DIR=/run/user/1000 journalctl --user -u workspace-git-sync-main.service -n 20 --no-pager'
```

Do not run host-side Git inside an agent workspace: the agent can write `.git/config` and hooks, so even `git log` on the host executes agent-controlled code outside the sandbox. Inspect history on GitHub or in a fresh clone instead.

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

Pulumi secrets: `telegramBotToken` (from @BotFather) + `telegramUserId`. Use `./scripts/get-telegram-id.sh` to discover user/group IDs. Declares default cron jobs for the main agent ([list](./docs/INTEGRATIONS.md#scheduled-tasks)); they remain disabled until the agent explicitly opts into scheduled automation.

```bash
pulumi config set telegramBotToken --secret && pulumi config set telegramUserId "123456789"
./scripts/provision.sh --tags telegram   # after editing group_vars/openclaw.yml for custom schedules
openclaw channels status && openclaw cron list
```

**Read [docs/INTEGRATIONS.md#telegram-integration](./docs/INTEGRATIONS.md#telegram-integration) in full when:** first-time Telegram setup, adding group chat routing, customizing cron schedules, or using `get-telegram-id.sh`.

## WhatsApp Integration (Optional)

Uses Baileys/WhatsApp Web protocol (not official Business API). Since openclaw 2026.5.12 the channel ships as the external `@openclaw/whatsapp` plugin — the whatsapp role installs it pinned to `openclaw_version` and the config role allowlists it; channel config stays at `channels.whatsapp.*`. **Sessions expire every ~14 days** and nothing alerts on expiry; check `openclaw channels status --probe`. Set `deliver_channel: "whatsapp"` in the agent's `openclaw.yml` entry.

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
| `_openclaw_workspaces` | `openclaw_agents` + provision.sh secrets | workspace, qmd, obsidian-headless, plugins roles |

Scheduled automation is separately opt-in. Agent creation keeps chat online but
does not enable heartbeats or cron jobs. Set
`scheduled_automation_enabled: true` on an agent to apply its latent
`heartbeat_every` cadence and enable its declared cron jobs. Setting it false
removes the agent heartbeat block and disables all of that agent's cron jobs in
place, preserving IDs and run history. A job-level `enabled: false` remains off
even while its agent is enabled.

**Naming conventions** (mechanical, from agent ID):

| Resource | main | other (e.g., `bob`) |
|---|---|---|
| MCP server | `github`, `qmd` | `github-bob`, `qmd-bob` |
| Workspace dir | `~/.openclaw/workspace` | `~/.openclaw/workspace-bob` |
| Deploy key var | `workspace_deploy_key` | `workspace_bob_deploy_key` |
| GitHub token var | `github_token` | `github_token_bob` |

### Adding an Agent

1. Add the agent to `openclaw_agents` in `openclaw.yml`; set `scheduled_automation_enabled: true` only when autonomous work is intended
2. Wire per-agent secrets through `scripts/provision.sh` (Pulumi config or env vars)
3. Run `./scripts/provision.sh`

MCP servers, workspaces, deny rules, and token mappings are generated automatically. Cron jobs remain manual (personal config — add to `openclaw.yml`).

### Role Ordering

`workspace` -> `openclaw` -> `config` -> `agents` -> `telegram` -> `whatsapp` -> `discord` -> `obsidian-headless` -> `qmd` -> `plugins` -> `sandbox` -> `claude-cli`

Telegram must run immediately after agents (prevents message misrouting). Plugins after qmd (qmd binary needed for MCP registration). `claude-cli` needs the plugins proxy and the sandbox image; `config` already points the backend at its launcher, so the first switch-on has a few minutes where turns fail until `claude-cli` has run.

## Sandboxing

OpenClaw's own tools run in Docker containers with bridge networking and a custom sandbox image with a dev toolchain. Agent turns themselves run through Claude Code on the host, and its tools (Bash, Read, Edit, Write) are not sandboxed: see [SECURITY.md §4](./docs/SECURITY.md#4-agent-host-command-abuse). `openclaw_claude_cli_enabled: true` runs each turn in its own container instead (`./scripts/provision.sh --tags config,claude-cli`); `false` switches back (`--tags config`).

| | OpenClaw's sandboxed tools |
|---|---|
| Runtime | Docker container (`openclaw-sandbox-custom:latest`) |
| Network | Bridge (outbound internet via Docker NAT) |
| Workspace | Read-write (mounted at `/workspace`) |
| Host filesystem | No access |
| Gateway config | Isolated (can't read `~/.openclaw/`) |
| Privilege escalation | Blocked (setuid bits stripped) |
| Dev toolchain | See `ansible/roles/sandbox/templates/Dockerfile.sandbox.j2` |

**Network:** Bridge (outbound internet for web research/git push). Git to GitHub goes through `mcp-auth-proxy`, which injects the agent's GitHub token: each workspace's `.git-proxy-config` rewrites GitHub URLs to the proxy's `/github-<agent>/` route (gateway IP of `codex-proxy-net`, port `codex_proxy_port`), and ufw admits the sandbox bridge to that port. Risks: [SECURITY.md §4](./docs/SECURITY.md#4-agent-host-command-abuse).

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

Claude-backed agents use native Bash → pinned SSH → the Mac account, with that account's full permissions. The retired Mac MCP package calls a removed command; `node_exec_enabled` does not restore it or expose OpenClaw's `exec` tool to this harness.

**Read [docs/NODE-EXEC.md](./docs/NODE-EXEC.md) in full when:** setting up the dedicated SSH identity, checking host pins, debugging Mac access or distinguishing SSH from an optional OpenClaw node host. The Mac-to-VPS route is accepted; see [SECURITY §5](./docs/SECURITY.md#5-self-modification-via-node-control).

## Semantic Search (qmd)

Each agent has a **qmd** instance providing local hybrid search (BM25 + vector + LLM reranking) over their workspace. Uses GGUF models (~1.5GB, auto-downloaded) — no API keys needed. Replaces the built-in `memorySearch` with 6 MCP tools per agent (6 × N_agents total).

**Collections per agent:**
- `workspace` — all `.md`, `.txt`, `.csv` files in the workspace
- `memory` — memory directory (`.md` files only)
- `extracted-content` — text extracted from PDFs, images, `.docx`, `.xlsx`

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

**RAM:** `deep_search` loads ~2.1GB GGUF models on-demand. CX43 (16 GB) handles multi-agent well. 2 GB swap configured.

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
