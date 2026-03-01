# Remote Node Control (Mac)

> **Disabled by default.** Node exec lets agents run arbitrary shell commands on your local machine with your user's full permissions — no sandbox. Enable with `node_exec_enabled: true` in `ansible/group_vars/all.yml` only after reading the security warnings in [docs/SECURITY.md](./SECURITY.md) section 5.

Agents can run shell commands on your Mac via the node host feature. This enables tmux-based workflows where a VPS agent controls a Claude Code session on your local machine.

Agents access node exec via the `mac_run` MCP tool (provided by `node-exec-mcp`), not the built-in exec tool. Each agent gets its own scoped tool: `mac_run` (main), `mac-manon_run`, etc.

```
┌──────────────────────┐     ┌────────────────────────┐     ┌──────────────────────┐
│  VPS Agent           │     │  MCP Adapter           │     │  Mac (Node Host)     │
│  (sandbox)           │────▶│  node-exec-mcp (stdio) │────▶│  openclaw node run   │
│                      │     │                        │     │  (LaunchAgent)       │
│  calls mac_run tool  │     │  OPENCLAW_TOKEN auth   │     │  tmux, claude        │
│  (cwd defaults /tmp) │     │  Tailscale Serve       │     │  /opt/homebrew/bin   │
└──────────────────────┘     └────────────────────────┘     └──────────────────────┘
```

## Setup

**1. Enable in config** (edit `ansible/group_vars/all.yml`):
```yaml
node_exec_enabled: true
```

**2. One-time Mac setup:**
```bash
./scripts/setup-mac-node.sh

# Then approve pairing on the VPS:
ssh ubuntu@openclaw-vps 'openclaw devices list'
ssh ubuntu@openclaw-vps 'openclaw devices approve <request-id>'

# Re-provision to install node-exec-mcp and auto-discover the node ID:
./scripts/provision.sh --tags config,plugins
```

**What `setup-mac-node.sh` does:**
1. Resolves gateway hostname from Tailscale
2. Installs a persistent LaunchAgent (`ai.openclaw.node.plist`)
3. Patches LaunchAgent to use stable Homebrew symlink (survives `brew upgrade`)
4. Sets node-side exec approvals to auto-approve all commands (`defaults.security: full`)

## Config

Gateway-side (set by Ansible):
```
tools.exec.host: sandbox        # Built-in exec stays sandboxed (agents use mac_run MCP tool instead)
tools.exec.security: full       # Tighten to "allowlist" after testing
tools.exec.ask: off             # Tighten to "on-miss" after testing
tools.exec.node: <auto>         # Auto-discovered during provisioning; used by node-exec-mcp
```

Node-side (set by `setup-mac-node.sh`):
- `~/.openclaw/exec-approvals.json` — `defaults.security: full` (auto-approve all commands)

**How auth works:** The `node-exec-mcp` server receives `OPENCLAW_TOKEN` (the gateway token) as an env var, which `openclaw nodes run` uses to authenticate with the gateway. Without this token, the connection fails with "pairing required".

**Two approval layers:** Both the gateway (`tools.exec.security/ask`) AND the node (`exec-approvals.json`) must allow a command. Configure both.

**CWD defaults to `/tmp`:** The gateway sends the agent's VPS workspace path as CWD, which doesn't exist on Mac. The `node-exec-mcp` server uses `DEFAULT_CWD=/tmp` as a workaround. Pass `workdir=/Users/<you>` explicitly when needed.

## Operations

```bash
# Check node status
ssh ubuntu@openclaw-vps 'openclaw nodes status'

# Test from VPS
ssh ubuntu@openclaw-vps 'openclaw nodes run --cwd /tmp echo hello'

# Manage Mac node host
openclaw node status          # Check LaunchAgent
openclaw node restart         # Restart after updates
openclaw node stop            # Stop the service

# Reset node ID pin (e.g., after re-pairing)
ssh ubuntu@openclaw-vps 'openclaw config unset tools.exec.node'
./scripts/provision.sh --tags config   # Re-discovers and pins

# View node host logs
tail -f ~/.openclaw/logs/node.log
```

**Note:** The node host disconnects on gateway restarts but auto-reconnects (LaunchAgent handles restarts). If the node ID changes (re-pairing), re-run `./scripts/provision.sh --tags config` to update the pin.
