# Optional Integrations

## Telegram Integration

Configured via Pulumi secrets `telegramBotToken` and `telegramUserId`. If not set, deployment proceeds without Telegram. For bot creation, see [README.md](../README.md#telegram-bot-setup).

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

When Telegram is configured, these default cron jobs are declared for the main agent. They stay disabled until the agent sets `scheduled_automation_enabled: true`:

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

## WhatsApp Integration

Agents can use WhatsApp instead of Telegram by setting `deliver_channel: "whatsapp"` in their agent definition. WhatsApp is the external `@openclaw/whatsapp` plugin (Baileys/WhatsApp Web protocol); the whatsapp role installs it pinned to `openclaw_version`.

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

WhatsApp Web sessions expire approximately every 14 days. Nothing alerts on expiry: no health-check cron exists, and the config role turns OpenClaw's own monitor off (`channels.whatsapp.healthMonitor.enabled: false`). Check with `openclaw channels status --probe`.

### Verify WhatsApp

```bash
# Check channel status
ssh ubuntu@openclaw-vps 'XDG_RUNTIME_DIR=/run/user/1000 openclaw channels status --probe'

# Check bindings
ssh ubuntu@openclaw-vps 'XDG_RUNTIME_DIR=/run/user/1000 openclaw agents list --json --bindings'
```

## Discord Integration

Configured via Pulumi secret `discordBotToken` and optional `discordGuildId`/`discordUserId`. If not set, deployment proceeds without Discord. Discord is a built-in channel with native per-channel session isolation — each Discord channel automatically gets its own isolated session context.

### Setup

1. **Create a Discord bot** at [Discord Developer Portal](https://discord.com/developers/applications):
   - New Application → Bot → Reset Token (copy the token)
   - Enable **MESSAGE_CONTENT** and **SERVER_MEMBERS** privileged intents
   - Invite to your server with `bot` + `applications.commands` scopes

2. **Get IDs** (enable Developer Mode in Discord settings → right-click to Copy ID):
   - Guild (server) ID
   - Your user ID

3. **Set Pulumi config:**
   ```bash
   cd pulumi
   pulumi config set discordBotToken --secret
   pulumi config set discordGuildId "YOUR_GUILD_ID"
   pulumi config set discordUserId "YOUR_USER_ID"
   ```

4. **Provision:**
   ```bash
   ./scripts/provision.sh --tags discord
   ```

### Session Isolation

Each Discord channel gets its own isolated session automatically — no additional configuration needed. Messages in `#general` and `#projects` will have separate conversation contexts.

### Verify Discord

```bash
# Check Discord channel config
ssh ubuntu@openclaw-vps 'XDG_RUNTIME_DIR=/run/user/1000 openclaw config get channels.discord'

# Check channel status
ssh ubuntu@openclaw-vps 'XDG_RUNTIME_DIR=/run/user/1000 openclaw channels status'

# Check gateway logs for Discord connection
ssh ubuntu@openclaw-vps 'XDG_RUNTIME_DIR=/run/user/1000 journalctl --user -u openclaw-gateway -n 20 | grep -i discord'
```

### Configuration Notes

- **`requireMention`**: Set to `false` by default (private server). Set to `true` if you want the bot to only respond when @mentioned.
- **Cron delivery**: By default, cron jobs deliver via Telegram (main agent's `deliver_channel`). To deliver via Discord instead, override the main agent's `deliver_channel: "discord"` and `deliver_to` with your Discord user ID in `openclaw.yml`.
- **No QR code needed**: Unlike WhatsApp, Discord uses a persistent bot token with no session expiry.

## Obsidian Headless Sync

Near-real-time two-way sync between agent workspaces and Obsidian Sync, enabling mobile access to agent notes via the Obsidian app. Requires an Obsidian Sync subscription and the `obsidian-headless` npm package. If secrets are not set, deployment proceeds without Obsidian Sync.

### Setup

1. **Install `obsidian-headless` locally** and authenticate:
   ```bash
   npm install -g obsidian-headless
   ob login    # creates ~/.obsidian-headless/auth_token
   ```

   > **On macOS, prefer the runtime project.** `scripts/setup-mac-workspaces.sh`
   > builds one at `~/.local/opt/obsidian-headless` (override with
   > `OB_RUNTIME_DIR`) and the daemons use its `node_modules/.bin/ob`.
   >
   > A *global* install lets the package pick the `better-sqlite3` version, and
   > that native addon only loads on one Node major — which one changed with ob
   > 0.0.13 and will change again. A global install therefore forces the
   > deployment to chase it with a Node pin, which is wrong in one direction or
   > the other after every bump. The project's lockfile owns the pin instead, so
   > there is no Node knob to maintain.
   >
   > If you do install globally with pnpm 10+, the build must be allowed
   > explicitly or the addon lands with **no compiled binary at all** — ob then
   > fails at `require()` with `MODULE_NOT_FOUND` for every ABI, which looks like
   > a version mismatch but is not:
   > ```bash
   > pnpm add -g --allow-build=better-sqlite3 obsidian-headless
   > ```
   > (`pnpm approve-builds` is rejected for global installs.) Note `ob --version`
   > does **not** load the addon, so it is not a valid health check — run a real
   > sync, or `ob sync-status --path <vault>`.

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

## Playwright Browser

Every agent can drive a real browser: open pages, click, type, fill forms and read the page as text. Pages load from a home internet connection, through a proxy on a Mac, so sites that block data-centre addresses (Google, shops, booking sites, Cloudflare checks) treat the agent like a normal visitor.

### How it works

```
agent turn → MCP adapter → Playwright container (one per agent, internal network, no route out)
                                 → relay container (only connects to the proxy)
                                 → tailnet → squid on the Mac (home connection) → website
```

- **Browser:** Microsoft's [Playwright MCP server](https://github.com/microsoft/playwright-mcp), pinned in `openclaw_playwright_image`, one container per agent. Each agent sees only its own browser's tools (`playwright_*` for main, `playwright-<id>_*` for the others), through the same per-agent deny rules as GitHub and qmd.
- **No way out except the proxy:** each browser sits on its own internal Docker network. Docker gives it no route to the internet, to this server's services or to the tailnet. The only other member of that network is the relay, which forwards every connection to the proxy and nowhere else.
- **Proxy on the Mac** (`scripts/deploy-mac-browser-proxy.sh`): squid, listening only on the Mac's tailnet address, serving only the server's tailnet address, ports 80 and 443 only. It refuses private, loopback, link-local and tailnet addresses and the home network's own IPv6 ranges, so a page cannot reach devices at home.
- **Nothing persists:** profiles are in memory (`--isolated`). Logins and cookies are gone after a container restart, and agents cannot use your accounts.
- **Certificate trust:** Playwright's bundled Chrome for Testing gets no CRLSet updates, so it still trusted the compromised emSign chains (eMudhra incident, Oct 2026). The role builds `openclaw-playwright-browser` from the pinned image plus a Chromium policy (`CADistrustedCertificates`, from `ansible/roles/playwright/files/distrust/`). After bumping `openclaw_playwright_image`, the deploy check proves the policy still loads.
- **Why not OpenClaw's own browser plugin:** turns on the Claude Code runtime get no OpenClaw sandbox ([openclaw#84942](https://github.com/openclaw/openclaw/issues/84942)), so the plugin would give all agents one shared browser on the host. Its sandbox browser image has no proxy setting either.

### Setup

1. **Pick the Mac** that stays on at home. Find both tailnet addresses: `tailscale ip -4` on the Mac and on the server.
2. **Configure** in `group_vars/openclaw.yml`:
   ```yaml
   openclaw_playwright_enabled: true
   openclaw_playwright_proxy:
     host: 100.x.y.z     # the Mac
     port: 3128
     client: 100.a.b.c   # the server
   ```
3. **Deploy the proxy** on that Mac, from this repo (needs `brew install squid yq` and sudo for the LaunchDaemon):
   ```bash
   ./scripts/deploy-mac-browser-proxy.sh
   ```
4. **Provision** the per-agent deny rules, the browsers and their registration with the adapter:
   ```bash
   ./scripts/provision.sh --tags agents,playwright,plugins
   ```
   The role then loads pages through every agent's browser. It fails if a browser leaves from the server's own address, the proxy stops refusing private addresses, or Chromium accepts the compromised emSign chain.

### Verify

```bash
# On the Mac: daemon state, and the proxy answers (it refuses the Mac itself)
./scripts/deploy-mac-browser-proxy.sh --status

# On the server: relay and browsers
ssh ubuntu@openclaw-vps 'XDG_RUNTIME_DIR=/run/user/1000 systemctl --user list-units "openclaw-playwright-*"'
```

### Limits

- **No logins.** Sites that need an account stay out of reach.
- **No screenshots.** The MCP adapter returns every result as one text part, so images are switched off (`--image-responses omit`). Agents read pages through snapshots.
- **The Mac must be on.** While the proxy is down, pages fail to load with a proxy error. Nothing falls back to the server's own address.
- **Rebuilt server:** a new server gets a new tailnet address. Provisioning stops with the new address; update `client`, rerun the Mac script, then provision again.
- **Home IPv6 ranges** are read when the proxy starts. If the provider changes them, restart the proxy: `./scripts/deploy-mac-browser-proxy.sh`.
- **Logs:** refusals go to `~/Library/Logs/openclaw/browser-proxy-refused.log` on the Mac. Allowed requests are not logged.
