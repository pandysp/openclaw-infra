#!/usr/bin/env bash
set -euo pipefail

# Get Telegram user IDs and group IDs by briefly pausing the gateway
# and polling the Telegram Bot API for new messages.
#
# Usage:
#   ./scripts/get-telegram-id.sh                    # Poll and display IDs
#   ./scripts/get-telegram-id.sh --set-config KEY   # Also set Pulumi config

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PULUMI_DIR="$SCRIPT_DIR/../pulumi"

PULUMI_KEY=""
if [ "${1:-}" = "--set-config" ] && [ -n "${2:-}" ]; then
    PULUMI_KEY="$2"
fi

# Read bot token
cd "$PULUMI_DIR"
BOT_TOKEN=$(pulumi config get telegramBotToken 2>/dev/null) || {
    echo "ERROR: telegramBotToken not set in Pulumi config."
    exit 1
}

# Stop the gateway (it consumes all updates via polling)
echo "Stopping gateway to free up Telegram polling..."
ssh ubuntu@openclaw-vps 'XDG_RUNTIME_DIR=/run/user/1000 systemctl --user stop openclaw-gateway' || {
    echo "ERROR: Could not stop gateway. Is the server reachable?"
    exit 1
}

# Ensure gateway restarts even if we fail
cleanup() {
    echo ""
    echo "Restarting gateway..."
    ssh ubuntu@openclaw-vps 'XDG_RUNTIME_DIR=/run/user/1000 systemctl --user start openclaw-gateway' && \
        echo "Gateway restarted." || \
        echo "WARNING: Failed to restart gateway. Run manually: ssh ubuntu@openclaw-vps 'XDG_RUNTIME_DIR=/run/user/1000 systemctl --user start openclaw-gateway'"
}
trap cleanup EXIT

echo ""
echo "============================================"
echo "  Send a message in the Telegram chat now."
echo "  (DM the bot, or post in the group)"
echo "  Waiting up to 60 seconds..."
echo "============================================"
echo ""

# Long-poll for updates
RESPONSE=$(curl -s "https://api.telegram.org/bot${BOT_TOKEN}/getUpdates?timeout=60") || {
    echo "ERROR: Failed to reach Telegram API."
    exit 1
}

# Parse results
RESULTS=$(echo "$RESPONSE" | jq -r '[.result[] | {
    chat_id: (.message // .my_chat_member // .channel_post).chat.id,
    chat_title: (.message // .my_chat_member // .channel_post).chat.title,
    chat_type: (.message // .my_chat_member // .channel_post).chat.type,
    from_id: (.message.from.id // null),
    from_name: ((.message.from.first_name // "") + " " + (.message.from.last_name // "") | gsub("^ +| +$"; "")),
    from_username: (.message.from.username // null)
}] | unique_by(.chat_id)')

COUNT=$(echo "$RESULTS" | jq 'length')

if [ "$COUNT" -eq 0 ]; then
    echo "No messages received. Try again and make sure the bot is a member of the chat."
    exit 1
fi

echo "Found $COUNT chat(s):"
echo ""
echo "$RESULTS" | jq -r '.[] | "  Chat ID:   \(.chat_id)\n  Title:     \(.chat_title // "N/A (DM)")\n  Type:      \(.chat_type)\n  From:      \(.from_name) (@\(.from_username // "N/A")) [user ID: \(.from_id // "N/A")]\n"'

# If --set-config, pick the ID and set it
if [ -n "$PULUMI_KEY" ]; then
    if [ "$COUNT" -eq 1 ]; then
        CHAT_ID=$(echo "$RESULTS" | jq -r '.[0].chat_id')
        CHAT_TYPE=$(echo "$RESULTS" | jq -r '.[0].chat_type')
    else
        echo "Multiple chats found. Enter the chat ID to use:"
        read -r CHAT_ID
        CHAT_TYPE="unknown"
    fi

    echo "Setting pulumi config: $PULUMI_KEY = $CHAT_ID"
    cd "$PULUMI_DIR"
    pulumi config set "$PULUMI_KEY" -- "$CHAT_ID"
    echo "Done."
fi
