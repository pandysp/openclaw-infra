# Security Model

Threat model and mitigations for the OpenClaw Hetzner/Tailscale deployment.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        INTERNET                                 │
│                                                                 │
│  Hetzner Firewall: BLOCK ALL INBOUND (infrastructure level)    │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │                                                         │   │
│  │  UFW: deny incoming, allow outgoing, allow tailscale0   │   │
│  │  ┌─────────────────────────────────────────────────┐   │   │
│  │  │                   Hetzner VPS                    │   │   │
│  │  │                                                  │   │   │
│  │  │  ┌──────────────────────────────────────────┐   │   │   │
│  │  │  │           OpenClaw Gateway                │   │   │   │
│  │  │  │           (localhost:18789)               │   │   │   │
│  │  │  │                                           │   │   │   │
│  │  │  │  - systemd user service (unprivileged)    │   │   │   │
│  │  │  │  - Tailscale identity + device pairing    │   │   │   │
│  │  │  │  - token auth as fallback                 │   │   │   │
│  │  │  └──────────────────────────────────────────┘   │   │   │
│  │  │                        ▲                        │   │   │
│  │  │           Tailscale Serve (HTTPS proxy)         │   │   │
│  │  │                        │                        │   │   │
│  │  └────────────────────────│────────────────────────┘   │   │
│  │                           │ tailscale0 interface       │   │
│  └───────────────────────────│────────────────────────────┘   │
│                              │                                 │
│            Tailscale (NAT traversal, encrypted)               │
│                              │                                 │
└──────────────────────────────│─────────────────────────────────┘
                               │
                               ▼
                       Your Machine
                      (Tailscale client)
```

## Defense in Depth

| Layer | What it does | Where |
|-------|--------------|-------|
| **Hetzner Firewall** | Blocks all inbound at infrastructure level | Before VM |
| **UFW** | Blocks all except tailscale0 interface | On VM |
| **Tailscale** | Encrypts + authenticates network access | Overlay network |
| **Gateway Auth** | Tailscale identity + device pairing (token fallback) | Application |
| **Process Isolation** | Unprivileged user, systemd --user service; all sessions sandboxed in Docker | OS level |

## Threat Model

### 1. Exposed Gateway

**Attack**: Attacker discovers and accesses the gateway from the internet.

**Mitigations**:
- Hetzner cloud firewall blocks ALL inbound traffic
- Gateway binds to `127.0.0.1` only (not `0.0.0.0`)
- Tailscale Serve proxies access through encrypted tunnel
- No DNS records point to the server's public IP
- Tailscale identity auth + device pairing required; token auth as fallback

**Residual Risk**: Low. Multiple layers must fail simultaneously.

### 2. Compromised Server

**Attack**: Attacker gains shell access to the VPS.

**Mitigations**:
- No SSH password authentication (key-only via Tailscale SSH)
- No public SSH port
- Runs as unprivileged `ubuntu` user (not root) via systemd --user
- Automatic security updates via unattended-upgrades
- Docker installed for sandbox support but gateway runs natively (reduced attack surface vs running gateway in Docker)

**Blast radius**: An attacker with shell access can read session transcripts at `~/.openclaw/agents/<agentId>/sessions/*.jsonl` — plaintext JSONL containing full conversation history. File permissions (`600`) prevent other OS users from reading them, but not the `ubuntu` user or root.

**Residual Risk**: Medium. Privilege escalation possible but requires additional exploits.

### 3. Secret Exposure

**Attack**: Setup token, gateway token, or other secrets are leaked.

**Mitigations**:
- Secrets stored in Pulumi encrypted state (never in git)
- Written to temp files during setup (`600` permissions), then deleted
- `set +x` disables command logging during secret operations
- Cloud-init log should be shredded after deployment (contains secrets)

**Residual Risk**: Low if cloud-init log is cleaned up. Medium if forgotten.

See [CLAUDE.md — Key Rotation](../CLAUDE.md#key-rotation) for rotation procedures.

### 4. Agent Host Command Abuse

**Attack**: The agent executes destructive or malicious commands on the host.

**Scenario**: OpenClaw has `tools.elevated.enabled: true` (default), giving it shell access. If the agent is manipulated via prompt injection, it could attempt to run destructive commands, exfiltrate data, or modify config.

**Mitigations in place**:
- **`agents.defaults.sandbox.mode: "all"`** — all sessions (including web chat) run in Docker containers, preventing direct host access
- `agents.defaults.sandbox.docker.image: "openclaw-sandbox-custom:latest"` — custom image with dev toolchain and setuid bits stripped
- `agents.defaults.sandbox.docker.network: "bridge"` — allows web research and git push while isolating from host filesystem, gateway config, and sudo
- **`agents.defaults.sandbox.docker.readOnlyRoot: false`** — rootfs is writable to allow runtime package installation (`pip install`, `npm install`, `curl` binaries). Container runs as UID 1000 with `--cap-drop ALL`, so Unix file permissions still prevent writing to system directories (`/usr/bin/`, `/etc/`, `/usr/lib/`). Only `/home/node/` is writable. Writable layer is disk-backed (overlay) and destroyed on container recreation. See [Writable Rootfs Rationale](#writable-rootfs-rationale) below.
- Tailscale-only access limits who can send prompts
- Dedicated VPS with no other services
- `agents.defaults.thinkingDefault: high` — extended thinking improves prompt injection resistance
- **`tools.elevated.allowFrom.telegram`** — when Telegram is configured, elevated actions require approval from the configured Telegram user (without Telegram, elevated tools are enabled without an approval gate)
- **`tools.sandbox.tools.allow`** — sandbox sessions have explicit access to all standard tool groups

**Mitigations available but not enabled**:
- `tools.elevated.enabled: false` — disables shell access entirely
- `tools.elevated.allowFrom.<channel>` — restricts elevated tools to additional channels beyond Telegram
- Hetzner firewall outbound rules — could restrict to known-good destinations

**Accepted risk**: All sandboxed sessions have workspace write access and bridge networking. A prompt-injected session could exfiltrate workspace data via HTTP or git push, or poison workspace content for future sessions. Host isolation prevents access to gateway config, credentials, and sudo. See [Autonomous Agent Safety](./AUTONOMOUS-SAFETY.md) for a multi-agent architecture that would further reduce risk by splitting the night shift into isolated agents.

**Prompt injection guidance** (from [official docs](https://docs.openclaw.ai/gateway/security)):
- Lock down inbound DMs (we use allowlist — done)
- Treat links, attachments, and pasted instructions as potentially hostile
- Keep secrets out of the agent filesystem where possible
- Prefer the latest, strongest model for tool-enabled agents
- Red flags: requests to "read this URL and do exactly what it says", ignore system prompts, or reveal hidden instructions

**Residual Risk**: Medium for all sessions (sandboxed in Docker — no host access, no sudo, no gateway config — but network-enabled workspace exfiltration possible).

### 5. Self-Modification via Node Control

**Attack**: OpenClaw accesses your infrastructure management machine through a connected node.

**Scenario**: If your Mac is added as an OpenClaw node and has Pulumi state + passphrase, the agent (or an attacker via prompt injection) could read/modify Pulumi state, destroy infrastructure, or access locally stored secrets. Commands run with your user's full permissions — there is no sandbox.

**Default state**: Node exec is **disabled by default** (`node_exec_enabled: false` in `ansible/group_vars/all.yml`). When disabled, no `tools.exec.*` gateway config is set, no node ID is pinned, no `node-exec-mcp` binary is installed, and no MCP servers are wired — the feature is completely inert.

**Mitigations** (when enabled):
- Don't add your infrastructure management machine as an OpenClaw node
- Store Pulumi passphrase in a password manager, not in shell history/envrc
- Consider Pulumi Cloud backend (requires browser auth, not just passphrase)
- Use `tools.exec.host: sandbox` (default) so agents must explicitly switch per-session
- Consider `tools.exec.security: allowlist` to restrict which commands can run

**Residual Risk**: None when disabled. Medium if Mac is an OpenClaw node. Low if infrastructure management is isolated.

### 6. Lateral Movement

**Attack**: Attacker uses compromised server to attack other systems.

**Mitigations**:
- Server is isolated (single-purpose VPS)
- No access to internal networks
- Tailscale device has minimal ACL permissions

**Note**: The Hetzner firewall allows all outbound traffic (required for API calls, updates, Tailscale). A compromised server could make outbound connections. Restrict outbound rules in the Hetzner firewall if this is a concern.

**Residual Risk**: Low. Limited inbound attack surface, but outbound is unrestricted.

### 7. Tailscale Account Compromise

**Attack**: Attacker compromises your Tailscale login and adds a device to your tailnet.

**Mitigations**:
- Enable 2FA on your Tailscale account
- Device pairing required before accessing the gateway (attacker can reach the server but can't use OpenClaw without approval)
- Regular audit of connected devices in the Tailscale admin console

**Residual Risk**: Medium. Tailscale is the single trust boundary for network access.

### 8. Supply Chain Attack

**Attack**: Malicious npm package or dependency.

**Mitigations**:
- Official OpenClaw package from npm registry
- Node.js installed via official OpenClaw installer (NodeSource)
- Automatic security patches via unattended-upgrades

**Residual Risk**: Medium. Trust in upstream is required.

### 9. Malicious Plugin

**Attack**: A compromised OpenClaw plugin gains full access to the gateway.

Plugins run in-process — they have the same access as the gateway itself (config, credentials, sessions, network, sudo). npm lifecycle scripts execute during installation before code review.

**Current status**: No plugins installed.

**When adding plugins**:
- Review source code before installing
- Use `plugins.allow` to explicitly allowlist approved plugins
- Pin exact versions (`@scope/pkg@1.2.3`)
- Prefer plugins from known/trusted authors

**Residual Risk**: Low (no plugins). Medium when plugins are added.

### 10. Infrastructure Token Compromise

**Attack**: Attacker obtains the Hetzner API token.

**Mitigations**:
- **Dedicated Hetzner Project** for OpenClaw (hard isolation from other infra)
- Token only exists in Pulumi encrypted state (never on VPS)
- Separate API token not shared with other projects

**Residual Risk**: Low if isolated. High if shared — a compromised agent with a shared Hetzner token could delete servers, create expensive instances, or pivot to other infrastructure in the same project.

### 11. Workspace Git Sync Compromise

**Attack**: Deploy key is used to push malicious content to the workspace repo, or workspace data is exfiltrated.

**Mitigations**:
- Deploy key is scoped to a single repo (ED25519, write access)
- SSH config uses a host alias to avoid conflicts with other keys
- Workspace repo should be private

**Residual Risk**: Low. Deploy key scope is narrow, but a compromised server could push arbitrary content to the workspace repo.

### Writable Rootfs Rationale

Sandbox containers run with `readOnlyRoot: false` (Docker `--read-only` disabled). This is a deliberate tradeoff to allow agents to install tools at runtime.

**Why**: Agents need to improvise — `pip install whisper`, `curl | bash` to install Deno, `npm install -g` for CLI tools. A read-only rootfs blocks all of these because they write to system paths or `$HOME`.

**What UID 1000 can actually write to** (verified empirically):

| Path | Writable? | Why |
|------|-----------|-----|
| `/usr/bin/`, `/usr/local/bin/`, `/etc/`, `/usr/lib/` | No | root:root 755, no DAC_OVERRIDE capability |
| `/home/node/` (pip --user, cargo, deno, etc.) | Yes | Owned by UID 1000 |
| `/workspace/` | Yes (already) | Bind mount, always writable |
| `/tmp/`, `/var/tmp/` | Yes (already) | tmpfs mounts |

**What this does NOT enable**:
- Modifying system binaries (blocked by Unix permissions — `--cap-drop ALL` removes `DAC_OVERRIDE`)
- Privilege escalation (blocked by `--security-opt no-new-privileges` + stripped setuid bits)
- Container escape (blocked by capability restrictions + namespace isolation)
- Persisting across container recreation (overlay layer destroyed when container is removed)

**What this does enable**:
- A prompt-injected session could install malicious pip packages that persist for the container's lifetime (6-33+ hours). However, this is strictly weaker than the existing `/workspace` write access, which persists indefinitely and gets synced to GitHub.

**Compensating controls**: `--cap-drop ALL`, `--security-opt no-new-privileges`, UID 1000, setuid bits stripped, bridge networking, Tailscale-only access.

## Credential Storage

Sensitive files on the server (all under `~/.openclaw/` unless noted):

| Path | Contents |
|------|----------|
| `openclaw.json` | Gateway config, auth tokens, Telegram bot token |
| `devices/paired.json` | Paired device tokens |
| `devices/pending.json` | Pending pairing requests |
| `credentials/` | Channel tokens, OAuth tokens |
| `agents/<id>/auth-profiles.json` | Model provider auth |
| `agents/<id>/sessions/*.jsonl` | Session transcripts (full conversation history) |
| `cron/jobs.json` | Cron job definitions |
| `~/.ssh/workspace-deploy-key` | GitHub deploy key (if workspace sync enabled) |

The backup script (`scripts/backup.sh`) copies `~/.openclaw/` with gateway auth tokens redacted from `openclaw.json`. Everything else is included unredacted: session transcripts, paired device tokens, channel credentials, and cron config.

The `~/.openclaw/` directory is restricted to owner-only access (mode `700`), enforced by Ansible during provisioning. This prevents other OS users from listing or accessing any files within. Re-apply with `./scripts/provision.sh --tags openclaw` if changed.

## Security Checklist

### Deployment

- [ ] Hetzner Project is dedicated to OpenClaw (not shared)
- [ ] Hetzner firewall has NO inbound rules
- [ ] UFW enabled: default deny incoming, allow tailscale0
- [ ] Tailscale auth key is ephemeral/reusable
- [ ] All secrets set via `pulumi config set --secret`
- [ ] Cloud-init log shredded after verification
- [ ] `./scripts/verify.sh` passes all checks
- [ ] `openclaw security audit --deep` shows 0 critical issues
- [ ] `openclaw security audit --fix` applied (hardens file permissions)

### Periodic Review

- [ ] Check Tailscale admin for unexpected devices: https://login.tailscale.com/admin/machines
- [ ] Review paired OpenClaw devices: `openclaw devices list`
- [ ] Check for OpenClaw updates: `npm outdated -g openclaw`
- [ ] Run `openclaw security audit --deep`
- [ ] Rotate Tailscale auth key and Claude setup token (see [CLAUDE.md — Key Rotation](../CLAUDE.md#key-rotation))

## Incident Response

1. **Isolate**: Remove server from Tailscale (`tailscale logout`)
2. **Revoke**: Rotate compromised credentials (see [CLAUDE.md — Key Rotation](../CLAUDE.md#key-rotation))
3. **Preserve**: Take Hetzner snapshot for forensics
4. **Destroy**: `pulumi destroy`
5. **Rebuild**: Fresh deployment with new credentials
