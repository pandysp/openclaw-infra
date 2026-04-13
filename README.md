# OpenClaw Infrastructure

Deploy [OpenClaw](https://openclaw.ai) on your own server.

## Why this setup?

You already know what OpenClaw can do. This repo gives you a secure, always-on place to run it.

- **Security first.** Zero public ports. Tailscale zero-trust networking. Firewall blocks all inbound traffic. Only your devices can reach the server.
- **Always on.** Runs on a VPS that doesn't sleep, travel, or lose WiFi. Your assistant keeps working whether your laptop is open or not.
- **Your data stays yours.** Conversations, memory, and workspace live on a server you control. Not on someone else's platform.
- **Reproducible.** Everything is infrastructure as code. Destroy it and recreate it in 30 minutes. Nothing is hand-configured.
- **Cheap.** ~€11/month for a dedicated server with 8 CPUs and 16 GB RAM.

## Before you start

**What it costs:**

| Service | Cost |
|---------|------|
| Claude Pro subscription (or higher) | ~$20/month |
| Hetzner VPS (8 vCPU, 16 GB RAM) | ~€11/month |
| Tailscale | Free |
| Optional API keys (web search, etc.) | Most have free tiers |

**What you need:** A computer, an internet connection, a credit card, and about 30 minutes.

## Where to run OpenClaw

This repo deploys to a VPS, but that's not the only option. Pick what fits your situation:

| | Your Mac/PC | Mac Mini / home server | VPS (this repo) |
|---|---|---|---|
| Always on? | No — sleeps when you close the lid | Yes, if you keep it running | Yes |
| Local hardware access? | Yes — camera, browser, local files | Yes | No (can add a Mac node later) |
| Monthly cost | Free | One-time ~€700+ | ~€11/month |
| Security | Only as secure as your network | Same | Zero public ports, Tailscale-only |
| Setup complexity | Easiest | Medium | Handled by a coding agent |

## What you get

**Included:**
- Hetzner VPS provisioned and configured automatically
- Tailscale zero-trust networking (no public ports)
- OpenClaw gateway running as a system service
- Sandboxed sessions in Docker with a full dev toolchain
- Web chat interface accessible from any device on your Tailscale network

**Optional features** (the coding agent will ask if you want these):
- **Telegram** — chat with your assistant from your phone, scheduled daily briefings
- **WhatsApp** — same, via WhatsApp
- **Discord** — same, via Discord (with per-channel session isolation)
- **Web search** — research capability via Grok (xAI)
- **Semantic search** — search across the assistant's own workspace and documents
- **Workspace backup** — hourly git sync of the workspace to a private GitHub repo
- **Multiple agents** — run several agents in parallel, each with their own workspace
- **Mac remote control** — let the assistant run commands on your local Mac
- **Obsidian sync** — two-way sync between agent workspace and Obsidian for mobile access
- **GitHub OAuth** — seamless git authentication inside workspaces
- **Custom domain** — wildcard TLS via Cloudflare for nicer URLs

## Getting started

You don't need to understand this repo or type infrastructure commands yourself. A coding agent will read the repo and guide you through everything — creating accounts, installing tools, deploying the server, and configuring features.

### 1. Set up your coding agent

If you don't have a coding agent yet, pick one and install it:

**Option A — Claude Code** (recommended)
1. Subscribe to [Claude Pro](https://claude.ai/pro) ($20/month) or higher
2. Install Claude Code:
   - **Mac/Linux:** Open Terminal, run: `npm install -g @anthropic-ai/claude-code`
   - **Windows:** First install [WSL](https://learn.microsoft.com/en-us/windows/wsl/install) by opening PowerShell as Administrator and running `wsl --install`, then restart your computer. Open the Ubuntu terminal that appears and run: `npm install -g @anthropic-ai/claude-code`

**Option B — Codex**
1. Subscribe to [ChatGPT Pro](https://openai.com/chatgpt/pricing/)
2. Install: `npm install -g @openai/codex`

**Option C — Cursor**
1. Download [Cursor](https://cursor.com) (free tier available)
2. Install and open it

> **Note:** If you don't have Node.js installed (needed for Options A and B), download it from [nodejs.org](https://nodejs.org/) first. Pick the LTS version.

### 2. Clone this repo

Open your terminal (or Ubuntu terminal on Windows) and run:

```bash
git clone https://github.com/pandysp/openclaw-infra.git
cd openclaw-infra
```

### 3. Start the coding agent

Launch your coding agent inside the repo directory:

```bash
# Claude Code
claude

# Codex
codex

# Cursor — open the folder in the Cursor app
```

### 4. Give it the setup prompt

Copy and paste the contents of [`SETUP_PROMPT.md`](./SETUP_PROMPT.md) into the coding agent. It contains the exact instructions for the agent to guide you through the full setup.

The agent will:
- Check your system and install any missing prerequisites
- Walk you through creating the necessary accounts (Hetzner, Tailscale)
- Help you generate API keys and tokens
- Deploy the server
- Verify everything is working
- Ask which optional features you'd like to enable
- Configure those features for you

### If something goes wrong

Paste the error message into the coding agent and ask it to fix it. These agents are good at diagnosing and recovering from infrastructure issues. If the agent gets stuck, the [troubleshooting guide](./docs/TROUBLESHOOTING.md) covers common problems.

## Documentation

These docs are primarily for coding agents, but humans are welcome too:

| Document | What it covers |
|----------|---------------|
| [CLAUDE.md](./CLAUDE.md) | Complete setup reference, all config options, operations guide |
| [SETUP_PROMPT.md](./SETUP_PROMPT.md) | The starter prompt for your coding agent |
| [docs/INTEGRATIONS.md](./docs/INTEGRATIONS.md) | Telegram, WhatsApp, Discord, Obsidian setup details |
| [docs/SECURITY.md](./docs/SECURITY.md) | Threat model and security architecture |
| [docs/TROUBLESHOOTING.md](./docs/TROUBLESHOOTING.md) | Common issues and fixes |

## Contributing

Issues and pull requests are welcome. This is a reference implementation — fork it and make it yours.

## License

[MIT](./LICENSE)
