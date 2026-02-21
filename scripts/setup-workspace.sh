#!/usr/bin/env bash
set -euo pipefail

# Set up workspace git sync for an agent: create the GitHub repo,
# add the deploy key, and set the Pulumi config — all in one step.
#
# Usage:
#   ./scripts/setup-workspace.sh henning          # Create for agent "henning"
#   ./scripts/setup-workspace.sh henning --org myorg  # Use a GitHub org instead of personal account
#
# Prerequisites:
#   - gh CLI authenticated (`gh auth login`)
#   - Pulumi stack with deploy key already generated (`pulumi up` at least once)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PULUMI_DIR="$SCRIPT_DIR/../pulumi"

if [ -z "${1:-}" ]; then
    echo "Usage: $0 <agent-id> [--org <github-org>]"
    echo ""
    echo "Examples:"
    echo "  $0 henning           # pandysp/openclaw-workspace-henning"
    echo "  $0 main              # pandysp/openclaw-workspace"
    echo "  $0 ph --org myorg    # myorg/openclaw-workspace-ph"
    exit 1
fi

AGENT_ID="$1"
shift

# Parse optional --org flag
GH_OWNER=""
while [ $# -gt 0 ]; do
    case "$1" in
        --org) GH_OWNER="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Derive repo name from agent ID
if [ "$AGENT_ID" = "main" ]; then
    REPO_NAME="openclaw-workspace"
else
    REPO_NAME="openclaw-workspace-${AGENT_ID}"
fi

# Derive Pulumi export/config names from agent ID
if [ "$AGENT_ID" = "main" ]; then
    DEPLOY_KEY_EXPORT="workspaceDeployPublicKey"
    CONFIG_KEY="workspaceRepoUrl"
else
    # Convert agent-id to PascalCase for Pulumi naming convention
    # e.g., "tl" -> "Tl", "henning" -> "Henning"
    PASCAL_ID="$(echo "$AGENT_ID" | awk '{print toupper(substr($0,1,1)) substr($0,2)}')"
    DEPLOY_KEY_EXPORT="workspace${PASCAL_ID}DeployPublicKey"
    CONFIG_KEY="workspace${PASCAL_ID}RepoUrl"
fi

# Determine repo owner (default: authenticated gh user)
if [ -z "$GH_OWNER" ]; then
    GH_OWNER=$(gh api user --jq '.login' 2>/dev/null) || {
        echo "ERROR: Could not determine GitHub username. Is 'gh' authenticated?"
        exit 1
    }
fi

FULL_REPO="${GH_OWNER}/${REPO_NAME}"
REPO_SSH_URL="git@github.com:${FULL_REPO}.git"

echo "=== Workspace setup for agent '${AGENT_ID}' ==="
echo "  Repo:       ${FULL_REPO}"
echo "  SSH URL:    ${REPO_SSH_URL}"
echo "  Deploy key: pulumi stack output ${DEPLOY_KEY_EXPORT}"
echo "  Config key: ${CONFIG_KEY}"
echo ""

# Step 1: Check if repo already exists
if gh repo view "$FULL_REPO" &>/dev/null; then
    echo "Repo ${FULL_REPO} already exists — skipping creation."
else
    echo "Creating private repo ${FULL_REPO}..."
    gh repo create "$FULL_REPO" --private --description "OpenClaw workspace for agent ${AGENT_ID}" || {
        echo "ERROR: Failed to create repo."
        exit 1
    }
    echo "Created."
fi

# Step 2: Get deploy key from Pulumi
echo "Reading deploy key from Pulumi..."
cd "$PULUMI_DIR"
DEPLOY_PUBLIC_KEY=$(pulumi stack output "$DEPLOY_KEY_EXPORT" 2>/dev/null) || {
    echo "ERROR: Could not read ${DEPLOY_KEY_EXPORT} from Pulumi."
    echo "Have you run 'pulumi up' at least once to generate the deploy keys?"
    exit 1
}

if [ -z "$DEPLOY_PUBLIC_KEY" ]; then
    echo "ERROR: Deploy key is empty."
    exit 1
fi

# Step 3: Add deploy key to repo (skip if already present)
EXISTING_KEYS=$(gh repo deploy-key list --repo "$FULL_REPO" 2>/dev/null || echo "")
KEY_TITLE="OpenClaw VPS (${AGENT_ID})"

if echo "$EXISTING_KEYS" | grep -q "$KEY_TITLE"; then
    echo "Deploy key '${KEY_TITLE}' already exists on repo — skipping."
else
    echo "Adding deploy key to ${FULL_REPO}..."
    echo "$DEPLOY_PUBLIC_KEY" | gh repo deploy-key add --repo "$FULL_REPO" --title "$KEY_TITLE" -w - || {
        echo "ERROR: Failed to add deploy key."
        exit 1
    }
    echo "Deploy key added."
fi

# Step 4: Set Pulumi config
echo "Setting pulumi config: ${CONFIG_KEY} = ${REPO_SSH_URL}"
pulumi config set "$CONFIG_KEY" "$REPO_SSH_URL"

echo ""
echo "=== Done ==="
echo "Run './scripts/provision.sh --tags workspace' to deploy, or 'pulumi up' for a full deploy."
