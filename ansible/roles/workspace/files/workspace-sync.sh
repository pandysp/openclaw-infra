#!/bin/bash
# Runs only inside the isolated sync container in production.
set -euo pipefail
: "${WORKSPACE_AGENT_ID:?Missing agent ID}"
: "${WORKSPACE_REPOSITORY:?Missing repository}"
cd "${WORKSPACE_PATH:-/workspace}"
export GIT_TERMINAL_PROMPT=0 GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.excludesFile
export GIT_CONFIG_VALUE_0="${WORKSPACE_EXCLUDES:-/etc/workspace-sync/excludes}"

has_head() {
    local status
    if git rev-parse --verify --quiet HEAD >/dev/null; then return 0; else status=$?; fi
    if [ "$status" -eq 1 ]; then return 1; fi
    exit "$status"
}

if [ ! -e .git ]; then
    if [ "${WORKSPACE_INITIALIZE:-0}" != 1 ]; then
        echo "ERROR: Missing repository. Initialization requires explicit provisioning." >&2
        exit 1
    fi
    git clone "$WORKSPACE_REPOSITORY" .
fi

CURRENT_BRANCH=$(git symbolic-ref --short HEAD)
if [ "$CURRENT_BRANCH" != main ]; then
    echo "ERROR: Workspace is not on main. Refusing to rename an active branch." >&2
    exit 1
fi

git config user.name "OpenClaw Agent (${WORKSPACE_AGENT_ID})"
git config user.email "openclaw-${WORKSPACE_AGENT_ID}@localhost"
git config remote.origin.url "$WORKSPACE_REPOSITORY"
git config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'
git fetch --prune origin
REMOTE_EXISTS=false
if git show-ref --verify --quiet refs/remotes/origin/main; then
    REMOTE_EXISTS=true
else
    RC=$?
    if [ "$RC" -ne 1 ]; then exit "$RC"; fi
fi

if [ "$REMOTE_EXISTS" = true ] && ! has_head; then
    echo "ERROR: Unborn repository has an existing remote history. Refusing restoration." >&2
    exit 1
fi

# Check enumeration before consuming NUL-delimited paths; process substitution
# would hide a failed Git command and allow a partial backup to continue.
IGNORED=$(mktemp)
trap 'rm -f "$IGNORED"' EXIT
git ls-files -ci --exclude-standard -z > "$IGNORED"
while IFS= read -r -d '' path; do
    git rm --cached -- "$path"
done < "$IGNORED"

git add -A
if git diff --cached --quiet; then
    :
else
    RC=$?
    if [ "$RC" -ne 1 ]; then exit "$RC"; fi
    git commit -m "Auto-sync: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
fi
if [ "$REMOTE_EXISTS" = true ]; then
    if ! git merge --no-edit origin/main; then
        if git rev-parse --verify MERGE_HEAD >/dev/null 2>&1; then git merge --abort; fi
        echo "ERROR: Workspace merge failed. Local commits are preserved; resolve before syncing." >&2
        exit 1
    fi
fi
if has_head; then git push -u origin main; fi
