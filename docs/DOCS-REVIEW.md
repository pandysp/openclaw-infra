# Official Docs Review

Tracking which [OpenClaw docs](https://docs.openclaw.ai) pages have been reviewed against our setup.

- Reviewed OpenClaw version: `2026.6.6`
- Reviewed on: `2026-08-14`
- Latest docs compared on: `2026-08-14`

The versioned docs and the published executable for `openclaw_version` define
the provisioning contract. Mutable latest docs are a drift signal only. A
version bump must update the reviewed version above and the scheduler contract
fixtures before provisioning can pass.

## Reviewed

- [x] [Home](https://docs.openclaw.ai/)
- [x] [Getting Started](https://docs.openclaw.ai/start/getting-started)
- [x] [Hetzner Platform Guide](https://docs.openclaw.ai/platforms/hetzner)
- [x] [Tailscale Networking](https://docs.openclaw.ai/gateway/tailscale) — adopted built-in `gateway.tailscale.mode: serve` and switched to `openclaw config set`
- [x] [Gateway Configuration](https://docs.openclaw.ai/gateway/configuration) — our config aligns; default model set to opus 4.5; sandbox now enabled (see Sandboxing entry)
- [x] [Telegram Channel](https://docs.openclaw.ai/channels/telegram) — our config is correct but minimal; see potential improvements below
- [x] [Heartbeat at v2026.6.6](https://github.com/openclaw/openclaw/blob/v2026.6.6/docs/gateway/heartbeat.md) — `0m` disables; `agents.list[]` heartbeat blocks form an allowlist when any are present
- [x] [Scheduled Tasks at v2026.6.6](https://github.com/openclaw/openclaw/blob/v2026.6.6/docs/automation/cron-jobs.md) — reconciliation uses the CLI rather than editing scheduler storage
- [x] [Device Pairing](https://docs.openclaw.ai/start/pairing) — our docs cover more than the official page; no changes needed
- [x] [Security](https://docs.openclaw.ai/gateway/security) — added `--fix` to checklist, prompt injection guidance to Threat 4, credential storage paths, plugin threat, browser control planning
- [x] [Sandboxing](https://docs.openclaw.ai/gateway/sandboxing) — enabled `all` mode with `workspaceAccess: rw` and bridge networking; custom image (`openclaw-sandbox-custom:latest`) with dev toolchain

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

- **Heartbeat default** — upstream enables a 30-minute heartbeat when cadence is omitted. IaC sets the runtime default to `0m` and requires an explicit per-agent automation switch.
- **Heartbeat allowlist** — v2026.6.6 uses `agents.list[]`; once any list entry has a heartbeat block, only entries with a block run. `main` is reconciled through the same list-level path as every other agent.
- **Disabled cron visibility** — the published v2026.6.6 CLI requires `cron list --all`; plain `cron list` omits disabled jobs.
- **Cron identity** — duplicate names are permitted upstream. Desired names must be unique, and multiple live matches fail closed rather than guessing which history to keep.
- **Cron updates** — `cron edit --enable/--disable` and field edits preserve IDs and run history. Missing jobs are created with `--disabled` when the agent policy is off.

## Known Latest-Docs Drift

- Current heartbeat docs use `agents.entries.*`; v2026.6.6 uses `agents.list[]`.
- Current CLI docs describe `openclaw automations`; v2026.6.6 exposes `openclaw cron`.
- The v2026.6.6 scheduled-task docs describe migration into shared SQLite state,
  while deployed hosts may retain `~/.openclaw/cron/jobs.json`. Treat the file as
  a possible legacy artifact and determine active state through the CLI; never
  edit scheduler storage directly.
- CI runs `./scripts/check-openclaw-contract.sh --latest-docs` against both the
  pinned and current upstream docs. It fails when either semantic contract moves
  beyond the reviewed `agents.list[]`/`cron` versus `agents.entries`/`automations`
  split. Provisioning also checks the installed pin and required CLI flags before
  mutation.
