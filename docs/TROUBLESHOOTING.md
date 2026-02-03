# Troubleshooting Guide

Common issues and solutions for OpenClaw deployment.

## Local Setup Issues

### Pulumi "passphrase must be set" error

**Symptom**: `pulumi stack init` fails with passphrase error.

**Solution**: Set the passphrase environment variable:
```bash
# Option 1: Set per-command
PULUMI_CONFIG_PASSPHRASE="your-passphrase" pulumi stack init prod

# Option 2: Export for session
export PULUMI_CONFIG_PASSPHRASE="your-passphrase"
```

### Tailscale stuck on "VPN starting..."

**Symptom**: macOS Tailscale app shows "VPN starting..." indefinitely.

**Solutions**:
1. Check System Settings → Privacy & Security for pending approvals
2. Look for "Tailscale" blocked message and click "Allow"
3. Quit and reopen Tailscale (menu bar icon → Quit Tailscale)
4. If still stuck, restart your Mac

### Tailscale System Extension blocked

**Symptom**: macOS blocks Tailscale kernel extension.

**Solution**:
1. Open System Settings → Privacy & Security
2. Scroll down to see blocked extension message
3. Click "Allow" next to Tailscale
4. May require restart

### brew install tailscale requires password

**Symptom**: `brew install --cask tailscale` prompts for sudo password.

**Solution**: Run in an interactive terminal (not from an IDE or script):
```bash
brew install --cask tailscale
```

## Deployment Issues

### Pulumi fails with "unauthorized"

**Symptom**: `pulumi up` fails with Hetzner auth error.

**Solution**:
```bash
# Re-set the Hetzner token
pulumi config set hcloud:token --secret
# Enter your token when prompted
```

### Cloud-init takes too long

**Symptom**: Server is up but OpenClaw not accessible after 10+ minutes.

**Solution**:
```bash
# SSH via Hetzner console (fallback)
# Check cloud-init status
cloud-init status

# View the log
sudo cat /var/log/cloud-init-openclaw.log

# Common issue: install script is slow on first run
# Check if Node.js is installed
node --version
```

### Missing @pulumi/random module

**Symptom**: `pulumi preview` fails with "Cannot find module '@pulumi/random'".

**Solution**:
```bash
cd ~/projects/openclaw-infra
npm install
```

## Connectivity Issues

### Can't reach server via Tailscale

**Symptom**: `tailscale ping openclaw-vps` times out.

**Causes & Solutions**:

1. **Cloud-init not complete**
   - Wait 5 minutes after deployment
   - Check Tailscale admin console for the device

2. **Auth key expired**
   - Generate new auth key in Tailscale admin
   - SSH via Hetzner console, re-run `tailscale up --authkey=...`

3. **Tailscale not installed**
   - SSH via Hetzner console
   - Check: `which tailscale`
   - Re-run install: `curl -fsSL https://tailscale.com/install.sh | sh`

### SSH connection refused

**Symptom**: Can ping via Tailscale but SSH fails.

**Solutions**:
```bash
# Tailscale SSH (if enabled)
ssh -o ProxyCommand="tailscale nc %h %p" ubuntu@openclaw-vps

# Or enable Tailscale SSH in ACLs
# https://tailscale.com/kb/1193/tailscale-ssh
```

## OpenClaw Service Issues

### Service not running

**Symptom**: Gateway not responding, service inactive.

**Diagnosis**:
```bash
ssh ubuntu@openclaw-vps.<tailnet>.ts.net

# Check service status (user service requires XDG_RUNTIME_DIR)
XDG_RUNTIME_DIR=/run/user/1000 systemctl --user status openclaw-gateway

# Check logs
XDG_RUNTIME_DIR=/run/user/1000 journalctl --user -u openclaw-gateway -n 100

# Check if the service file exists
ls -la ~/.config/systemd/user/openclaw.service
```

**Common causes**:
- Setup token invalid or expired
- Node.js not in PATH
- User lingering not enabled

**Fixes**:
```bash
# Re-enable user lingering
sudo loginctl enable-linger ubuntu

# Restart user systemd
systemctl start user@1000.service

# Reinstall daemon
XDG_RUNTIME_DIR=/run/user/1000 openclaw daemon install
XDG_RUNTIME_DIR=/run/user/1000 systemctl --user start openclaw-gateway
```

### Service keeps restarting

**Symptom**: Service shows "activating (auto-restart)" or crash loop.

**Diagnosis**:
```bash
# Check recent logs for errors
XDG_RUNTIME_DIR=/run/user/1000 journalctl --user -u openclaw-gateway -n 200

# Check if OpenClaw is installed
which openclaw
openclaw --version
```

**Common causes**:
- Invalid setup token format
- Port conflict (something else on 18789)
- Node.js version mismatch (needs v22+)

### Node.js not found

**Symptom**: Service fails with "node: command not found".

**Solution**: Reinstall using the official installer:
```bash
# Verify Node.js is installed
node --version  # Should show v22.x.x

# If missing, reinstall OpenClaw (includes Node.js)
OPENCLAW_NO_ONBOARD=1 OPENCLAW_NO_PROMPT=1 curl -fsSL https://openclaw.ai/install.sh | bash

# Reinstall daemon
XDG_RUNTIME_DIR=/run/user/1000 openclaw daemon install
XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart openclaw-gateway
```

## Setup Token Issues

### Token expired or invalid

**Symptom**: OpenClaw fails to authenticate, logs show auth errors.

**Solution**:
```bash
# Generate new setup token locally
claude setup-token

# Update on server
ssh ubuntu@openclaw-vps.<tailnet>.ts.net

# Re-run onboarding with new token
openclaw onboard --non-interactive --accept-risk \
    --mode local \
    --auth-choice setup-token \
    --token "YOUR_NEW_TOKEN" \
    --gateway-port 18789 \
    --gateway-bind loopback \
    --skip-daemon \
    --skip-skills

# Restart service
XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart openclaw-gateway
```

### /status shows no usage tracking

**Symptom**: OpenClaw works but /status endpoint doesn't show usage.

**Cause**: This is expected behavior. Setup tokens only have `user:inference` scope (missing `user:profile`), so usage tracking isn't available. See [GitHub issue #4614](https://github.com/openclaw/openclaw/issues/4614).

**Workaround**: None currently. This is a limitation of setup tokens.

## Tailscale Serve Issues

### Web UI not accessible

**Symptom**: Browser shows connection error for `https://openclaw-vps.<tailnet>.ts.net/`.

**Diagnosis**:
```bash
ssh ubuntu@openclaw-vps.<tailnet>.ts.net

# Check serve status
tailscale serve status

# Check if gateway is listening
curl http://127.0.0.1:18789/

# Check if serve is running
sudo ss -tlnp | grep tailscale
```

**Fix if serve not configured**:
```bash
tailscale serve --bg 18789
```

### HTTPS certificate errors

**Symptom**: Browser warns about invalid certificate.

**Cause**: Using IP instead of Tailscale DNS name.

**Solution**: Always use `https://openclaw-vps.<tailnet>.ts.net/`, not the IP.

## Performance Issues

### Slow response times

**Causes & Solutions**:

1. **Geographic distance**
   - Server is in Frankfurt (fsn1)
   - Consider different Hetzner location

2. **Resource constraints**
   - CAX21 has 4 vCPU, 8GB RAM
   - Check resource usage: `htop`

3. **Node.js overhead**
   - Node.js startup adds slight overhead
   - Not significant for running service

### High CPU usage

**Diagnosis**:
```bash
# On server
htop

# Check OpenClaw specifically
ps aux | grep openclaw
```

**Solutions**:
- OpenClaw is CPU-intensive during browser automation
- Consider larger instance type
- Check for runaway processes

## Recovery Procedures

### Rebuild from scratch

If all else fails:

```bash
# Destroy infrastructure
cd pulumi
pulumi destroy

# Wait 60 seconds for Hetzner cleanup

# Redeploy
pulumi up

# Wait for cloud-init (~5 min)
./scripts/verify.sh

# Clean up cloud-init log
ssh ubuntu@openclaw-vps.<tailnet>.ts.net "sudo shred -u /var/log/cloud-init-openclaw.log"
```

### Manual service restart

```bash
ssh ubuntu@openclaw-vps.<tailnet>.ts.net

# Set environment and restart
export XDG_RUNTIME_DIR=/run/user/1000

systemctl --user restart openclaw-gateway
systemctl --user status openclaw-gateway
```

### Update OpenClaw manually

```bash
ssh ubuntu@openclaw-vps.<tailnet>.ts.net

# Update to latest version
OPENCLAW_NO_ONBOARD=1 OPENCLAW_NO_PROMPT=1 curl -fsSL https://openclaw.ai/install.sh | bash

# Restart service
XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart openclaw-gateway
```

## Getting Help

1. Check cloud-init log: `sudo cat /var/log/cloud-init-openclaw.log`
2. Check service log: `XDG_RUNTIME_DIR=/run/user/1000 journalctl --user -u openclaw-gateway`
3. Check gateway directly: `curl http://127.0.0.1:18789/`
4. Verify deployment: `./scripts/verify.sh`
