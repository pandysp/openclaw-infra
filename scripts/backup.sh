#!/bin/bash
#
# OpenClaw Backup Script
#
# Creates a backup of OpenClaw configuration and workspace data.
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
BACKUP_TMP="/tmp/openclaw-backup-$(date +%s)"
mkdir -p "$BACKUP_TMP"

# Backup OpenClaw configuration
echo "Backing up configuration..."
if [ -d ~/.openclaw ]; then
    cp -r ~/.openclaw "$BACKUP_TMP/openclaw-config"
    # Remove sensitive tokens from backup copy
    if [ -f "$BACKUP_TMP/openclaw-config/openclaw.json" ]; then
        python3 -c "
import json, sys
with open('$BACKUP_TMP/openclaw-config/openclaw.json') as f:
    c = json.load(f)
# Redact auth tokens
if 'gateway' in c and 'auth' in c['gateway']:
    c['gateway']['auth']['token'] = '<REDACTED>'
if 'gateway' in c and 'remote' in c['gateway']:
    c['gateway']['remote']['token'] = '<REDACTED>'
with open('$BACKUP_TMP/openclaw-config/openclaw.json', 'w') as f:
    json.dump(c, f, indent=2)
"
    fi
fi

# Backup workspace data (memory, projects, notes)
echo "Backing up workspace..."
if [ -d ~/.openclaw/workspace ]; then
    cp -r ~/.openclaw/workspace "$BACKUP_TMP/workspace"
fi

# Backup cron configuration
echo "Backing up cron jobs..."
openclaw cron list > "$BACKUP_TMP/cron-jobs.txt" 2>/dev/null || true

# Backup systemd service file
echo "Backing up service configuration..."
cp ~/.config/systemd/user/openclaw-gateway.service "$BACKUP_TMP/" 2>/dev/null || true

# Create archive
echo "Creating archive..."
tar czf /tmp/openclaw-backup.tar.gz -C "$BACKUP_TMP" .

# Cleanup
rm -rf "$BACKUP_TMP"

echo "Backup created at /tmp/openclaw-backup.tar.gz"
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
echo ""
echo "Contents:"
echo "  openclaw-config/  - OpenClaw configuration (tokens redacted)"
echo "  workspace/        - Workspace data (memory, projects)"
echo "  cron-jobs.txt     - Scheduled task listing"
echo ""
echo "To inspect: tar tzf $BACKUP_FILE"
