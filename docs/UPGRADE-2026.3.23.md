# OpenClaw 2026.3.13 → 2026.3.23 Upgrade Spec

> Research completed 2026-03-23. All items from the 200+ commit changelog investigated.

## Implementation Strategy

**Commit 1 — Upgrade (this spec):**
- Fix CLAWDBOT env vars
- 3 version bumps
- Remove redundant WS handshake timeout drop-in
- Full provision
- Run verification checklist

**Commit 2 — Day-2 features (separate, after upgrade verified stable):**
- Telegram `silentErrorReplies`
- Post-compaction JSONL truncation
- Health monitor threshold tuning

**Deferred (separate session, needs external dependencies or careful ordering):**
- Per-agent reasoning — needs agent index verification on live server
- Search providers (Exa/Tavily/Firecrawl) — need API key signups, Pulumi secrets, `plugins.allow` entries
- Config batch provisioning refactor
- Claude Code `--bare` flag in fork

## Code Changes Required

### 1. Fix Legacy Env Vars (MUST before upgrade)

**File:** `ansible/roles/plugins/tasks/main.yml:1367`

The node-exec MCP server env block uses removed `CLAWDBOT_*` env vars:

```
# Current (broken on 2026.3.23):
OPENCLAW_TOKEN: $gw_token           ← dead, never read by anything
CLAWDBOT_GATEWAY_URL: $gw_url       ← removed in 2026.3.23
CLAWDBOT_GATEWAY_TOKEN: $gw_token   ← removed in 2026.3.23

# Fixed:
OPENCLAW_GATEWAY_URL: $gw_url
OPENCLAW_GATEWAY_TOKEN: $gw_token
```

Evidence: In 2026.3.13, `auth-profiles-DRjqKE3G.js` has `readGatewayEnv(env, ["OPENCLAW_GATEWAY_TOKEN", "CLAWDBOT_GATEWAY_TOKEN"])` with fallback. On `main` (2026.3.23), `src/gateway/call.ts` has only `OPENCLAW_*`. The `node-exec-mcp` package itself doesn't read these — it passes `process.env` through to spawned `openclaw` CLI processes.

Note: This code is inside a `{% if node_exec_enabled %}` Jinja guard. When `node_exec_enabled: false` (default), this block is never rendered. The fix is still correct to make regardless — it prevents breakage if node exec is enabled later.

### 2. Version Bumps

**File:** `ansible/group_vars/all.yml`

| Variable | From | To | Line |
|----------|------|----|------|
| `openclaw_version` | `"2026.3.13"` | `"2026.3.23"` | 41 |
| `codex_version` | `"0.115.0"` | `"0.116.0"` | 187 |
| `claude_code_version` | `"2.1.77"` | `"2.1.81"` | 193 |

### 3. Remove Redundant WS Handshake Timeout Drop-In

**File:** `ansible/roles/openclaw/tasks/daemon.yml`

We added a systemd drop-in (`openclaw-gateway.service.d/handshake-timeout.conf`) setting `OPENCLAW_HANDSHAKE_TIMEOUT_MS=10000` as a workaround for the 3s default. In 2026.3.23, the default is now 10s (PR #49262), making our drop-in redundant. Remove the drop-in creation task and the drop-in file cleanup.

Note: The WSS URL routing workaround in `playbook.yml` pre_tasks should be **kept** — the root cause (issue #48167) is still open, and the WSS path avoids the race condition entirely.

### 4. Provision Command

Run **full provision** (not `--tags openclaw` alone) to ensure sandbox container cleanup:

```bash
./scripts/provision.sh
```

If wanting to minimize scope:
```bash
./scripts/provision.sh --tags openclaw,config,plugins,sandbox
```

The `config` role removes stale sandbox containers on config change. The `plugins` role rebuilds MCP Docker images on version change. The `sandbox` role only needed if forcing image rebuild.

## Verification Checklist

```bash
# 1. Gateway health
openclaw health
openclaw doctor

# 2. Security baseline
openclaw security audit --deep
# Expected: 0 critical · 0 warn

# 3. MCP servers loaded
ssh ubuntu@openclaw-vps 'XDG_RUNTIME_DIR=/run/user/1000 openclaw config get plugins.entries.openclaw-mcp-adapter.config' | jq '.servers | length'
# Expected: N_agents × 6 server types

# 4. Test each MCP server type (trigger one tool call each)
# - GitHub: any github_* tool
# - Codex: codex_codex (new session)
# - Claude Code: claude_code_claude_code (new session)
# - Pi: pi_pi (new session)
# - qmd: qmd_search (search query)
# - Node exec (if enabled): mac_run

# 5. Test channels
# - Telegram: send a DM, check response
# - WhatsApp: send a message, check response
# - Discord: send in an allowed channel
# - Web chat: open https://openclaw-vps.<tailnet>.ts.net/

# 6. Verify containers
ssh ubuntu@openclaw-vps 'docker ps --format "{{.Names}} {{.Image}} {{.Status}}"'
# Expected: new sandbox containers, MCP containers with updated images

# 7. Verify node-exec env vars (if node_exec_enabled)
ssh ubuntu@openclaw-vps 'XDG_RUNTIME_DIR=/run/user/1000 openclaw config get plugins.entries.openclaw-mcp-adapter.config' | jq '.servers[] | select(.name | startswith("mac")) | .env'
# Expected: OPENCLAW_GATEWAY_URL and OPENCLAW_GATEWAY_TOKEN (no CLAWDBOT_*)

# 8. Verify memorySearch still disabled
ssh ubuntu@openclaw-vps 'jq ".agents.defaults.memorySearch.enabled" /home/ubuntu/.openclaw/openclaw.json'
# Expected: false

# 9. Test WS handshake via localhost (optional — root cause unfixed)
ssh ubuntu@openclaw-vps 'XDG_RUNTIME_DIR=/run/user/1000 openclaw health'
# If works: WSS workaround in playbook.yml could be removed later (separate PR)

# 10. Clean up Telegram phantom session orphans (optional)
# Old phantom sessions from the DM topic bug will become orphans (no longer written to).
# Safe to clean up stale entries:
ssh ubuntu@openclaw-vps 'ls -la /home/ubuntu/.openclaw/agents/*/sessions/'
# Review and optionally remove stale session files
```

## Upstream Bug Fixes (free with binary upgrade, no code changes)

These bugs existed in 2026.3.13 and are fixed in 2026.3.23. No config or code changes needed — just verify after upgrade.

| Bug | What Was Happening | Verify After Upgrade |
|-----|-------------------|---------------------|
| **WhatsApp silent message loss** (PR #42588) | After any gateway restart or connection hiccup, inbound messages delivered as `append` type on reconnection were silently dropped. Health check (`channels status --probe`) showed "healthy" because it only checks connectivity, not message routing. | Restart gateway, send a WhatsApp message during restart window, confirm agent responds |
| **Telegram DM topic phantom sessions** (PR #48773) | `/status` showed 0 tokens when real session had data. Different code paths (inbound vs commands) produced different session keys, creating isolated phantom sessions with context fragmentation. | Run `/status` in a DM topic, confirm token count matches reality |
| **Gateway SIGTERM zombies** (PR #51242) | `systemctl restart openclaw-gateway` could leave zombie processes. Shutdown timeout was 5s, now 25s (within `TimeoutStopSec=30`). | After restart: `pgrep -f openclaw` should show only one gateway process |
| **Plugin TDZ registration** | device-pair, phone-control, talk-voice plugins could fail to load on some startup sequences due to temporal dead zone error. | `openclaw doctor` should show all plugins healthy |
| **Grok provider type-check regression** (PR #49472) | Missing credential metadata (`credentialPath`, `inactiveSecretPaths`) in bundled Grok provider registration. | `openclaw health` should show web search as enabled |
| **Session manager memory leak** (PR #52427) | One-shot sessions (cron, quick DMs) accumulated expired cache entries in memory over gateway lifetime. | Not directly verifiable — long-running gateway RAM usage should be more stable |
| **Claude Code `--resume` dropping parallel tool results** (v2.1.80) | When resuming a Claude Code session via `claude_code_reply` (with `--resume threadId`), parallel tool results from the previous turn could be dropped, causing the agent to lose context. | Multi-tool sessions via `claude_code_reply` should be more reliable |
| **Compaction overflow recovery** | When post-compaction context still exceeded safe threshold (our 200K limit), no recovery was triggered. Now triggers overflow recovery automatically. | Long sessions should compact more reliably within 200K |
| **Memory flush dedup** | Potential duplicate memory entries during `compaction.memoryFlush` fallback retries. Transcript-hash dedup now stays active across retries. | Check `memory/` dir for duplicate date entries after compaction |

## What's Safe (verified through source analysis)

| Item | Finding | Evidence |
|------|---------|----------|
| MCP adapter SDK compat | Not affected — uses `export default function(api)` callback, zero `openclaw/*` imports | npm tarball inspection |
| SSRF blocking vs credential proxy | All 3 SSRF changes scoped to media/CDP/proxy-DNS. MCP adapter uses `StreamableHTTPClientTransport` with direct `fetch()` — no SSRF guard | PR analysis + adapter source |
| Codex MCP tool schema | Identical between 0.115.0 and 0.116.0: `codex` + `codex-reply`, same params, same output `{threadId, content}` | npm tarball diff |
| Codex default model | Independent bundled `models.json` — stays on `gpt-5.3-codex`. OpenClaw's gpt-5.4 default only affects gateway conversations | Source analysis of model selection |
| Claude Code JSON schema | `-p --output-format json` result schema identical. Fork reads only `result`, `session_id`, `is_error` — all stable | npm tarball + fork source |
| Claude Code subprocess | Fork uses `stdio: ['ignore', 'pipe', 'pipe']` — already immune to v2.1.79 hang fix | Fork source inspection |
| OAuth header | `anthropic-beta: oauth-2025-04-20` still required, unchanged. All major projects still use it | Multi-source verification |
| Discord Carbon reconcile | Code ownership change only (local reconcile → `@buape/carbon` library). No behavioral change for sessions/routing/allowlists | PR #46597 review |
| xAI Grok IDs | Internal metadata sync only. `tools.web.search.provider: "grok"` and `tools.web.search.grok.apiKey` unchanged. Search model stays `grok-4-1-fast` | PR analysis |
| Tool-call ID dedup | Operates in model provider layer (gateway↔LLM), not MCP adapter path. No impact on Codex/Claude Code | PR #40996 review |
| Auth scope spoofing | Our device-paired Tailscale sessions unaffected. Fix targets device-less proxy sessions. PR #46800 adds loopback spoofing defense (positive) | Security advisory analysis |
| Config validation | `--json` now uses strict `JSON.parse` — our values already strict. No config paths renamed/removed/type-changed | Source diff of config parser |
| memorySearch re-enable | `agents.defaults.memorySearch.enabled: false` controls both tools. Persists in `openclaw.json`, survives binary upgrades | PR #52639 + Ansible code review |
| plugins list --json | Stdout pollution fixed (PR #52449). JSON structure unchanged. Our Ansible doesn't even use this command | Source analysis |
| Daemon service template | `buildSystemdUnit()` identical between versions. `OPENCLAW_HANDSHAKE_TIMEOUT_MS` drop-in unaffected | Source diff |
| Container stickiness (MCP) | Codex/Claude Code/Pi use `--rm` flag — auto-remove on gateway restart. Low risk | Ansible role review |
| Embedded-runner cache | 4-line memory leak fix — automatic, no config | PR #52427 |
| Transcript maintenance | New `maintain()` hook for context engine plugins. Only fires with third-party plugins (not our default engine) | PR #51191 |
| `allowPrivateUrls` in qmd config | This field is a **no-op** — the MCP adapter doesn't consume it (`config.ts` only extracts `name`, `transport`, `command`, `args`, `env`, `url`, `headers`). Harmless but provides no protection. No SSRF guard exists in the adapter's HTTP transport path | Adapter source inspection |

## Container Stickiness Gap (sandbox only)

Sandbox containers (`openclaw-sbx-*`) do NOT use `--rm` and persist across gateway restarts. The `openclaw` Ansible role (binary upgrade) restarts the gateway but does **not** remove stale sandbox containers. Container cleanup only happens in `config`, `sandbox`, and `plugins` roles.

**Mitigation:** Always run full provision or at minimum `--tags openclaw,config` after binary upgrade.

## Minor Changes — No Action Required

Additional items from the 200+ commit sweep. All assessed as no-impact for our setup, documented for completeness.

| Change | Why No Action |
|--------|--------------|
| Telegram `allow_sending_without_reply` on reply sends | Prevents dropped replies when parent message deleted. Automatic, no config. |
| Telegram hard-timeout stuck `getUpdates` requests | Fixes polling hangs. Automatic, no config. |
| Telegram preserved spaces in HTML rechunking | Fixes message formatting. Automatic, no config. |
| `jq` removed from default exec safe bins allowlist | Only affects exec allowlist mode. Our exec security is `full` (unrestricted). |
| Network interface discovery hardening for WSL2/restricted hosts | Degrades gracefully when interfaces are missing. Positive for Tailscale-only setups. |
| Memory plugin system-prompt registration | `memory-core` now registers its own system-prompt section. No config change. |
| Gateway startup lazy-loading (bundled plugins, channel add paths) | Faster restarts. Automatic. |
| WhatsApp: wait for pending creds before reopening after pairing restart | Fixes reconnection race. Automatic. |
| WhatsApp: pinned listener registry to `globalThis` singleton | Fixes event handler duplication. Automatic. |
| Telegram: fail loud on unknown `accountId` in `message send` | Better error messages. No config. |
| Control UI: expand-to-canvas button, roundness slider, usage overview styling | Cosmetic additions. No config. |
| Control UI: scope persisted session selection per gateway | Prevents stale session bleed across gateways. Automatic. |
| Default agent timeout raised from 600s to 48h | We explicitly set `openclaw_agent_timeout_seconds: 1500`. Our override takes precedence. |
| Default memory slot no longer required as explicit plugin config | Eliminates spurious validation warnings. Automatic. |
| ClawHub install precedence for `plugins install` | Scoped npm packages (`@pandysp/...`) still resolve to npm. Our `--pin` flag anchors the source. |

## Bugs Fixed by This Upgrade

| Bug | Impact | PR |
|-----|--------|-----|
| **WhatsApp silent message loss** | After reconnection, all `append`-type messages silently dropped. Health check showed "healthy" | #42588 |
| **Telegram DM topic phantom sessions** | `/status` showed 0 tokens; context fragmentation in named-account DM topics | #48773 |
| **Gateway SIGTERM zombies** | `systemctl restart` could leave zombie processes | #51242 |
| **Plugin TDZ registration** | device-pair/phone-control/talk-voice could fail to load | changelog |
| **Session manager memory leak** | One-shot sessions accumulated expired cache entries | #52427 |
| **Grok provider type-check regression** | Missing credential metadata in bundled provider registration | #49472 |

## Day-2 Opportunities (after stable upgrade)

| Feature | Config Path | Notes |
|---------|------------|-------|
| Post-compaction JSONL truncation | `agents.defaults.compaction.truncateAfterCompaction: true` | Reduces session files 80-95%. Safe with memoryFlush. No archive option. |
| Per-agent reasoning | `agents.list.<i>.thinkingDefault` | Values: off/minimal/low/medium/high/xhigh/adaptive. Index-based — verify agent order first. |
| Telegram silent errors | `channels.telegram.silentErrorReplies: true` | Suppresses error notification sounds |
| Health monitor tuning | See full config paths below | Tune stale-event thresholds, per-channel overrides |
| Search providers | See full config paths below | Exa/Tavily/Firecrawl. Need `plugins.allow` entries + Pulumi secrets. |
| Config batch | `openclaw config set --batch-json` | Array of `{path, value}` objects. Could speed up provisioning. |
| Claude Code `--bare` flag | Update `@pandysp/claude-code-mcp` fork to pass `--bare` | Skips hooks, LSP, plugin sync, skill walks. Reduces container startup overhead. Requires `ANTHROPIC_API_KEY` (already set). |
| Codex model upgrade to gpt-5.4 | Add `model = "gpt-5.4"` to `codex-config.toml.j2` | Currently defaults to `gpt-5.3-codex`. Cost delta: input +43% ($1.75→$2.50/MTok), output +7% ($14→$15/MTok). |
| ClawHub | `openclaw skills search\|install\|update` | Works out of the box. |
| `/btw` side questions | — | Available automatically, no config. |

### Health Monitor Config Paths (2026.3.23+)

| Config Path | Type | Default | Purpose |
|-------------|------|---------|---------|
| `gateway.channelHealthCheckMinutes` | number | 5 | How often the monitor probes channels |
| `gateway.channelStaleEventThresholdMinutes` | number | 30 | Minutes without events before channel is restarted |
| `gateway.channelMaxRestartsPerHour` | number | 10 | Rolling-window cap on monitor-initiated restarts |
| `channels.<channel>.healthMonitor.enabled` | boolean | inherits global | Per-channel override (e.g., `channels.whatsapp.healthMonitor.enabled`) |
| `channels.<channel>.accounts.<id>.healthMonitor.enabled` | boolean | inherits channel | Per-account override (highest priority) |

Resolution: account-level > channel-level > global default (true). Fails closed on error (skips monitoring rather than crashing).

Note: The health monitor detects event-silence (stale sockets), not message delivery failures. A WhatsApp session that silently drops messages (like the pre-upgrade recency filter bug) would only be caught if it also stops producing all events.

### Per-Agent Reasoning Config (2026.3.23+, PR #51974)

Three new per-agent paths under `agents.list.<index>.*`:

| Path | Values | Purpose |
|------|--------|---------|
| `thinkingDefault` | `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `adaptive` | Thinking level |
| `reasoningDefault` | `on`, `off`, `stream` | Reasoning visibility |
| `fastModeDefault` | `true`, `false` | Fast mode |

**Resolution order:** inline directive (`/think high`) > session override > per-agent default (new) > global default (`agents.defaults.thinkingDefault`) > fallback (`adaptive` for Claude 4.6, `low` for other reasoning models, `off` otherwise)

**Auto-revert of disallowed model overrides:** When a stored model override becomes invalid for the active agent (e.g., agent switched, allowlist changed), the override auto-clears and falls back to default. A system notice is emitted: "Model override not allowed for this agent; reverted to {default model}."

**CLI:** `openclaw config set agents.list.0.thinkingDefault "high"` (index-based — verify agent-to-index mapping with `openclaw agents list --json` first)

**Caution:** Index order depends on agent creation order, which can differ between fresh and incremental provisions. Always verify before writing index-based config.

### Search Provider Setup (when adopting Exa/Tavily/Firecrawl)

**plugins.allow must include the plugin IDs.** Current allowlist blocks all unlisted plugins, including bundled ones.

Add to `plugins.allow` array: `"exa"`, `"tavily"`, `"firecrawl"` (only the ones you're enabling).

**Config paths per provider:**

| Provider | Enable | API Key | Plugin ID for `plugins.allow` |
|----------|--------|---------|-------------------------------|
| Exa | `plugins.entries.exa.enabled: true` | `plugins.entries.exa.config.webSearch.apiKey` | `"exa"` |
| Tavily | `plugins.entries.tavily.enabled: true` | `plugins.entries.tavily.config.webSearch.apiKey` | `"tavily"` |
| Firecrawl | `plugins.entries.firecrawl.enabled: true` | `plugins.entries.firecrawl.config.webSearch.apiKey` | `"firecrawl"` |

These add **dedicated tools** alongside the primary Grok search — they don't replace it. Primary `tools.web.search.provider` stays `"grok"`.

### Codex config.toml New Optional Field (0.116.0)

`websocket_connect_timeout_ms: Option<u64>` — added to `ModelProviderInfo` struct. Our template doesn't set it (fine, it's optional with a sane default). Only relevant if using WebSocket-based model providers.

## Post-Upgrade Housekeeping

### Memory Files to Update

After the upgrade is verified stable, update these memory entries:

- **`project_ws_handshake_bug.md`** — the 3s default is now 10s upstream. Our `OPENCLAW_HANDSHAKE_TIMEOUT_MS=10000` drop-in is removed (item 3 in code changes). WSS workaround still in place (root cause unfixed). Update the memory to reflect the new baseline.
- **MEMORY.md version references** — any mention of `openclaw_version: "2026.3.13"` or component versions should note the upgrade.

## Pricing Reference (search providers, if adopted)

| Provider | Cost | Best For |
|----------|------|----------|
| Exa | ~$7/1k searches (with contents) | Semantic search, content extraction |
| Tavily | Free 1k/mo, then ~$0.008/credit | Agent workflows, structured extraction |
| Firecrawl | ~$0.001/page (Standard) | Web scraping, JS-rendered pages |
| Grok (current) | Via xAI API key | Agentic search (search+read+synthesize) |
