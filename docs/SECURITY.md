# Security Model

This document describes the threat model and mitigations for the OpenClaw deployment.

## Architecture Overview

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
│  │  │  │  - systemd user service                   │   │   │   │
│  │  │  │  - runs as unprivileged ubuntu user       │   │   │   │
│  │  │  │  - Tailscale identity + device pairing    │   │   │   │
│  │  │  └──────────────────────────────────────────┘   │   │   │
│  │  │                        ▲                        │   │   │
│  │  │           Tailscale Serve (proxy)               │   │   │
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

Traffic must pass through multiple security layers:

| Layer | What it does | Where |
|-------|--------------|-------|
| **Hetzner Firewall** | Blocks all inbound at infrastructure | Before VM |
| **UFW** | Blocks all except tailscale0 interface | On VM |
| **Tailscale** | Encrypts + authenticates network access | Overlay network |
| **Gateway Auth** | Tailscale identity + device pairing | Application |
| **Process Isolation** | Unprivileged user, systemd user service | OS level |

## Threat Model

### Threat 1: Exposed Gateway

**Attack**: Attacker discovers and accesses the OpenClaw gateway from the internet.

**Mitigations**:
- Hetzner cloud firewall blocks ALL inbound traffic
- OpenClaw binds to `127.0.0.1` only (not `0.0.0.0`)
- Tailscale Serve proxies access through encrypted tunnel
- No DNS records point to the server's public IP
- Gateway requires authentication token even through Tailscale

**Residual Risk**: Low. Multiple layers must fail simultaneously.

### Threat 2: Compromised Server

**Attack**: Attacker gains shell access to the VPS.

**Mitigations**:
- No SSH password authentication (key-only)
- No public SSH port (Tailscale SSH only)
- Process isolation via systemd user service
- Runs as unprivileged `ubuntu` user (not root)
- Automatic security updates via unattended-upgrades
- No Docker daemon attack surface

**Blast radius**: Beyond server control, an attacker with shell access can read all session transcripts at `~/.openclaw/agents/<agentId>/sessions/*.jsonl`. These are plaintext JSONL files containing full conversation history, tool outputs, and anything discussed with the agent. File permissions (`600`) prevent other OS users from reading them, but not the `ubuntu` user or root.

**Residual Risk**: Medium. Privilege escalation possible but requires additional exploits.

### Threat 3: Setup Token Leak

**Attack**: Claude setup token is exposed.

**Mitigations**:
- Setup token stored in Pulumi encrypted state
- Never committed to git (`.gitignore`)
- Written to temp file during setup, then deleted
- Temp files have `600` permissions
- Cloud-init log should be shredded after deployment

**Residual Risk**: Low if practices followed. Rotate token via `claude setup-token` if suspected leak.

**Incident Response**:
1. Run `claude setup-token` to generate a new token
2. Update Pulumi config: `pulumi config set claudeSetupToken --secret`
3. Redeploy: `pulumi up` (or SSH to server and update `~/.openclaw/.env`)
4. Previous token automatically becomes invalid

### Gateway Token Authentication

**Why is a gateway token required if Tailscale already authenticates?**

OpenClaw enforces authentication whenever the gateway is exposed beyond localhost. This is an intentional security guardrail in OpenClaw itself—there is no way to disable it.

**Auth modes available:**
- `token` - Shared secret token (default, used by this deployment)
- `password` - Password-based auth
- ~~`none`~~ - Not supported; OpenClaw blocks this

**Why this matters:**
1. **Defense in depth** - If Tailscale is misconfigured, the token prevents unauthorized access
2. **Multi-user tailnets** - Token prevents other tailnet members from using your Claude subscription
3. **Compromised device** - If another device on your tailnet is compromised, attacker still needs the token

**User experience:**
- Token only needs to be entered once per browser (cached in localStorage)
- Use the tokenized URL from `pulumi stack output tailscaleUrlWithToken --show-secrets` for first access
- After that, the plain URL works

**Token rotation:**
```bash
# Generate new token
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'openclaw config set gateway.auth.token "$(openssl rand -base64 36)"'

# Restart gateway
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart openclaw-gateway'

# Get new token
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'openclaw config get gateway.auth.token'
```

### Tailscale Identity Auth and Device Pairing

This deployment uses **Tailscale identity authentication** with device pairing, which provides the strongest security posture:

```
gateway.auth.allowTailscale: true
gateway.controlUi.allowInsecureAuth: false
```

**How it works:**
1. Tailscale Serve passes identity headers (`Tailscale-User-Login`, etc.) to the gateway
2. Gateway validates the Tailscale identity via local `tailscaled`
3. First-time browser access requires device pairing approval
4. After pairing, the device is trusted and Tailscale identity is sufficient

**Security benefits:**
- **No token in URL** - Tailscale identity replaces token auth
- **Clean security audit** - `openclaw security audit --deep` shows 0 critical issues
- **Device tracking** - Paired devices can be listed and revoked
- **Defense in depth** - Tailscale network auth + device pairing + HTTPS

**Approving new devices:**
```bash
# List pending devices
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'openclaw devices list'

# Approve a pending request
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'openclaw devices approve <request-id>'

# Revoke a device
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'openclaw devices revoke <device-id>'
```

**Fallback access:**
If device pairing isn't working, use the tokenized URL:
```bash
pulumi stack output tailscaleUrlWithToken --show-secrets
```

**Verifying security posture:**
```bash
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'openclaw security audit --deep'
# Expected: 0 critical · 0 warn · 1 info
```

### Threat 4: Agent Host Command Abuse

**Attack**: The OpenClaw agent executes destructive or malicious commands on the host.

**Scenario**: OpenClaw has `tools.elevated.enabled: true` (the default), which gives it shell access on the host. The `ubuntu` user has passwordless sudo. If the agent is manipulated via prompt injection (e.g., through fetched web content, malicious files, or crafted messages), it could:
- Run destructive commands (`rm -rf /`, overwrite system files)
- Install malware or join the server to a botnet
- Exfiltrate secrets (Tailscale keys, config tokens) via outbound network
- Modify its own config to weaken security
- Use sudo to escalate to root

**Current status**: Elevated tools are enabled (default). This is the pragmatic choice for a single-user personal server where the agent needs to be useful.

**Mitigations in place**:
- Tailscale-only access limits who can send prompts to the agent
- Dedicated VPS with no other services to damage
- Hetzner firewall limits outbound attack surface (but allows all outbound by default)
- `agents.defaults.thinkingDefault: high` — modern instruction-hardened models with extended thinking are more robust against prompt injection

**Mitigations available but not enabled**:
- `tools.elevated.enabled: false` — disables host shell access entirely
- `agents.defaults.sandbox.mode: "non-main"` — runs agent code in Docker containers with restricted host access
- `tools.elevated.allowFrom.<channel>` — restricts which channels can trigger host commands
- Hetzner firewall outbound rules — could restrict outbound to known-good destinations

**Prompt injection defensive guidance** (from [official docs](https://docs.openclaw.ai/gateway/security)):
- Lock down inbound DMs (we use allowlist — done)
- Use mention gating in groups; avoid always-on bots in public rooms
- Treat links, attachments, and pasted instructions as potentially hostile
- Keep secrets out of the agent filesystem where possible
- Prefer the latest, strongest model for tool-enabled agents — weaker models are less robust against injection
- Red flags: requests to "read this URL and do exactly what it says", ignore system prompts, or reveal hidden instructions

**Residual Risk**: Medium. A single-user Tailscale-only setup limits the attack surface for prompt injection, but the agent has full root access if exploited. Enabling sandbox mode would reduce this to Low for most scenarios.

### Threat 5: Lateral Movement

**Attack**: Attacker uses compromised server to attack other systems.

**Mitigations**:
- Server is isolated (single-purpose VPS)
- No access to internal networks
- Tailscale device has minimal ACL permissions
- Outbound-only firewall prevents hosting attack infrastructure

**Residual Risk**: Low. Limited attack surface.

### Threat 6: Supply Chain Attack

**Attack**: Malicious npm package or dependency.

**Mitigations**:
- Use official OpenClaw package from npm registry
- Node.js installed via official OpenClaw installer
- Pulumi dependencies from npm registry
- Automatic security patches via unattended-upgrades

**Residual Risk**: Medium. Trust in upstream is required.

### Threat 7: Malicious Plugin

**Attack**: A compromised or malicious OpenClaw plugin gains full access to the gateway.

**Scenario**: OpenClaw plugins run in-process with the gateway — they are not sandboxed. A plugin has the same access as the gateway itself:
- Read/write `~/.openclaw/` (config, credentials, auth tokens, session transcripts)
- Access all channel connections (Telegram, etc.)
- Make network calls (exfiltrate data)
- Execute as the `ubuntu` user (including sudo)
- npm lifecycle scripts execute during installation, before you've reviewed the code

**Current status**: No plugins installed. This threat becomes relevant when plugins are added.

**Mitigations for when plugins are used**:
- Review plugin source code before installing
- Use `plugins.allow` to explicitly allowlist only approved plugins
- Pin exact versions (`@scope/pkg@1.2.3`) to prevent supply chain attacks via version ranges
- Monitor `~/.openclaw/extensions/` for unexpected additions
- Prefer plugins from known/trusted authors
- Restart the gateway after any plugin changes

**Residual Risk**: Low (no plugins installed). Will become Medium when plugins are added — treat plugin installation with the same caution as granting shell access.

### Threat 8: Infrastructure Token Compromise

**Attack**: Attacker obtains Hetzner API token and pivots to other infrastructure.

**Mitigations**:
- **Use a dedicated Hetzner Project** for OpenClaw (hard isolation)
- Use a separate API token not shared with other projects
- Token only exists in Pulumi encrypted state (never on VPS)
- Minimal token permissions where possible

**Residual Risk**: Low if isolated. High if token is shared with production infrastructure.

**Why this matters**: OpenClaw runs autonomous AI agents that could be vulnerable to prompt injection or other attacks. If compromised, an attacker with a shared Hetzner token could delete servers, create expensive instances, or pivot to other infrastructure in the same project.

### Threat 9: Self-Modification via Node Control

**Attack**: OpenClaw controls its own infrastructure deployment through a connected node.

**Scenario**: OpenClaw can be configured to control "nodes" (computers it can operate), including your Mac. If:
1. Your Mac is added as an OpenClaw node
2. Pulumi state and passphrase are on your Mac
3. OpenClaw (or an attacker via prompt injection) accesses your Mac

Then OpenClaw could theoretically:
- Read/modify Pulumi state
- Destroy or modify its own infrastructure
- Access secrets stored locally

**Mitigations**:
- Don't add your infrastructure management machine as an OpenClaw node
- Use a separate machine for OpenClaw node operations
- Store Pulumi passphrase in a password manager, not in shell history/envrc
- Use Pulumi Cloud backend (requires browser auth, not just passphrase)
- Consider remote-only deployment (CI/CD from GitHub Actions, not local machine)

**Residual Risk**: Medium if Mac is an OpenClaw node. Low if infrastructure management is isolated.

### Threat 10: Cloud-Init Log Exposure

**Attack**: Secrets visible in cloud-init log on the server.

**Mitigations**:
- Secrets written to temp files (600 permissions), not CLI arguments
- `set +x` disables command logging during secret operations
- Log should be shredded after verifying deployment works

**Incident Response**:
```bash
# After verifying deployment works
ssh ubuntu@openclaw-vps.<tailnet>.ts.net "sudo shred -u /var/log/cloud-init-openclaw.log"
```

**Residual Risk**: Low if log is cleaned up. Medium if forgotten.

## Browser Control (Future Planning)

Not currently enabled. When the agent needs to interact with authenticated websites (shopping, booking, admin dashboards, etc.), there are three viable approaches with different security tradeoffs.

### Option B: Cookie Sync to VPS Headless Chrome

**How it works**: Run headless Chromium on the VPS. Periodically export cookies for specific domains from your Mac's Chrome and sync them to the VPS browser profile.

| Pros | Cons |
|------|------|
| Agent stays contained on VPS | Cookies expire, need periodic re-sync |
| No Mac exposure | Fragile (Chrome cookie DB format can change) |
| Selective — only domains you choose | Requires scripting and maintenance |

**Implementation**: Script extracts cookies from Mac Chrome SQLite DB (`~/Library/Application Support/Google/Chrome/Default/Cookies`) for allowlisted domains, transfers via scp, imports into headless Chrome profile on VPS.

**Best for**: Low-frequency tasks on a small number of sites where sessions are long-lived.

### Option D: Password Vault with Dedicated Email

**How it works**: Give the agent access to a dedicated password vault (1Password, Bitwarden) containing only approved credentials. Agent logs in fresh on VPS headless Chrome.

| Pros | Cons |
|------|------|
| Clean separation — you choose exactly which accounts | 2FA and passwordless login are a problem |
| No Mac exposure | Agent must handle login flows |
| Auditable — vault logs show what was accessed | More setup per site |

**The 2FA/passwordless problem**: Most sites now use email magic links or SMS codes. Solutions:
- **Dedicated email** (e.g., `agent@yourdomain.com`) that only receives login codes, with the agent having IMAP access. Limited blast radius — this email has no other accounts attached.
- **TOTP-based 2FA**: Share the TOTP secret with the agent. Works for sites that support authenticator apps.
- **SMS forwarding**: Forward codes from a dedicated phone number to the agent via Telegram.

**Best for**: Sites with stable login flows and TOTP support. The dedicated email approach extends this to passwordless sites.

### Option E: Mac as Node with Restricted Browser Profile (Recommended)

**How it works**: Pair your Mac as an OpenClaw node, but restrict it to a dedicated Chrome profile with only specific logins, no shell access, and a website allowlist.

| Pros | Cons |
|------|------|
| Most practical — real browser, real logins | Mac is exposed (though restricted) |
| Website allowlist prevents malicious navigation | Requires Mac to be online |
| Exec denial prevents shell access | Dedicated profile needs separate login sessions |

**Key restrictions to apply**:
1. **Dedicated Chrome profile** — not your main profile. Only log into sites the agent should access.
2. **Exec approvals: "deny"** — agent can control the browser but cannot run terminal commands on your Mac (Settings → Exec approvals).
3. **Website allowlist** — restrict which domains the agent can navigate to. Prevents prompt injection from steering the browser to malicious sites.
4. **Isolate from infrastructure** — keep Pulumi state and infra credentials off the Mac (or in a separate user account) to prevent Threat 9 (Self-Modification).

**Best for**: Most use cases. Balances convenience with security. The website allowlist is the critical control — it limits blast radius even if the agent is prompt-injected.

### Recommendation

Start with **Option E** for general-purpose browser tasks. The dedicated profile + exec deny + website allowlist combination provides a practical security boundary. Add **Option D** for headless automation of specific sites where you don't want Mac exposure at all.

Avoid giving the agent access to your main browser profile or unrestricted shell access on your Mac.

## Credential Storage Paths

Sensitive data on the server that should be backed up carefully and protected with `600` permissions:

| Path | Contents |
|------|----------|
| `~/.openclaw/openclaw.json` | Gateway config including auth tokens |
| `~/.openclaw/devices/paired.json` | Paired device tokens |
| `~/.openclaw/devices/pending.json` | Pending pairing requests |
| `~/.openclaw/credentials/` | Channel allowlists, OAuth tokens |
| `~/.openclaw/agents/<agentId>/auth-profiles.json` | Model provider auth |
| `~/.openclaw/agents/<agentId>/sessions/*.jsonl` | Session transcripts (contain conversation history) |
| `~/.openclaw/cron/jobs.json` | Cron job definitions |

The backup script (`scripts/backup.sh`) copies `~/.openclaw/` and redacts gateway auth tokens. Session transcripts are included — be aware they contain full conversation history.

## Security Checklist

### Deployment

- [ ] **Hetzner Project is dedicated to OpenClaw** (not shared with other infra)
- [ ] Hetzner API token is unique to this project
- [ ] Hetzner firewall has NO inbound rules
- [ ] UFW enabled with default deny incoming, allow tailscale0
- [ ] SSH keys are Pulumi-generated (no password auth)
- [ ] Tailscale auth key is ephemeral/reusable
- [ ] Setup token is set via `pulumi config set --secret`
- [ ] Cloud-init log is shredded after verification

### Verification

- [ ] `./scripts/verify.sh` passes all checks
- [ ] No ports are publicly accessible (check with `nmap`)
- [ ] Tailscale Serve is configured correctly
- [ ] Service runs as unprivileged user
- [ ] `openclaw security audit --deep` shows 0 critical issues
- [ ] `openclaw security audit --fix` applied (hardens file permissions and guardrails)
- [ ] UFW status shows only tailscale0 allowed

### Ongoing Monitoring

**Weekly:**
- [ ] Check Tailscale admin console for unexpected devices: https://login.tailscale.com/admin/machines
- [ ] Review paired OpenClaw devices: `openclaw devices list`
- [ ] Check for OpenClaw updates: `npm outdated -g openclaw`

**Monthly:**
- [ ] Review Tailscale ACLs if using shared tailnet
- [ ] Check unattended-upgrades logs for failed updates
- [ ] Run `openclaw security audit --deep`

**Quarterly:**
- [ ] Rotate Tailscale auth key (generate new, update Pulumi config)
- [ ] Rotate Claude setup token (`claude setup-token`)
- [ ] Review and remove unused paired devices
- [ ] Check Hetzner project for unexpected resources

### Key Rotation Schedule

| Secret | Rotation Frequency | How to Rotate |
|--------|-------------------|---------------|
| Tailscale auth key | Quarterly | Generate at tailscale.com, `pulumi config set tailscaleAuthKey --secret` |
| Claude setup token | Quarterly | `claude setup-token`, `pulumi config set claudeSetupToken --secret` |
| Gateway token | On compromise only | `pulumi up` (auto-generates new token) |
| Pulumi passphrase | On compromise only | Cannot rotate - must redeploy from scratch |

## Incident Response

### Suspected Compromise

1. **Isolate**: Remove server from Tailscale (`tailscale logout`)
2. **Revoke**: Rotate Claude setup token via `claude setup-token`
3. **Preserve**: Take snapshot of server for forensics
4. **Destroy**: `pulumi destroy` the infrastructure
5. **Rebuild**: Fresh deployment with new credentials

### Setup Token Rotation

1. Run `claude setup-token` in your terminal
2. Authorize in browser
3. Update Pulumi config: `pulumi config set claudeSetupToken --secret`
4. Redeploy: `pulumi up`
5. Previous token automatically becomes invalid
