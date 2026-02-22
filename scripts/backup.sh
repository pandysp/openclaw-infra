#!/bin/bash
#
# OpenClaw Backup Script
#
# Creates a backup of OpenClaw configuration and session data.
# Workspaces are excluded (synced to GitHub via git sync timers).
# Run locally - connects to server via Tailscale.
#
# Usage:
#   ./scripts/backup.sh
#   OPENCLAW_HOSTNAME=openclaw-vps-2 ./scripts/backup.sh  # if Tailscale assigned a suffix

set -euo pipefail

# Configuration
HOSTNAME="${OPENCLAW_HOSTNAME:-openclaw-vps}"
TAILNET="${TAILNET:-}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

# Detect tailnet if not set
if [ -z "$TAILNET" ]; then
    TAILNET=$(tailscale status --json 2>/dev/null | jq -r '.MagicDNSSuffix // empty' || true)
    if [ -z "$TAILNET" ]; then
        echo -e "${RED}Error: TAILNET not set and could not be detected.${NC}"
        echo "Set it with: export TAILNET=your-tailnet.ts.net"
        exit 1
    fi
fi

FULL_HOSTNAME="${HOSTNAME}.${TAILNET}"
BACKUP_FILE="${BACKUP_DIR}/openclaw-backup-${TIMESTAMP}.tar.gz"

echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║                     OpenClaw Backup                              ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""
echo "Server: $FULL_HOSTNAME"
echo "Backup: $BACKUP_FILE"
echo ""

# Create backup directory
mkdir -p "$BACKUP_DIR"

# Stop service temporarily (optional - for consistent backup)
read -p "Stop OpenClaw service during backup for consistency? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "Stopping OpenClaw service..."
    ssh "ubuntu@$FULL_HOSTNAME" "XDG_RUNTIME_DIR=/run/user/1000 systemctl --user stop openclaw-gateway"
    STOPPED=true
else
    STOPPED=false
fi

# Create backup on server
echo "Creating backup on server..."
ssh "ubuntu@$FULL_HOSTNAME" << 'REMOTE_SCRIPT'
set -e

BACKUP_TMP=$(mktemp -d)
trap 'rm -rf "$BACKUP_TMP"' EXIT

# Copy .openclaw to temp dir, excluding workspaces and large caches
echo "Copying config and sessions (excluding git-synced workspaces)..."
rsync -a \
    --exclude='workspace' \
    --exclude='workspace-*' \
    --exclude='agents/*/workspace' \
    --exclude='extensions/*/node_modules' \
    --exclude='qmd/*/embeddings' \
    ~/.openclaw/ "$BACKUP_TMP/openclaw-config/"

# Redact tokens from the backup copy
echo "Redacting sensitive tokens..."
if [ -f "$BACKUP_TMP/openclaw-config/openclaw.json" ]; then
    python3 -c "
import json, sys
try:
    with open('$BACKUP_TMP/openclaw-config/openclaw.json') as f:
        c = json.load(f)
except (json.JSONDecodeError, IOError) as e:
    print(f'ERROR: Could not parse openclaw.json for redaction: {e}', file=sys.stderr)
    print('Aborting — refusing to create backup with unredacted secrets.', file=sys.stderr)
    sys.exit(1)
# Redact all known sensitive fields
for path in [('gateway','auth','token'), ('gateway','remote','token'),
             ('channels','telegram','botToken'), ('tools','web','search','grok','apiKey')]:
    obj = c
    for key in path[:-1]:
        obj = obj.get(key, {})
    if path[-1] in obj:
        obj[path[-1]] = '<REDACTED>'
with open('$BACKUP_TMP/openclaw-config/openclaw.json', 'w') as f:
    json.dump(c, f, indent=2)
"
fi

# Create archive from redacted copy
echo "Creating archive..."
tar czf /tmp/openclaw-backup.tar.gz -C "$BACKUP_TMP" openclaw-config

BACKUP_SIZE=$(du -h /tmp/openclaw-backup.tar.gz | cut -f1)
echo "Backup created: /tmp/openclaw-backup.tar.gz ($BACKUP_SIZE)"
REMOTE_SCRIPT

# Download backup
echo "Downloading backup..."
scp "ubuntu@$FULL_HOSTNAME:/tmp/openclaw-backup.tar.gz" "$BACKUP_FILE"

# Cleanup remote backup
ssh "ubuntu@$FULL_HOSTNAME" "rm /tmp/openclaw-backup.tar.gz"

# Restart service if stopped
if [ "$STOPPED" = true ]; then
    echo "Restarting OpenClaw service..."
    ssh "ubuntu@$FULL_HOSTNAME" "XDG_RUNTIME_DIR=/run/user/1000 systemctl --user start openclaw-gateway"
fi

echo ""
echo -e "${GREEN}Backup complete: $BACKUP_FILE${NC}"
BACKUP_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
echo "Size: $BACKUP_SIZE"
echo ""
echo "Contents (workspaces excluded — synced to GitHub separately):"
echo "  openclaw-config/openclaw.json   - Gateway config (tokens redacted)"
echo "  openclaw-config/agents/         - Agent sessions, settings"
echo "  openclaw-config/devices/        - Paired devices"
echo "  openclaw-config/extensions/     - Plugins (without node_modules)"
echo "  openclaw-config/media/          - Media files"
echo "  openclaw-config/settings/       - User settings"
echo ""
echo "To inspect: tar tzf $BACKUP_FILE"
