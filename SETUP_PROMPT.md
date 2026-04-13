# Setup Prompt

Copy and paste everything below the line into your coding agent (Claude Code, Codex, Cursor, etc.).

---

I want to deploy OpenClaw on a Hetzner VPS using this repo. I'm not a developer — please handle everything for me. Do not ask me to run commands myself; execute them directly. Only ask me when you need me to do something in a browser (like creating an account or copying an API key).

Here's how to proceed:

1. **Check my system.** Verify I have everything needed: Node.js, Pulumi CLI, Ansible, Tailscale, git. If anything is missing, install it for me. If I'm on Windows, verify WSL is set up correctly.

2. **Read the docs.** Read `CLAUDE.md` thoroughly — it's your reference for the full setup. Also read `docs/INTEGRATIONS.md` for optional features.

3. **Walk me through account creation.** I need accounts on:
   - **Hetzner Cloud** (for the server) — guide me to create an account and generate an API token
   - **Tailscale** (for private networking) — guide me to create an account, install it on my machine, enable MagicDNS and HTTPS, and generate a reusable auth key
   - **Claude / Anthropic** — I need a setup token (run `claude setup-token`)
   - **Pulumi** (for infrastructure management) — guide me to create a free account

   Open the relevant URLs for me when possible. Wait for me to provide each token/key before continuing.

4. **Configure and deploy.** Run `npm install`, set up the Pulumi stack, configure all required secrets, and run `pulumi up`. Wait for the deployment to complete.

5. **Verify.** Run `./scripts/verify.sh` and confirm everything is healthy. Help me pair my first device so I can access the web chat.

6. **Offer optional features.** After the base setup is working, ask me about each optional feature one at a time. Explain what each one does in plain language, and if I want it, set it up for me:
   - **Telegram** — chat with my assistant from my phone + scheduled daily briefings
   - **WhatsApp** — chat via WhatsApp
   - **Discord** — chat via Discord with per-channel sessions
   - **Web search** — let the assistant search the web (needs an xAI API key)
   - **Workspace backup** — hourly backup of the workspace to a private GitHub repo
   - **Multiple agents** — run several assistants in parallel
   - **Mac remote control** — let the assistant run commands on my Mac
   - **Obsidian sync** — two-way sync with Obsidian for mobile access
   - **Semantic search** — search across the assistant's workspace and documents

   For each feature I enable, configure it fully, verify it works, and move on to the next.

7. **Clean up.** After setup, shred the cloud-init log as documented in CLAUDE.md. Run a final health check. Give me a summary of what's running, how to access it, and what the monthly cost will be.

Take your time. Verify each step before moving to the next. If something fails, diagnose and fix it — don't skip ahead.
