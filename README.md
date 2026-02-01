# OpenClaw Infrastructure

Deploy [OpenClaw](https://github.com/anthropics/anthropic-quickstarts/tree/main/computer-use-demo) (Anthropic Computer Use) on a €5/month Hetzner VPS with zero-trust Tailscale networking.

## Features

- **Cheap**: €4.51/mo ARM server + €0.90 backups
- **Secure**: No public ports, Tailscale-only access
- **Simple**: Pulumi IaC, single command deploy
- **Portable**: Docker container, easy to backup/migrate

## Quick Start

```bash
# Prerequisites: Node.js 18+, Pulumi CLI, Tailscale CLI

# Install
npm install

# Configure
cd pulumi
pulumi stack init prod
pulumi config set hcloud:token --secret    # Hetzner API token
pulumi config set tailscaleAuthKey --secret # Tailscale auth key
pulumi config set anthropicApiKey --secret  # Anthropic API key

# Deploy
pulumi up

# Verify (wait ~3 min for cloud-init)
cd ..
./scripts/verify.sh
```

## Access

After deployment, access OpenClaw at:
```
https://openclaw-vps.<your-tailnet>.ts.net/
```

## Documentation

- [CLAUDE.md](./CLAUDE.md) - Comprehensive setup and operations guide
- [docs/SECURITY.md](./docs/SECURITY.md) - Threat model and mitigations
- [docs/TROUBLESHOOTING.md](./docs/TROUBLESHOOTING.md) - Common issues

## Architecture

```
Your Machine ──(Tailscale)──▶ Hetzner VPS ──▶ Docker ──▶ OpenClaw
                              (no public ports)    (localhost:18789)
```

## License

MIT
