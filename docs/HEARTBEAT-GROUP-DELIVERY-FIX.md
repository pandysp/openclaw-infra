# Heartbeat Group Delivery Fix

## Problem

OpenClaw has a bug where heartbeat delivery to Telegram **groups** silently falls back to the default user's DM. This affects agents whose `heartbeat.to` is a group chat ID (negative number).

**Affected agents:** `tl`, `ph` (any agent with `deliver_type: group`)

**Upstream issues:**
- [#18573](https://github.com/openclaw/openclaw/issues/18573) — Heartbeat target resolves wrong for non-DM targets (open, no PR)
- [#22298](https://github.com/openclaw/openclaw/issues/22298) — Cron announce delivery fails with "pairing required" (PR #22838, not merged)
- [#22430](https://github.com/openclaw/openclaw/issues/22430) — Broader cron announce delivery failures (open)

## Root Cause

In `resolveHeartbeatSenderId()` (file: `dist/reply-*.js` and `dist/subagent-registry-*.js`):

1. The heartbeat delivery target correctly resolves to the group ID (e.g., `-5046803888`)
2. `resolveHeartbeatSenderId` tries to match this against Telegram's `allowFrom` list
3. `allowFrom` only contains individual user IDs — group IDs never match
4. Falls back to `allowList[0]` → the primary user's DM ID
5. The heartbeat ctx sets `From: sender, To: sender` — both become the wrong user ID
6. Session is created with `deliveryContext.to` pointing to the user's DM instead of the group

## Fix (Monkeypatch)

One-line change in `resolveHeartbeatSenderId()`:

```javascript
// BEFORE (broken):
if (allowList.length > 0) return allowList[0];

// AFTER (fixed):
if (allowList.length > 0) return candidates[0] ?? allowList[0];
```

This makes `candidates[0]` (which is `deliveryTo` — the configured group ID) preferred over `allowList[0]` (the default user ID). The `allowList[0]` fallback only activates when there are no candidates at all.

### Files to patch

The function exists in two bundled files (search by function name since hashes change per version):

1. `dist/reply-*.js` — search for `function resolveHeartbeatSenderId`
2. `dist/subagent-registry-*.js` — same function, different chunk

Both must be patched.

### After patching

1. Stop gateway
2. Delete stale `agent:<id>:main` entries from `sessions.json` for affected agents + their JSONL files
3. Start gateway
4. Next heartbeat/cron creates fresh sessions with correct group targets

## Ansible Automation

### Patch on upgrade (`openclaw` role)

The `ansible/roles/openclaw/tasks/main.yml` should re-apply this patch after installing/upgrading the binary. See the `heartbeat-group-fix` tag.

### Session cleanup on provision (`telegram` role)

The `ansible/roles/telegram/tasks/session-cleanup.yml` runs after bindings are set and checks each group agent's main session. If `deliveryContext.to` doesn't match the expected group ID, it patches `sessions.json`.

## Verification

```bash
# Check session delivery targets (via SSH)
ssh ubuntu@openclaw-vps 'python3 -c "
import json
for agent in [\"tl\", \"ph\"]:
    path = f\"/home/ubuntu/.openclaw/agents/{agent}/sessions/sessions.json\"
    with open(path) as f:
        d = json.load(f)
    entry = d.get(f\"agent:{agent}:main\", {})
    to = entry.get(\"deliveryContext\", {}).get(\"to\", \"NONE\")
    print(f\"{agent}: deliveryContext.to={to}\")
"'

# Expected: group IDs (negative numbers), NOT user IDs (positive numbers)
# tl: deliveryContext.to=-5046803888
# ph: deliveryContext.to=-5203656694
```

## Status

- **Patch applied:** 2026-02-22 on OpenClaw 2026.2.19-2
- **Verified:** Session targets correct for both `tl` and `ph`
- **Separate issue:** Cron announce delivery (#22298) still fails — that's a different bug (pairing/scope-upgrade). The session target fix ensures heartbeats deliver to the right place; cron announce delivery needs the upstream PR #22838.

## When to remove

Remove this patch when upstream #18573 is fixed and deployed. Check the [issue](https://github.com/openclaw/openclaw/issues/18573) periodically.
