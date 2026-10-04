# Migration Guide

## 2026-10-01 — Claude Code, Codex and Pi MCP servers removed

The `claude-code`, `codex` and `pi` MCP server types are gone, together with
their Docker images, the Claude Code plugin install, the Codex login in
`provision.sh`, and the Codex and Anthropic routes of `mcp-auth-proxy`. The
proxy itself stays: it injects per-agent GitHub tokens for sandbox git.
`openclaw_compaction_*` was removed too: OpenClaw never applies it to
claude-cli turns, which Claude Code compacts itself
(`claude_code_auto_compact_window`).

### What happens automatically on your next playbook run

- the MCP adapter config is rewritten without the `claude*`, `codex*` and `pi*` servers
- the obsolete provider routes stay removed; the GitHub proxy remains, and C preparation adds a fixed-target OpenClaw MCP relay (not a general-purpose destination proxy)

### What stays on your host (manual cleanup)

Run on the VPS as `ubuntu`:

```bash
# Containers and images
for role in claude-code-mcp codex-mcp pi-mcp; do docker ps -aq --filter "label=openclaw-role=$role" | xargs -r docker rm -f; done
docker image rm claude-code-mcp:latest codex-mcp:latest pi-mcp:latest 2>/dev/null || true

# Build directories, configs and credentials only these servers used
rm -rf ~/.openclaw/claude-code-mcp-build ~/.openclaw/claude-code-plugins \
       ~/.openclaw/codex-mcp-build ~/.openclaw/pi-mcp-build ~/.openclaw/codex-config.toml
rm -f ~/.openclaw/anthropic-auth-token
rm -rf ~/.codex            # Codex login plus the CLI's own state (logs, memories, skills)
rm -rf ~/.claude/plugins   # old plugin marketplaces; Claude Code may recreate this directory
npm uninstall -g @openai/codex

# OpenClaw compaction keys (no effect on claude-cli turns)
openclaw config unset agents.defaults.compaction

# UFW rule for MCP containers on codex-proxy-net (keep the 172.17 "sandbox git push" rule)
sudo ufw status numbered | grep 8787    # find the codex-proxy-net subnet rule
sudo ufw delete <N>
```

Claude Code automatically recreates `~/.claude/plugins/synced/`, sometimes
with an empty sync marker. This is harmless bookkeeping, not a restored
plugin installation. Do not repeat cleanup just because the directory
reappears.

Also delete the `CODEX_AUTH_JSON` secret from the repository's GitHub Actions
settings if you set it for staging.


## 2026-04-19 — Beads removal

Beads ([steveyegge/beads](https://github.com/steveyegge/beads)) was retired as an
openclaw-infra dependency in [PR #13](https://github.com/pandysp/openclaw-infra/pull/13).

**If you forked this repo before 2026-04-19 and ran the playbook**, your host
currently has Beads installed and running. This guide walks you through the
cleanup the playbook no longer does for you.

### What happens automatically on your next playbook run

After pulling PR #13, `ansible-playbook` will:

- stop injecting `BEADS_DOLT_*` env vars into new sandbox configs
- strip those four env vars from your existing openclaw config via a one-shot
  `check_unset` loop in the `config` role (removed on 2026-10-01; forks that
  had not re-run the playbook by then must `openclaw config unset` the four
  `agents.defaults.sandbox.docker.env.BEADS_DOLT_*` keys by hand)
- stop baking `bd` + `dolt` into new sandbox container images
- stop running the SQLite→Dolt workspace migration task on new agents
- stop `export_beads` in qmd-watch
- stop re-asserting the UFW rule for the shared Dolt port

### What stays on your host (manual cleanup)

The playbook deliberately does **not** touch these, so you don't lose issue
data by accident:

- `/usr/local/bin/bd` and `/usr/local/bin/dolt` — binaries
- `~/.config/systemd/user/dolt-beads-shared.service` — systemd unit (may be
  running or inactive)
- `~/.beads/shared-server/` — the shared Dolt database and all your issue data
- `.beads/` directories inside every workspace
- `.beads` entry in your `obsidian_headless_excluded_folders` override, if you
  copied it from `openclaw.yml.example`
- UFW rule allowing port 3308 from the Docker bridge (removed from Ansible but
  UFW keeps previously-applied rules until you delete them)

### Optional: archive your issues before cleanup

If you want to keep a record of your tracked work:

```bash
# Per workspace (run on the VPS)
cd /home/ubuntu/.openclaw/workspace        # or workspace-<agent-id>
bd export > /home/ubuntu/beads-archive-$(date +%F)-$(basename "$PWD").jsonl
```

### Manual teardown (copy-paste)

Run on the VPS as the `ubuntu` user. Safe to run once; idempotent on re-runs:

```bash
# 1. Stop and disable the shared Dolt systemd unit
systemctl --user stop dolt-beads-shared 2>/dev/null || true
systemctl --user disable dolt-beads-shared 2>/dev/null || true
rm -f ~/.config/systemd/user/dolt-beads-shared.service
systemctl --user daemon-reload

# 2. Remove the central Beads/Dolt data directory
rm -rf ~/.beads

# 3. Remove per-workspace .beads/ directories
rm -rf ~/.openclaw/workspace*/.beads

# 4. Remove bd + dolt binaries (sudo required)
sudo rm -f /usr/local/bin/bd /usr/local/bin/dolt
rm -f ~/.local/bin/bd

# 5. Drop the UFW rule for the shared Dolt port
# (port 3308 from Docker bridge — adapt the subnet if yours differs)
sudo ufw status numbered | grep 3308   # find the rule number
sudo ufw delete <N>                    # replace <N> with the number above

# 6. Scrub workspace AGENTS.md references (if your agents are still active)
# Each agent's workspace has an AGENTS.md with three beads mentions. See
# pandysp/openclaw-workspace-main for the post-scrub template at main HEAD.
```

### If your agents used Beads in practice

If the peer agents in your fleet were actively filing `bd create` issues (as
opposed to the template saying they should), you'll want a replacement task
tracker before merging this change. The updated `AGENTS.md.reference` template
points agents at `notes/todos/` + the **Open** sections of daily notes, which
is file-based and works without any external dependency. Adjust to your
project tracker of choice (Linear, GitHub Issues, etc.) before your agents'
next heartbeat.
