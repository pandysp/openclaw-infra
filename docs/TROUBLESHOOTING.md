# Troubleshooting Guide

Common issues and solutions for OpenClaw deployment.

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
# Common issue: Docker pull is slow
docker pull ghcr.io/anthropics/anthropic-quickstarts:computer-use-demo-latest
```

## Connectivity Issues

### Can't reach server via Tailscale

**Symptom**: `tailscale ping openclaw-vps` times out.

**Causes & Solutions**:

1. **Cloud-init not complete**
   - Wait 3-5 minutes after deployment
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

## Container Issues

### Container not starting

**Symptom**: `docker ps` shows no openclaw container.

**Diagnosis**:
```bash
# Check service status
sudo systemctl status openclaw

# Check logs
sudo journalctl -u openclaw -n 100

# Check docker directly
cd /opt/openclaw
docker compose logs
```

**Common causes**:
- Missing API key in `.env`
- Image pull failed (check network)
- Disk full (check `df -h`)

### Container keeps restarting

**Symptom**: Container status shows "Restarting" loop.

**Diagnosis**:
```bash
# Check container logs
docker logs openclaw --tail 100

# Check for crash reasons
docker inspect openclaw | jq '.[0].State'
```

**Common causes**:
- Invalid API key format
- Port conflict (something else on 18789)
- Resource limits hit

## Tailscale Serve Issues

### Web UI not accessible

**Symptom**: Browser shows connection error for `https://openclaw-vps.<tailnet>.ts.net/`.

**Diagnosis**:
```bash
ssh ubuntu@openclaw-vps.<tailnet>.ts.net

# Check serve status
tailscale serve status

# Check if container is listening
curl http://127.0.0.1:18789/

# Check if serve is running
sudo netstat -tlnp | grep tailscale
```

**Fix if serve not configured**:
```bash
tailscale serve --bg https / http://127.0.0.1:18789
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
   - CAX11 has 2 vCPU, 4GB RAM
   - Upgrade to CAX21 for more resources

3. **Docker resource limits**
   - Check: `docker stats`
   - Increase limits in docker-compose.yml if needed

### High CPU usage

**Diagnosis**:
```bash
# On server
htop
docker stats
```

**Solutions**:
- OpenClaw is CPU-intensive during browser automation
- Consider larger instance type
- Check for runaway processes in container

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

# Wait for cloud-init (~3 min)
./scripts/verify.sh
```

### Restore from backup

```bash
# Run backup script to download data
./scripts/backup.sh

# Extract
tar xzf backups/openclaw-backup-*.tar.gz

# After fresh deployment, copy data back
scp volume-data.tar.gz ubuntu@openclaw-vps.<tailnet>.ts.net:/tmp/
ssh ubuntu@openclaw-vps.<tailnet>.ts.net << 'EOF'
cd /opt/openclaw
docker compose down
docker run --rm -v openclaw-data:/data -v /tmp:/backup alpine \
    sh -c "rm -rf /data/* && tar xzf /backup/volume-data.tar.gz -C /data"
docker compose up -d
rm /tmp/volume-data.tar.gz
EOF
```

## Getting Help

1. Check cloud-init log: `sudo cat /var/log/cloud-init-openclaw.log`
2. Check service log: `sudo journalctl -u openclaw`
3. Check container log: `docker logs openclaw`
4. Verify deployment: `./scripts/verify.sh`
