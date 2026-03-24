#!/usr/bin/env bash
# Smoke test for staging OpenClaw deployment.
# Tests device pairing, gateway health, and model connectivity.
#
# Required env vars:
#   STAGING_HOST   — Full Tailscale hostname (e.g., openclaw-staging.tail1234.ts.net)
#   GATEWAY_TOKEN  — OpenClaw gateway token for admin operations

set -euo pipefail

: "${STAGING_HOST:?STAGING_HOST is required}"
: "${GATEWAY_TOKEN:?GATEWAY_TOKEN is required}"

SSH_OPTS="-o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
ENV_PREFIX="XDG_RUNTIME_DIR=/run/user/1000"

pass() { echo "  ✓ $1"; }
fail() { echo "  ✗ $1"; }
info() { echo "  · $1"; }

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║               OpenClaw Staging Smoke Test                    ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "Host: $STAGING_HOST"
echo ""

# ── 1. Trigger device pairing ─────────────────────────────────────
echo "1. Triggering device pairing..."
# openclaw health on the VPS triggers a pending pairing request
# for the VPS CLI device. It may fail since it's not yet paired.
ssh $SSH_OPTS "ubuntu@$STAGING_HOST" "$ENV_PREFIX openclaw health" 2>&1 || true
sleep 5

# ── 2. Approve pending device ─────────────────────────────────────
echo "2. Approving pending device..."
DEVICES=$(ssh $SSH_OPTS "ubuntu@$STAGING_HOST" \
  "$ENV_PREFIX OPENCLAW_GATEWAY_TOKEN=$GATEWAY_TOKEN openclaw devices list" 2>&1) || true
echo "$DEVICES" | sed 's/^/   /'

# Extract pending request ID (first column of lines containing "pending")
REQUEST_ID=$(echo "$DEVICES" | grep -i "pending" | awk '{print $1}' | head -1 || true)

if [ -n "$REQUEST_ID" ] && [ "$REQUEST_ID" != "No" ]; then
  info "Approving request: $REQUEST_ID"
  ssh $SSH_OPTS "ubuntu@$STAGING_HOST" \
    "$ENV_PREFIX OPENCLAW_GATEWAY_TOKEN=$GATEWAY_TOKEN openclaw devices approve $REQUEST_ID" 2>&1 || {
    fail "Device approval failed (non-fatal)"
  }
  sleep 3
else
  info "No pending requests found (device may already be paired)"
fi

# ── 3. Health check ───────────────────────────────────────────────
echo "3. Checking gateway health (post-pairing)..."
HEALTH=$(ssh $SSH_OPTS "ubuntu@$STAGING_HOST" "$ENV_PREFIX openclaw health" 2>&1) || {
  fail "Health check failed"
  echo "$HEALTH" | sed 's/^/   /'
  exit 1
}
pass "Gateway health OK"
echo "$HEALTH" | head -5 | sed 's/^/   /'

# ── 4. Doctor (model connectivity) ────────────────────────────────
echo "4. Running openclaw doctor..."
run_doctor() {
  ssh $SSH_OPTS "ubuntu@$STAGING_HOST" "$ENV_PREFIX openclaw doctor" 2>&1
}

DOCTOR=$(run_doctor) || {
  info "Doctor failed on first attempt, retrying in 15s..."
  sleep 15
  DOCTOR=$(run_doctor) || {
    fail "Doctor failed after retry"
    echo "$DOCTOR" | sed 's/^/   /'
    exit 1
  }
}
pass "Doctor checks passed"
echo "$DOCTOR" | head -10 | sed 's/^/   /'

echo ""
echo "══════════════════════════════════════════════════════════════"
echo "                    Smoke Test Passed"
echo "══════════════════════════════════════════════════════════════"
