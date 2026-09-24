#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ALL_VARS="$REPO_DIR/ansible/group_vars/all.yml"
DOCS_REVIEW="$REPO_DIR/docs/DOCS-REVIEW.md"
AGENTS_TASKS="$REPO_DIR/ansible/roles/agents/tasks/main.yml"
CONFIG_TASKS="$REPO_DIR/ansible/roles/config/tasks/main.yml"
CRON_TASKS="$REPO_DIR/ansible/roles/telegram/tasks/cron.yml"
CRON_RECONCILER="$REPO_DIR/ansible/roles/telegram/files/reconcile_openclaw_cron.py"

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

CHECK_LATEST_DOCS=false
if [ "${1:-}" = "--latest-docs" ]; then
    CHECK_LATEST_DOCS=true
elif [ "$#" -gt 0 ]; then
    fail "usage: $0 [--latest-docs]"
fi

IAC_VERSION=$(sed -nE 's/^openclaw_version: "([^"]+)"/\1/p' "$ALL_VARS")
REVIEWED_VERSION=$(sed -nE 's/^openclaw_docs_reviewed_version: "([^"]+)"/\1/p' "$ALL_VARS")
DOCS_VERSION=$(sed -nE 's/^- Reviewed OpenClaw version: `([^`]+)`.*/\1/p' "$DOCS_REVIEW")

[ -n "$IAC_VERSION" ] || fail "openclaw_version is missing"
[ "$REVIEWED_VERSION" = "$IAC_VERSION" ] || fail "IaC pins $IAC_VERSION but contract review pins ${REVIEWED_VERSION:-missing}"
[ "$DOCS_VERSION" = "$IAC_VERSION" ] || fail "DOCS-REVIEW.md covers ${DOCS_VERSION:-missing}, expected $IAC_VERSION"

grep -q 'agents.defaults.heartbeat.*0m\|every.*0m' "$AGENTS_TASKS" || fail "agents role does not enforce the 0m heartbeat default"
if grep -q 'agents.defaults.heartbeat' "$CONFIG_TASKS"; then
    fail "config role still writes heartbeat state; agents must be the sole owner"
fi
grep -q 'cron list --all --json' "$CRON_TASKS" || fail "cron role does not list disabled jobs"
grep -q '"--disabled"' "$CRON_RECONCILER" || fail "cron reconciler cannot create disabled jobs"
grep -q '"--enable"' "$CRON_RECONCILER" || fail "cron reconciler cannot enable jobs in place"
grep -q '"--disable"' "$CRON_RECONCILER" || fail "cron reconciler cannot disable jobs in place"

if [ "$CHECK_LATEST_DOCS" = true ]; then
    DOCS_TMP=$(mktemp -d)
    trap 'rm -rf "$DOCS_TMP"' EXIT
    PINNED_BASE="https://raw.githubusercontent.com/openclaw/openclaw/v${IAC_VERSION}/docs"
    LATEST_BASE="https://raw.githubusercontent.com/openclaw/openclaw/main/docs"
    curl -fsSL --retry 3 "$PINNED_BASE/gateway/heartbeat.md" -o "$DOCS_TMP/pinned-heartbeat.md" \
        || fail "could not fetch pinned heartbeat docs for v$IAC_VERSION"
    curl -fsSL --retry 3 "$PINNED_BASE/automation/cron-jobs.md" -o "$DOCS_TMP/pinned-cron.md" \
        || fail "could not fetch pinned cron docs for v$IAC_VERSION"
    curl -fsSL --retry 3 "$LATEST_BASE/gateway/heartbeat.md" -o "$DOCS_TMP/latest-heartbeat.md" \
        || fail "could not fetch latest heartbeat docs"
    curl -fsSL --retry 3 "$LATEST_BASE/automation/cron-jobs.md" -o "$DOCS_TMP/latest-cron.md" \
        || fail "could not fetch latest cron docs"

    grep -Fq 'agents.list[]' "$DOCS_TMP/pinned-heartbeat.md" \
        || fail "pinned heartbeat docs no longer describe agents.list[]; re-review the v$IAC_VERSION contract"
    grep -Fq 'openclaw cron list' "$DOCS_TMP/pinned-cron.md" \
        || fail "pinned cron docs no longer describe the v$IAC_VERSION cron CLI"
    grep -Fq 'agents.entries.*.heartbeat' "$DOCS_TMP/latest-heartbeat.md" \
        || fail "latest heartbeat docs drifted beyond the known agents.entries contract; review required"
    grep -Fq 'openclaw automations list' "$DOCS_TMP/latest-cron.md" \
        || fail "latest scheduler docs drifted beyond the known automations CLI; review required"
    echo "Latest docs drift is still the reviewed agents.entries + automations contract"
fi

if [ -n "${OPENCLAW_CONTRACT_HOST:-}" ]; then
    REMOTE_VERSION=$(ssh -o ConnectTimeout=10 "$OPENCLAW_CONTRACT_HOST" 'openclaw --version' \
        | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
    [ "$REMOTE_VERSION" = "$IAC_VERSION" ] || fail "remote has $REMOTE_VERSION, expected $IAC_VERSION"
    ssh -o ConnectTimeout=10 "$OPENCLAW_CONTRACT_HOST" '
        set -e
        openclaw cron list --help | grep -q -- --all
        openclaw cron add --help | grep -q -- --disabled
        openclaw cron edit --help | grep -q -- --enable
        openclaw cron edit --help | grep -q -- --disable
    ' || fail "remote OpenClaw CLI is missing required scheduler flags"
fi

echo "OpenClaw scheduler contract OK: $IAC_VERSION"
