# OpenClaw Infrastructure

Deploy [OpenClaw](https://github.com/anthropics/anthropic-quickstarts/tree/main/computer-use-demo) (Anthropic Computer Use) on a Hetzner VPS with zero-trust Tailscale networking. ~€7.79/month.

## Features

- **Cheap**: €6.49/mo ARM server + €1.30 backups
- **Secure**: No public ports, Tailscale-only access, device pairing
- **Simple**: Pulumi IaC, single command deploy
- **Extensible**: Optional Telegram integration with scheduled tasks

## Quick Start

```bash
# Prerequisites: Node.js 18+, Pulumi CLI, Tailscale
# See CLAUDE.md for detailed installation instructions

# Install
npm install

# Configure
cd pulumi
pulumi stack init prod
pulumi config set hcloud:token --secret       # Hetzner API token
pulumi config set tailscaleAuthKey --secret    # Tailscale auth key
pulumi config set claudeSetupToken --secret    # From `claude setup-token`

# Deploy
pulumi up

# Verify (wait ~5 min for cloud-init)
cd ..
./scripts/verify.sh
```

## Access

After deployment, open in your browser:
```
https://openclaw-vps.<your-tailnet>.ts.net/
```

First visit shows **"pairing required"** — this is expected. Approve your device via SSH:
```bash
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'openclaw devices list'
ssh ubuntu@openclaw-vps.<tailnet>.ts.net 'openclaw devices approve <request-id>'
```

See [CLAUDE.md](./CLAUDE.md#first-time-access-device-pairing) for details.

## Architecture

```
Your Machine ──(Tailscale)──> Hetzner VPS ──> OpenClaw Gateway
                               (no public ports)  (localhost:18789, systemd)
```

## Documentation

- [CLAUDE.md](./CLAUDE.md) - Comprehensive setup and operations guide
- [docs/SECURITY.md](./docs/SECURITY.md) - Threat model and mitigations
- [docs/TROUBLESHOOTING.md](./docs/TROUBLESHOOTING.md) - Common issues

## License

[MIT](./LICENSE)
