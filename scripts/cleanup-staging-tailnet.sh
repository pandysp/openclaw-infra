#!/usr/bin/env bash
#
# Delete stale openclaw-staging* devices from the Tailscale tailnet.
#
# The staging phoenix creates a throwaway VPS each run and destroys it, but
# unless the device joined with an ephemeral auth key its tailnet record
# lingers offline forever — and each leftover collides the hostname, pushing
# the next run to openclaw-staging-2, -3, … (suffixes the deploy tolerates but
# that clutter the admin console). staging-cleanup.yml prunes orphaned Hetzner
# resources but not these device records; this fills that gap.
#
# Safety: only removes devices whose hostname matches openclaw-staging(-N) AND
# are currently offline, so an in-flight run's box is never touched.
#
# Auth (either, both need the devices:core write scope):
#   - TAILSCALE_API_KEY=tskey-api-...                      (a direct API key), or
#   - TS_OAUTH_CLIENT_ID + TS_OAUTH_SECRET=tskey-client-... (an OAuth client,
#     exchanged here for a short-lived token — what CI already has).
#
# Usage:
#   TAILSCALE_API_KEY=tskey-api-... ./scripts/cleanup-staging-tailnet.sh [--dry-run]
#   TS_OAUTH_CLIENT_ID=... TS_OAUTH_SECRET=... ./scripts/cleanup-staging-tailnet.sh [--dry-run]

set -euo pipefail

TAILNET="${TAILNET:--}"   # '-' means "the tailnet that owns the credential"
DRY_RUN=false
[ "${1:-}" = "--dry-run" ] && DRY_RUN=true

API="https://api.tailscale.com/api/v2"

# Auth: a direct API key (tskey-api-…), or an OAuth client (id + secret) we
# exchange for a short-lived access token. CI already holds the OAuth client
# (TS_OAUTH_CLIENT_ID / TS_OAUTH_SECRET, used to join the tailnet), so no extra
# secret is needed there as long as that client has the devices:core scope.
TOKEN="${TAILSCALE_API_KEY:-}"
if [ -z "$TOKEN" ] && [ -n "${TS_OAUTH_CLIENT_ID:-}" ] && [ -n "${TS_OAUTH_SECRET:-}" ]; then
    TOKEN=$(curl -fsSL -X POST "${API}/oauth/token" \
        -d "client_id=${TS_OAUTH_CLIENT_ID}" \
        -d "client_secret=${TS_OAUTH_SECRET}" | jq -r '.access_token // empty') || true
fi
if [ -z "$TOKEN" ]; then
    echo "ERROR: provide TAILSCALE_API_KEY (tskey-api-… with devices write), or" >&2
    echo "       TS_OAUTH_CLIENT_ID + TS_OAUTH_SECRET for an OAuth client with that scope." >&2
    exit 1
fi
AUTH=(-H "Authorization: Bearer ${TOKEN}")

devices_json=$(curl -fsSL "${AUTH[@]}" "${API}/tailnet/${TAILNET}/devices") || {
    echo "ERROR: could not list devices (check credential scope/tailnet)" >&2
    exit 1
}

# Offline = lastSeen more than 5 minutes ago. Match openclaw-staging or -N.
# Guard .hostname against null (a single null-hostname device in the tailnet
# would otherwise make `test()` error and abort the whole filter — leaving
# targets empty and silently skipping every stale device). Strip any fractional
# seconds before fromdateiso8601, which only accepts %Y-%m-%dT%H:%M:%SZ.
mapfile -t targets < <(echo "$devices_json" | jq -r --arg now "$(date -u +%s)" '
    .devices[]
    | select((.hostname // "") | test("^openclaw-staging(-[0-9]+)?$"))
    | select(((($now | tonumber) - ((.lastSeen // "1970-01-01T00:00:00Z") | sub("\\.[0-9]+";"") | fromdateiso8601)) > 300))
    | "\(.id)\t\(.hostname)\t\(.lastSeen)"')

if [ "${#targets[@]}" -eq 0 ]; then
    echo "No stale openclaw-staging* devices to remove."
    exit 0
fi

echo "Stale openclaw-staging* devices (offline > 5m):"
printf '  %s\n' "${targets[@]}"

removed=0
for row in "${targets[@]}"; do
    id="${row%%$'\t'*}"
    rest="${row#*$'\t'}"
    name="${rest%%$'\t'*}"
    if [ "$DRY_RUN" = true ]; then
        echo "DRY-RUN: would delete $name ($id)"
        continue
    fi
    if curl -fsSL -X DELETE "${AUTH[@]}" "${API}/device/${id}" >/dev/null; then
        echo "Deleted $name ($id)"
        removed=$((removed + 1))
    else
        echo "WARNING: failed to delete $name ($id)" >&2
    fi
done

[ "$DRY_RUN" = true ] || echo "Removed ${removed}/${#targets[@]} stale device(s)."
