# Official Docs Review

Tracking which [OpenClaw docs](https://docs.openclaw.ai) pages have been reviewed against our setup.

## Reviewed

- [x] [Home](https://docs.openclaw.ai/)
- [x] [Getting Started](https://docs.openclaw.ai/start/getting-started)
- [x] [Hetzner Platform Guide](https://docs.openclaw.ai/platforms/hetzner)
- [x] [Tailscale Networking](https://docs.openclaw.ai/gateway/tailscale) — adopted built-in `gateway.tailscale.mode: serve` and switched to `openclaw config set`
- [x] [Gateway Configuration](https://docs.openclaw.ai/gateway/configuration) — our config aligns; default model set to opus 4.5; sandbox now enabled (see Sandboxing entry)
- [x] [Telegram Channel](https://docs.openclaw.ai/channels/telegram) — our config is correct but minimal; see potential improvements below
- [x] [Cron Jobs](https://docs.openclaw.ai/automation/cron-jobs) — our setup is correct; thinking default set to high; cron idempotency bug fixed
- [x] [Device Pairing](https://docs.openclaw.ai/start/pairing) — our docs cover more than the official page; no changes needed
- [x] [Security](https://docs.openclaw.ai/gateway/security) — added `--fix` to checklist, prompt injection guidance to Threat 4, credential storage paths, plugin threat, browser control planning
- [x] [Sandboxing](https://docs.openclaw.ai/gateway/sandboxing) — enabled `all` mode with `workspaceAccess: rw` and bridge networking; custom image (`openclaw-sandbox-custom:latest`) includes Claude Code

## To Review

- [ ] [Wizard](https://docs.openclaw.ai/start/wizard)
- [x] [Setup](https://docs.openclaw.ai/start/setup) — mostly targets macOS/local installs; `openclaw health` already in verify.sh; workspace-as-git-repo is a nice idea but not urgent
- [ ] [OpenClaw](https://docs.openclaw.ai/start/openclaw)
- [ ] [Onboarding](https://docs.openclaw.ai/start/onboarding)
- [ ] [Configuration Examples](https://docs.openclaw.ai/gateway/configuration-examples)
- [ ] [Docker Install](https://docs.openclaw.ai/install/docker)
- [ ] [Nix Install](https://docs.openclaw.ai/install/nix)
- [ ] [Updating](https://docs.openclaw.ai/install/updating)

## Potential Improvements

- **Telegram `streamMode`** — set `channels.telegram.streamMode` to `"partial"` for draft message streaming. Shows incremental output instead of waiting for the full response. (Source: [Gateway Configuration](https://docs.openclaw.ai/gateway/configuration), [Telegram Channel](https://docs.openclaw.ai/channels/telegram))
- **Telegram `configWrites: false`** — by default, the bot can modify its own config via `/config set` commands in Telegram. Disable with `channels.telegram.configWrites: false` for a security-conscious deployment. (Source: [Telegram Channel](https://docs.openclaw.ai/channels/telegram))
- **Telegram `chunkMode: "newline"`** — splits long messages on paragraph boundaries instead of hard character limits. Small UX improvement. (Source: [Telegram Channel](https://docs.openclaw.ai/channels/telegram))
- **Telegram `tokenFile`** — the docs support `channels.telegram.tokenFile` to read the bot token from a file path instead of storing it directly in config. Keeps the token out of `openclaw.json`. (Source: [Telegram Channel](https://docs.openclaw.ai/channels/telegram))
- **Telegram privacy mode** — by default, Telegram bots only see @mentions and `/commands` in groups (privacy mode enabled). To let the bot see all group messages, disable privacy via BotFather `/setprivacy` or add the bot as a group admin. Only relevant if the bot is used in group chats. (Source: [Telegram Channel](https://docs.openclaw.ai/channels/telegram))
- ~~**Workspace as private git repo**~~ — **Implemented.** Hourly auto-sync via systemd timer with Pulumi-generated ED25519 deploy key. See CLAUDE.md "Workspace Git Sync" section.

## Verified

- **Cron job idempotency** — tested: `openclaw cron add` with a duplicate `--name` creates a second job with a different ID. Fixed by adding `remove_cron_by_name` helper that removes existing jobs by name before re-adding. `cron remove` only accepts IDs (not names), so the helper parses `cron list --json` with `jq`.
