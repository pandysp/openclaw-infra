#!/usr/bin/env bash
# Check that the pinned OpenClaw version matches the reviewed heartbeat and cron
# contract and that the roles still honour it. The contract, as of openclaw_docs_reviewed_version:
# - Upstream runs a 30-minute heartbeat when cadence is omitted; IaC sets the
#   default to 0m and each agent opts in explicitly.
# - Heartbeats live in agents.entries.<id>.heartbeat; once any entry has a
#   heartbeat block, only entries with a block run.
# - Each heartbeat runs as a system-owned automation ("Heartbeat (<id>)");
#   its standing instructions live in that job's scratch. HEARTBEAT.md has no
#   effect.
# - `cron` is an alias of `automations`. `cron list` omits disabled jobs; use
#   `cron list --all`. `cron edit --enable/--disable` keeps IDs and history;
#   missing jobs are created with --disabled. Never edit scheduler storage directly.
# After reviewing a new version, bump openclaw_docs_reviewed_version in
# ansible/group_vars/all.yml.
# --pinned-docs also checks the pinned version's docs (every PR, see lint.yml).
# --latest-docs checks the pinned docs and OpenClaw main's docs. Upstream main
# moves on its own, so this runs weekly (upstream-contract.yml), not per PR: a
# failure means "adapt the contract before the next upgrade", not "this PR broke".
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ALL_VARS="$REPO_DIR/ansible/group_vars/all.yml"
AGENTS_TASKS="$REPO_DIR/ansible/roles/agents/tasks/main.yml"
HEARTBEAT_TASKS="$REPO_DIR/ansible/roles/agents/tasks/heartbeat.yml"
CONFIG_TASKS="$REPO_DIR/ansible/roles/config/tasks/main.yml"
CRON_TASKS="$REPO_DIR/ansible/roles/telegram/tasks/cron.yml"
CRON_RECONCILER="$REPO_DIR/ansible/roles/telegram/files/reconcile_openclaw_cron.py"

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

DOCS_REFS=()
case "${1:-}" in
    "") ;;
    --pinned-docs) DOCS_REFS=(pinned) ;;
    --latest-docs) DOCS_REFS=(pinned latest) ;;
    *) fail "usage: $0 [--pinned-docs | --latest-docs]" ;;
esac

IAC_VERSION=$(sed -nE 's/^openclaw_version: "([^"]+)"/\1/p' "$ALL_VARS")
REVIEWED_VERSION=$(sed -nE 's/^openclaw_docs_reviewed_version: "([^"]+)"/\1/p' "$ALL_VARS")

[ -n "$IAC_VERSION" ] || fail "openclaw_version is missing"
[ "$REVIEWED_VERSION" = "$IAC_VERSION" ] || fail "IaC pins $IAC_VERSION but contract review pins ${REVIEWED_VERSION:-missing}"

grep -q 'agents.defaults.heartbeat.*0m\|every.*0m' "$AGENTS_TASKS" || fail "agents role does not enforce the 0m heartbeat default"
grep -q 'agents.entries.{{ _heartbeat_agent.id }}.heartbeat' "$HEARTBEAT_TASKS" || fail "agents role does not write per-agent heartbeats under agents.entries"
if grep -q 'agents.defaults.heartbeat' "$CONFIG_TASKS"; then
    fail "config role still writes heartbeat state; agents must be the sole owner"
fi
grep -q 'cron list --all --json' "$CRON_TASKS" || fail "cron role does not list disabled jobs"
grep -q '"--disabled"' "$CRON_RECONCILER" || fail "cron reconciler cannot create disabled jobs"
grep -q '"--enable"' "$CRON_RECONCILER" || fail "cron reconciler cannot enable jobs in place"
grep -q '"--disable"' "$CRON_RECONCILER" || fail "cron reconciler cannot disable jobs in place"

if [ "${#DOCS_REFS[@]}" -gt 0 ]; then
    DOCS_TMP=$(mktemp -d)
    trap 'rm -rf "$DOCS_TMP"' EXIT
    for docs in "${DOCS_REFS[@]}"; do
        if [ "$docs" = pinned ]; then base="https://raw.githubusercontent.com/openclaw/openclaw/v${IAC_VERSION}/docs"
        else base="https://raw.githubusercontent.com/openclaw/openclaw/main/docs"; fi
        curl -fsSL --retry 3 "$base/gateway/heartbeat.md" -o "$DOCS_TMP/$docs-heartbeat.md" \
            || fail "could not fetch $docs heartbeat docs"
        curl -fsSL --retry 3 "$base/automation/cron-jobs.md" -o "$DOCS_TMP/$docs-cron.md" \
            || fail "could not fetch $docs cron docs"
    done

    for docs in "${DOCS_REFS[@]}"; do
        grep -Fq 'agents.entries.*.heartbeat' "$DOCS_TMP/$docs-heartbeat.md" \
            || fail "$docs heartbeat docs no longer describe agents.entries.*.heartbeat; review the contract"
        grep -Fq '**only those agents** run heartbeats' "$DOCS_TMP/$docs-heartbeat.md" \
            || fail "$docs heartbeat docs no longer state the per-agent heartbeat allowlist; review the contract"
        grep -Fq 'heartbeat monitor scratch' "$DOCS_TMP/$docs-heartbeat.md" \
            || fail "$docs heartbeat docs no longer keep instructions in the monitor scratch; review the contract"
        # shellcheck disable=SC2016  # literal Markdown backticks, not a command
        grep -Fq '`openclaw cron` remains an alias' "$DOCS_TMP/$docs-cron.md" \
            || fail "$docs scheduler docs no longer keep the cron alias; review the contract"
    done
    echo "Docs match the reviewed contract: ${DOCS_REFS[*]}"
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
