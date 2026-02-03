# Troubleshooting

Common issues and solutions for the OpenClaw deployment.

**Quick reference** — jump to your symptom:
- Can't reach server → [Connectivity](#connectivity-issues)
- "Pairing required" in browser → [Device Pairing](#device-pairing-not-working)
- Service not running → [Service Issues](#service-issues)
- Token problems → [Setup Token](#setup-token-issues)
- Web UI not loading → [Tailscale Serve](#tailscale-serve-issues)
- Workspace sync not working → [Workspace Git Sync](#workspace-git-sync-issues)

> **Note**: All `systemctl --user` and `journalctl --user` commands on the server require `XDG_RUNTIME_DIR=/run/user/1000`. Either prefix each command or run `export XDG_RUNTIME_DIR=/run/user/1000` once per session.

## Deployment Issues

### Pulumi "passphrase must be set" error

**Symptom**: Any `pulumi` command fails with passphrase error.

**Solution**:
```bash
export PULUMI_CONFIG_PASSPHRASE="your-passphrase"
```

See [CLAUDE.md — Pulumi Passphrase](../CLAUDE.md#pulumi-passphrase) for options.

### Pulumi fails with "unauthorized"

**Symptom**: `pulumi up` fails with Hetzner auth error.

**Solution**:
```bash
pulumi config set hcloud:token --secret
```

### Missing @pulumi/random module

**Symptom**: `pulumi preview` fails with "Cannot find module '@pulumi/random'".

**Solution**: Run `npm install` from the project root.

### Cloud-init not completing

**Symptom**: Server is up but OpenClaw not accessible after 10+ minutes.

Cloud-init typically completes in 3-5 minutes. If still not done after 10:

```bash
# Check status
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'sudo cloud-init status'

# If "running", wait. If "error", check the log:
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'sudo tail -50 /var/log/cloud-init-openclaw.log'
```

If Tailscale never started (can't SSH), use the Hetzner Cloud console (web terminal) to access the server directly.

### Server hostname changed after redeploy

**Symptom**: After `pulumi destroy` + `pulumi up`, SSH commands fail because hostname is now `openclaw-vps-2` instead of `openclaw-vps`.

Each redeploy creates a new Tailscale device with an incremented suffix. Check `tailscale status` for the current hostname, and clean up stale devices in the [Tailscale admin console](https://login.tailscale.com/admin/machines).

See [CLAUDE.md — Clean Up Stale Tailscale Devices](../CLAUDE.md#clean-up-stale-tailscale-devices).

## Connectivity Issues

### Can't reach server via Tailscale

**Symptom**: `tailscale ping openclaw-vps` times out.

1. **Cloud-init not complete** — wait 5 minutes, check [Tailscale admin](https://login.tailscale.com/admin/machines) for the device
2. **Auth key expired** — generate a new key in Tailscale admin, then SSH via Hetzner console and re-run `tailscale up --authkey=<new-key> --hostname=openclaw-vps --ssh`
3. **Tailscale not installed** — SSH via Hetzner console, run `curl -fsSL https://tailscale.com/install.sh | sh`

### SSH connection refused

**Symptom**: Can ping via Tailscale but SSH fails.

Tailscale SSH is enabled by default in this deployment (`--ssh` flag). If SSH is refused:
- Cloud-init may still be running — wait and retry
- Remove stale host key: `ssh-keygen -R openclaw-vps.<tailnet>.ts.net`

## Device Pairing

### Device pairing not working

**Symptom**: Browser shows "pairing required" after opening the gateway URL.

This is expected on first visit. Approve your device:
```bash
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'openclaw devices list'
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'openclaw devices approve <request-id>'
```

Then refresh the browser.

**Common causes of repeated pairing prompts**:
- Browser localStorage was cleared
- Using incognito/private mode
- Different browser or device

**Fallback**: Use the tokenized URL to bypass pairing:
```bash
cd pulumi && pulumi stack output tailscaleUrlWithToken --show-secrets
```

## Service Issues

### Service not running

**Symptom**: Gateway not responding.

```bash
# Quick fix — restart the service
ssh ubuntu@openclaw-vps.<tailnet>.ts.net \
  'XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart openclaw-gateway'
```

If that doesn't work, diagnose:
```bash
ssh ubuntu@openclaw-vps.<tailnet>.ts.net

XDG_RUNTIME_DIR=/run/user/1000 systemctl --user status openclaw-gateway
XDG_RUNTIME_DIR=/run/user/1000 journalctl --user -u openclaw-gateway -n 100

# Check if service file exists
ls -la ~/.config/systemd/user/openclaw-gateway.service
```

**Common causes**: setup token expired, user lingering not enabled, Node.js not in PATH.

**Fixes**:
```bash
# Re-enable user lingering
sudo loginctl enable-linger ubuntu

# Reinstall daemon
XDG_RUNTIME_DIR=/run/user/1000 openclaw daemon install
XDG_RUNTIME_DIR=/run/user/1000 systemctl --user start openclaw-gateway
```

### Service keeps restarting

**Symptom**: Service shows "activating (auto-restart)" or crash loop.

```bash
XDG_RUNTIME_DIR=/run/user/1000 journalctl --user -u openclaw-gateway -n 200
```

**Common causes**: invalid setup token, port conflict (something else on 18789), Node.js version mismatch (needs v22+).

### Node.js not found

**Symptom**: Service fails with "node: command not found".

```bash
# Reinstall OpenClaw (includes Node.js)
OPENCLAW_NO_ONBOARD=1 OPENCLAW_NO_PROMPT=1 curl -fsSL https://openclaw.ai/install.sh | bash

# Reinstall daemon and restart
XDG_RUNTIME_DIR=/run/user/1000 openclaw daemon install
XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart openclaw-gateway
```

## Setup Token Issues

### Token expired or invalid

**Symptom**: OpenClaw fails to authenticate, logs show auth errors.

```bash
# Generate new token locally
claude setup-token

# Re-onboard on the server
ssh ubuntu@openclaw-vps.<tailnet>.ts.net

openclaw onboard --non-interactive --accept-risk \
    --mode local \
    --auth-choice token \
    --token "YOUR_NEW_TOKEN" \
    --token-provider anthropic \
    --gateway-port 18789 \
    --gateway-bind loopback \
    --skip-daemon \
    --skip-skills

XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart openclaw-gateway
```

### /status shows no usage tracking

**Symptom**: OpenClaw works but /status endpoint doesn't show usage.

This is expected. Setup tokens only have `user:inference` scope (missing `user:profile`). See [GitHub issue #4614](https://github.com/openclaw/openclaw/issues/4614).

## Tailscale Serve Issues

### Web UI not accessible

**Symptom**: Browser shows connection error for `https://openclaw-vps.<tailnet>.ts.net/`.

```bash
ssh ubuntu@openclaw-vps.<tailnet>.ts.net

# Check if serve is configured
tailscale serve status

# Check if gateway is listening
curl -s http://127.0.0.1:18789/ | head -5
```

OpenClaw manages Tailscale Serve automatically. Restart the gateway to re-establish it:
```bash
XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart openclaw-gateway
```

### HTTPS certificate errors

**Symptom**: Browser warns about invalid certificate.

Always use the Tailscale DNS name (`https://openclaw-vps.<tailnet>.ts.net/`), not the IP address.

## Workspace Git Sync Issues

### Sync not running

**Symptom**: No commits appearing in the workspace repo.

```bash
ssh ubuntu@openclaw-vps.<tailnet>.ts.net

# Check timer
XDG_RUNTIME_DIR=/run/user/1000 systemctl --user status workspace-git-sync.timer

# Check last sync result
XDG_RUNTIME_DIR=/run/user/1000 systemctl --user status workspace-git-sync.service

# Trigger a manual sync
XDG_RUNTIME_DIR=/run/user/1000 systemctl --user start workspace-git-sync.service

# Check git log
cd ~/.openclaw/workspace && git log --oneline -5
```

**Common causes**: deploy key not added to GitHub repo, repo doesn't exist, SSH host key not accepted.

## Telegram / Cron Issues

**Most common fix**: You must message your bot first (`/start` in Telegram) before it can message you.

For full Telegram and cron troubleshooting, see [CLAUDE.md](../CLAUDE.md#telegram-not-working).

## Recovery

### Rebuild from scratch

If all else fails:

```bash
cd pulumi
pulumi destroy
# Wait 60 seconds for Hetzner cleanup
pulumi up
# Wait ~5 min for cloud-init
cd .. && ./scripts/verify.sh
# Clean up cloud-init log (contains secrets)
ssh ubuntu@openclaw-vps.<tailnet>.ts.net "sudo shred -u /var/log/cloud-init-openclaw.log"
```

## General Diagnostics

```bash
# Cloud-init log
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'sudo tail -50 /var/log/cloud-init-openclaw.log'

# Service log
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'XDG_RUNTIME_DIR=/run/user/1000 journalctl --user -u openclaw-gateway -n 50'

# Gateway health
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'curl -s http://127.0.0.1:18789/'

# Verify deployment
./scripts/verify.sh
```
