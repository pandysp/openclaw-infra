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

## WhatsApp Integration

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

## Gmail / Calendar / Drive (gogcli)

Provides Gmail, Calendar, Drive, and other Google Workspace access via [`gogcli`](https://github.com/steipete/gogcli) running as a containerized MCP server on `codex-proxy-net`. The agent gets a single `gog_run` tool (`gog-<id>_run` for non-default agents) that wraps the `gog` CLI; the agent's CLI knowledge comes from its [`gog` skill](https://github.com/openclaw/openclaw/blob/main/skills/gog/SKILL.md), not from the MCP tool. If the three Pulumi secrets below are not set, deployment skips the role and the MCP servers are not registered.

### Setup

1. **Create the OAuth client** in [Google Cloud Console](https://console.cloud.google.com):
   - Create a project and enable the APIs you want (Gmail API, Google Calendar API, etc.)
   - **APIs & Services → Credentials → Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
   - Download the `client_secret.json`

   See the [gogcli README](https://github.com/steipete/gogcli) for the canonical walkthrough, including which scopes each service needs.

2. **Set Pulumi secrets:**
   ```bash
   cd pulumi
   pulumi config set gogAccount "you@gmail.com"
   pulumi config set gogKeyringPassword --secret              # choose a strong password — you'll need it again in step 4
   pulumi config set gogClientSecret --secret "$(cat /path/to/client_secret.json)"
   ```

3. **Provision:**
   ```bash
   ./scripts/provision.sh --tags gog,plugins
   ```
   The `gog` role installs the `gogcli` binary, imports the OAuth client into a file-backed keyring at `~/.config/gogcli`, and builds the hardened `gog-mcp:latest` image. The `plugins` role then registers one MCP server per agent.

4. **Authenticate on the VPS** (one-time, manual — the VPS has no browser):
   ```bash
   ssh ubuntu@openclaw-vps.<tailnet>.ts.net
   export GOG_KEYRING_BACKEND=file
   export GOG_KEYRING_PASSWORD='<the password from step 2>'
   gog auth add you@gmail.com --services gmail,calendar --manual
   ```
   `gog` prints a Google OAuth URL. Open it in a browser on your laptop, sign in, copy the verification code, paste it back into the terminal. The refresh token is stored in the keyring file. Pass whichever `--services` subset you actually need — services not authorized here will fail at the Google API even if `gog_enable_commands` allows the command.

   If you forgot the keyring password, read it back with `pulumi config get gogKeyringPassword --show-secrets`.

### Verify

```bash
# Per-agent MCP servers are registered
openclaw config get plugins.entries.openclaw-mcp-adapter.config \
  | jq '.servers[] | select(.name | startswith("gog"))'

# gog can talk to Google end-to-end (run on the VPS, outside the container)
ssh ubuntu@openclaw-vps.<tailnet>.ts.net \
  "GOG_KEYRING_BACKEND=file GOG_KEYRING_PASSWORD='<the password>' gog gmail labels list --json"
```

Then ask the agent something like "what's in my inbox today?" and confirm it returns Gmail data, not an error.

### Re-authentication

OAuth refresh tokens generally don't expire, but Google revokes them after password changes, manual revocation, or longer inactivity. To re-issue, repeat step 4 — `gog auth add` overwrites the existing keyring entry. To rotate the keyring password itself, update `gogKeyringPassword` in Pulumi, re-provision, then re-run `gog auth add` (the old keyring file becomes unreadable with the new password and must be replaced).

### Operational Notes

- **Multi-agent**: a second agent (e.g., `bob`) automatically gets its own `gog-bob` MCP server via the `openclaw_mcp_server_types × openclaw_agents` cross-product in `playbook.yml`. All agents share the same host-side keyring and OAuth identity.
- **Allowlist scope**: `gog_enable_commands` in `ansible/group_vars/all.yml` controls which top-level `gog` commands the container will execute. The default list blocks `auth`, `config`, and other meta-commands so an agent cannot manipulate its own credentials through the MCP tool. Edit and re-provision with `--tags gog,plugins` to change the allowlist.
- **Rebuild image**: `./scripts/provision.sh --tags gog -e force_gog_mcp_rebuild=true` rebuilds `gog-mcp:latest`, runs the smoke tests, and removes any stale containers labelled `openclaw-role=gog-mcp`.
- **Security model**: container runs with `--cap-drop ALL`, `--read-only`, `no-new-privileges`, `--pids-limit 50`, 512 MB memory cap. The `~/.config/gogcli` directory is bind-mounted **read-only**. The container lives on `codex-proxy-net`, which sandbox containers on the default bridge cannot reach.
