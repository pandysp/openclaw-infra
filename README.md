# OpenClaw Infrastructure

**Your AI assistant shouldn't die when you close your laptop.**

OpenClaw is an AI agent that runs 24/7 on your own server. It sends you a morning briefing on Telegram. It does research while you sleep. It remembers everything across conversations. It costs less than a Netflix subscription.

This repo is everything you need to set that up — from zero to a running AI assistant in about 30 minutes.

## Why self-host?

Most AI assistants live inside a browser tab. Close it, and they're gone. They don't remember yesterday. They can't reach out to you. They can't work while you're offline.

A self-hosted OpenClaw is different:

- **Always on.** It runs on a cheap cloud server. Close your laptop, go hiking — it keeps working.
- **It messages you.** Morning briefings, research results, task updates — delivered to Telegram, WhatsApp, or Discord. You don't have to check on it.
- **It remembers.** Persistent memory and workspace across every conversation. Context that builds over weeks, not minutes.
- **It's private.** Your server, your data, your network. No one else can access it — not even the cloud provider can reach the ports.
- **It's cheap.** ~$12/month for a server with 8 CPUs and 16 GB RAM. No per-message fees beyond your existing Claude subscription.

## What can it actually do?

**Talk to you where you already are**
Connect Telegram, WhatsApp, or Discord. Ask it questions, give it tasks, get updates — from your phone, your desktop, wherever.

**Work on a schedule**
Set up recurring tasks: a daily standup summary, a nightly research shift, a weekly planning session. It runs them automatically and delivers the results.

**Run code safely**
Every session runs in a sandboxed Docker container with a full dev toolchain (Python, Node.js, git, and more). It can write and execute code without risking your server.

**Search the web and your files**
Web search via Grok, semantic search across its own workspace. It can research topics, read documents, and synthesize findings.

**Control your Mac remotely** (optional)
Run shell commands on your local machine from the server — useful for automation workflows. Disabled by default, requires explicit opt-in.

**Run multiple agents**
Need a research agent and a coding agent? Run them in parallel, each with their own workspace and memory.

## What you'll need

No programming experience required, but you should be comfortable with:
- Typing commands in a terminal
- Creating accounts on a few services
- Editing a configuration file

**Accounts to create** (all have free tiers except the server):

| Service | What it's for | Cost |
|---------|--------------|------|
| [Hetzner Cloud](https://www.hetzner.com/cloud) | The server that runs your assistant | ~$12/month |
| [Tailscale](https://tailscale.com/start) | Private networking — makes the server accessible only to your devices | Free |
| [Anthropic / Claude](https://claude.ai) | The AI that powers OpenClaw | Your existing subscription |
| [Pulumi](https://www.pulumi.com) | Manages the server setup (so you can recreate it anytime) | Free |

**Optional** (add anytime):

| Service | What it adds |
|---------|-------------|
| Telegram | Chat with your assistant from your phone |
| WhatsApp | Same, via WhatsApp |
| Discord | Same, via Discord |
| [xAI / Grok](https://x.ai/api) | Web search capability |

**Software to install on your computer:**
- [Node.js](https://nodejs.org/) 18 or newer
- [Pulumi CLI](https://www.pulumi.com/docs/install/)
- [Ansible](https://docs.ansible.com/ansible/latest/installation_guide/) (`pip install ansible`)
- [Tailscale](https://tailscale.com/download)

## Getting started

### 1. Set up Tailscale (5 min)

Tailscale creates a private network between your devices. Your server will only be reachable through this network — no public ports, no exposure to the internet.

1. Create an account at [tailscale.com/start](https://tailscale.com/start) (sign up with GitHub, Google, or email)
2. Install on your computer:
   ```bash
   # Mac
   brew install --cask tailscale
   # Then open Tailscale from Applications and log in

   # Linux — see https://tailscale.com/download/linux
   ```
3. Enable MagicDNS and HTTPS in the [admin console](https://login.tailscale.com/admin/dns)
4. Generate a server auth key at [admin console > Keys](https://login.tailscale.com/admin/settings/keys):
   - Enable **Reusable** and **Ephemeral**
   - Copy the key (starts with `tskey-auth-...`)

### 2. Get your tokens (5 min)

- **Hetzner API token**: Create at [console.hetzner.cloud](https://console.hetzner.cloud/) → your project → API Tokens
- **Claude setup token**: Run `claude setup-token` in your terminal

### 3. Deploy (10 min)

```bash
git clone https://github.com/pandysp/openclaw-infra.git && cd openclaw-infra
npm install
cd pulumi
pulumi login
pulumi stack init prod

# Set your secrets
pulumi config set hcloud:token --secret       # Hetzner API token
pulumi config set tailscaleAuthKey --secret    # Tailscale auth key from step 1
pulumi config set claudeSetupToken --secret    # Claude setup token from step 2

# Deploy
pulumi up
```

Wait about 5 minutes for the server to finish setting up, then verify:

```bash
cd ..
./scripts/verify.sh
```

### 4. Connect (2 min)

Open the web chat:
```
https://openclaw-vps.<your-tailnet>.ts.net/chat
```

On first visit, you'll need to pair your device:
```bash
ssh ubuntu@openclaw-vps.<your-tailnet>.ts.net 'openclaw devices list'
ssh ubuntu@openclaw-vps.<your-tailnet>.ts.net 'openclaw devices approve <request-id>'
```

Refresh the browser — you're in.

> **Shortcut:** Skip pairing with `cd pulumi && pulumi stack output tailscaleUrlWithToken --show-secrets`

### 5. Add Telegram (optional, 5 min)

1. Open Telegram, search for **@BotFather**, send `/newbot`, follow the prompts
2. Get your user ID: run `./scripts/get-telegram-id.sh` or message **@userinfobot** on Telegram
3. Configure:
   ```bash
   cd pulumi
   pulumi config set telegramBotToken --secret    # From @BotFather
   pulumi config set telegramUserId "YOUR_ID"     # Your numeric user ID
   cd ..
   ./scripts/provision.sh --tags telegram
   ```

Your assistant will now send you a daily standup at 09:30 and run a night shift at 23:00 (customizable).

## How it works

```
Your phone/laptop ──(Tailscale VPN)──> Hetzner cloud server ──> OpenClaw gateway
                                        No public ports exposed
                                        Firewall blocks all inbound traffic
                                        Only your Tailscale devices can connect
```

The server runs OpenClaw as a background service. Tailscale provides encrypted networking. A firewall ensures nothing is accessible from the public internet. Your conversations, memory, and workspace live on the server and are optionally backed up to a private GitHub repo.

| Component | What it does |
|-----------|-------------|
| **Hetzner VPS** | Runs everything (~$12/month, 8 CPU, 16 GB RAM) |
| **Tailscale** | Private encrypted network between your devices and the server |
| **OpenClaw** | The AI gateway — manages sessions, memory, tools, and messaging |
| **Docker** | Sandboxes every AI session for safety |
| **Pulumi** | Infrastructure as code — reproducible setup you can recreate anytime |
| **Ansible** | Configures the server (installs software, sets up services) |

## Day-to-day operations

```bash
# Check health
openclaw health
openclaw doctor

# Update OpenClaw on the server
./scripts/provision.sh --tags openclaw

# Change settings (model, tools, sandbox config)
# Edit ansible/group_vars/all.yml, then:
./scripts/provision.sh --tags config

# Update scheduled tasks
# Edit ansible/group_vars/openclaw.yml, then:
./scripts/provision.sh --tags telegram

# Tear everything down
cd pulumi && pulumi destroy
```

## Going further

Once you're running, you can:

- **Add WhatsApp or Discord** — see [docs/INTEGRATIONS.md](./docs/INTEGRATIONS.md)
- **Enable web search** — add an xAI API key (`pulumi config set xaiApiKey --secret`)
- **Back up the workspace to GitHub** — run `./scripts/setup-workspace.sh`
- **Run multiple agents** — define them in `ansible/group_vars/openclaw.yml`
- **Control your Mac remotely** — see [docs/NODE-EXEC.md](./docs/NODE-EXEC.md)
- **Add semantic search** over the workspace — see the qmd section in [CLAUDE.md](./CLAUDE.md)
- **Sync with Obsidian** — see [docs/INTEGRATIONS.md](./docs/INTEGRATIONS.md#obsidian-headless-sync)

## Documentation

| Document | When to read it |
|----------|----------------|
| [CLAUDE.md](./CLAUDE.md) | Detailed setup, all config options, operations reference |
| [docs/INTEGRATIONS.md](./docs/INTEGRATIONS.md) | Telegram, WhatsApp, Discord, Obsidian setup |
| [docs/SECURITY.md](./docs/SECURITY.md) | Threat model and security architecture |
| [docs/TROUBLESHOOTING.md](./docs/TROUBLESHOOTING.md) | Something not working? Start here |

## Contributing

This is a reference implementation — fork it and make it yours. If you find bugs or have improvements, PRs and issues are welcome.

## License

[MIT](./LICENSE)
