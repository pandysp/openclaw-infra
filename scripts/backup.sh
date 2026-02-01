#!/bin/bash
#
# OpenClaw Backup Script
#
# Creates a backup of OpenClaw data volumes and configuration.
# Run locally - connects to server via Tailscale.

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
    ssh "ubuntu@$FULL_HOSTNAME" "sudo systemctl stop openclaw"
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

# Backup docker volume data
echo "Backing up Docker volume..."
docker run --rm -v openclaw-data:/data -v "$BACKUP_TMP":/backup alpine \
    tar czf /backup/volume-data.tar.gz -C /data .

# Backup configuration (without secrets)
echo "Backing up configuration..."
sudo cp /opt/openclaw/docker-compose.yml "$BACKUP_TMP/"

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
    ssh "ubuntu@$FULL_HOSTNAME" "sudo systemctl start openclaw"
fi

echo ""
echo -e "${GREEN}✓ Backup complete: $BACKUP_FILE${NC}"
echo ""
echo "To restore, use:"
echo "  tar xzf $BACKUP_FILE"
echo "  # Then copy files to server and restore docker volume"
