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
