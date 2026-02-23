# Investigation: Data-Driven Agent Variables

**Story:** US-016
**Date:** 2026-02-23
**Status:** Conditional Go (with Pulumi alias migration)

## Current Duplication

### index.ts — Per-Agent Variable Locations

| Category | Lines | Count | Per-agent items |
|----------|-------|-------|-----------------|
| Config reads: GitHub tokens | 24-28 | 5 | `githubToken`, `githubTokenManon`, `githubTokenTl`, `githubTokenHenning`, `githubTokenPh` |
| Config reads: Telegram IDs | 31-34 | 4 | `telegramManonUserId`, `telegramGroupId`, `telegramHenningUserId`, `telegramPhGroupId` |
| Config reads: Workspace repo URLs | 37-41 | 5 | `workspaceRepoUrl`, `workspaceManonRepoUrl`, ..., `workspacePhRepoUrl` |
| Config reads: Obsidian vault URLs | 44-46 | 3 | `obsidianAndyVaultRepoUrl`, `obsidianManonVaultRepoUrl`, `obsidianTlVaultRepoUrl` |
| Deploy key resources | 75-91 | 5 | `workspace-deploy-key`, `workspace-deploy-key-manon`, ..., `workspace-deploy-key-ph` |
| Ansible env vars: workspace | 133-152 | 10 | `PROVISION_WORKSPACE_*_REPO_URL` + `PROVISION_WORKSPACE_*_DEPLOY_KEY` |
| Ansible env vars: telegram | 136-145 | 4 | `PROVISION_TELEGRAM_*_USER_ID`, `PROVISION_TELEGRAM_*_GROUP_ID` |
| Ansible env vars: github | 155-159 | 5 | `PROVISION_GITHUB_TOKEN_*` |
| Ansible env vars: obsidian | 160-165 | 3 | `PROVISION_OBSIDIAN_*_VAULT_REPO_URL` |
| Exports: deploy key pairs | 194-225 | 10 | Public + private key exports per agent |

**Total per-agent locations in index.ts: ~54 lines** across 6 categories. Adding a new agent requires touching all 6.

### provision.sh — Per-Agent Variable Locations

| Category | Lines | Count | Notes |
|----------|-------|-------|-------|
| PROVISION_* env var reads (env path) | 34-57 | 22 | Reads PROVISION_* into local vars |
| Pulumi CLI fallback reads | 69-94 | 22 | Mirror of env path for day-2 manual runs |
| Deploy key validation calls | 125-129 | 5 | `validate_deploy_key` per agent |
| Status echo lines | 131-151 | 20 | Human-readable config summary |
| Python YAML keys list | 194-204 | 17 | Keys written to secrets YAML |
| Deploy key append calls | 223-227 | 5 | `append_deploy_key` per agent |

**Total per-agent locations in provision.sh: ~91 lines** across 6 categories. Adding a new agent requires touching all 6.

### Combined Impact

**Adding one new agent today requires editing ~145 lines across 12 locations in 2 files.** This is error-prone (easy to miss a location) and review-heavy (large diffs for a simple config change).

## Proposed Data Structure

### index.ts: Agent Registry Array

```typescript
// Agent registry — single source of truth for per-agent resources
const agentIds = ["main", "manon", "tl", "henning", "ph"] as const;

// Helper: convert agent ID to camelCase config key suffix
// "main" → "" (no suffix), "manon" → "Manon", "tl" → "Tl"
function configSuffix(id: string): string {
  if (id === "main") return "";
  return id.charAt(0).toUpperCase() + id.slice(1);
}

// Per-agent config reads (loop)
const agentConfigs = Object.fromEntries(
  agentIds.map(id => {
    const s = configSuffix(id);
    return [id, {
      githubToken: config.getSecret(`githubToken${s}`),
      workspaceRepoUrl: config.get(`workspace${s}RepoUrl`),
      // Telegram: varies by type (userId vs groupId) — see Risks
    }];
  })
);

// Per-agent deploy keys (loop)
const deployKeys = Object.fromEntries(
  agentIds.map(id => [
    id,
    new tls.PrivateKey(`workspace-deploy-key${id === "main" ? "" : `-${id}`}`, {
      algorithm: "ED25519",
    }),
  ])
);

// Per-agent Ansible env vars (loop)
const agentEnvVars = Object.fromEntries(
  agentIds.flatMap(id => {
    const S = id === "main" ? "" : `_${id.toUpperCase()}`;
    return [
      [`PROVISION_WORKSPACE${S}_REPO_URL`, agentConfigs[id].workspaceRepoUrl || ""],
      [`PROVISION_WORKSPACE${S}_DEPLOY_KEY`, deployKeys[id].privateKeyOpenssh],
      [`PROVISION_GITHUB_TOKEN${S}`, agentConfigs[id].githubToken || ""],
    ];
  })
);

// Per-agent exports (loop)
agentIds.forEach(id => {
  const prefix = id === "main" ? "workspace" : `workspace${configSuffix(id)}`;
  // Pulumi exports must be module-level — see Risks
});
```

### provision.sh: Agent Array Loop

```bash
# Agent IDs — single source of truth
AGENTS=(main manon tl henning ph)

# Per-agent config reads (env var path)
declare -A workspace_repo_urls workspace_deploy_keys github_tokens
for agent in "${AGENTS[@]}"; do
  suffix=$(echo "$agent" | tr '[:lower:]' '[:upper:]')
  if [ "$agent" = "main" ]; then suffix=""; fi
  workspace_repo_urls[$agent]="${!PROVISION_WORKSPACE${suffix:+_${suffix}}_REPO_URL:-}"
  workspace_deploy_keys[$agent]="${!PROVISION_WORKSPACE${suffix:+_${suffix}}_DEPLOY_KEY:-}"
  github_tokens[$agent]="${!PROVISION_GITHUB_TOKEN${suffix:+_${suffix}}:-}"
done

# Deploy key validation (loop)
for agent in "${AGENTS[@]}"; do
  validate_deploy_key "workspace ($agent)" \
    "${workspace_repo_urls[$agent]}" "${workspace_deploy_keys[$agent]}"
done

# Status echoes (loop)
for agent in "${AGENTS[@]}"; do
  echo "  workspace_sync ($agent): $([ -n "${workspace_repo_urls[$agent]}" ] && echo configured || echo skipped)"
  echo "  github_mcp ($agent): $([ -n "${github_tokens[$agent]}" ] && echo configured || echo skipped)"
done

# Python YAML generation (loop — pass agent list, generate keys dynamically)
# Deploy key appends (loop)
```

## Risks

### Critical Risk: Pulumi Resource URN Changes

**This is the primary concern.** Pulumi identifies resources by their URN, which includes the resource's logical name. The current deploy keys have these logical names:

| Agent | Current Logical Name |
|-------|---------------------|
| main | `workspace-deploy-key` |
| manon | `workspace-deploy-key-manon` |
| tl | `workspace-deploy-key-tl` |
| henning | `workspace-deploy-key-henning` |
| ph | `workspace-deploy-key-ph` |

**If the refactored loop generates the same logical names, no resource replacement occurs.** This is achievable:

```typescript
const deployKeys = Object.fromEntries(
  agentIds.map(id => [
    id,
    new tls.PrivateKey(
      // MUST match existing logical name exactly
      `workspace-deploy-key${id === "main" ? "" : `-${id}`}`,
      { algorithm: "ED25519" }
    ),
  ])
);
```

The logical name `workspace-deploy-key-manon` is constructed from `"workspace-deploy-key" + "-" + "manon"` — exactly matching the existing resource. **No resource replacement.**

**However**, if someone later changes the naming convention (e.g., standardizes to `workspace-deploy-key-main` for the main agent), that WOULD trigger replacement — destroying and regenerating the deploy key. The old public key in GitHub would become invalid.

**Mitigation:** Pulumi `aliases` can map old URNs to new ones, preventing replacement:

```typescript
new tls.PrivateKey("workspace-deploy-key-main", {
  algorithm: "ED25519",
}, {
  aliases: [{ name: "workspace-deploy-key" }],
});
```

**Verification step:** After refactoring, run `pulumi preview` and confirm output shows `0 to destroy`. If any deploy key shows "replace", the logical name doesn't match.

### Medium Risk: Telegram Config Irregularity

Telegram config is not uniform across agents:

| Agent | Telegram config key | Type |
|-------|-------------------|------|
| main | `telegramUserId` | User ID (DM) |
| manon | `telegramManonUserId` | User ID (DM) |
| tl | (none) | — |
| henning | `telegramHenningUserId` | User ID (DM) |
| ph | `telegramPhGroupId` | **Group ID** (not user ID) |
| (shared) | `telegramGroupId` | Group ID |

The `ph` agent uses a **group ID** while others use **user IDs**. A simple loop with `telegramUserId` per agent wouldn't capture this. Options:

1. **Generic `telegramId` field**: Each agent has a `telegramId` that could be either user or group. Ansible already handles the distinction via binding config.
2. **Keep Telegram config separate**: Don't include Telegram in the loop. It has enough irregularity (group vs. user, shared group ID) that a loop adds complexity without saving much.

**Recommendation:** Keep Telegram config outside the agent loop. It's only 4 variables and the semantic differences make a generic loop misleading.

### Medium Risk: Obsidian Vault Irregularity

Obsidian vault config doesn't map 1:1 to agents:

| Config key | Agent |
|-----------|-------|
| `obsidianAndyVaultRepoUrl` | main (Andy's vault) |
| `obsidianManonVaultRepoUrl` | manon |
| `obsidianTlVaultRepoUrl` | tl |

Only 3 of 5 agents have Obsidian vaults. The key uses "Andy" (not "main"). A loop would need an optional `obsidianVaultKey` per agent, or Obsidian should stay outside the loop.

**Recommendation:** Keep Obsidian config outside the agent loop. It's only 3 variables and the naming ("Andy" vs "main") makes a generic loop awkward.

### Low Risk: Bash Associative Arrays

The provision.sh refactor would use Bash associative arrays (`declare -A`). These require Bash 4+ (available on macOS via Homebrew's bash, and on Ubuntu). The script already uses `#!/usr/bin/env bash` and `set -euo pipefail`, and runs on macOS (where `/bin/bash` is 3.x but Homebrew provides 5.x).

**Mitigation:** Add a Bash version check at the top of the script, or use indexed arrays with naming conventions instead of associative arrays.

### Low Risk: Pulumi Export Naming

Pulumi exports are module-level `export const` statements. In a loop:

```typescript
// Can't do this — TypeScript doesn't support dynamic exports
agentIds.forEach(id => {
  export const [`${prefix}DeployPublicKey`] = ...;
});
```

**Workaround:** Export a single object containing all deploy key data, or use `pulumi.output()` with `pulumi.secret()`:

```typescript
// Option A: Keep individual exports (generated by loop, assigned to module-level vars)
// Requires declaring all export vars at module level first — less DRY but preserves API

// Option B: Single object export
export const workspaceDeployKeys = pulumi.output(
  Object.fromEntries(
    agentIds.map(id => [id, {
      publicKey: deployKeys[id].publicKeyOpenssh,
      privateKey: pulumi.secret(deployKeys[id].privateKeyOpenssh),
    }])
  )
);
```

**Option B changes the Pulumi stack output API**, which would break `setup-workspace.sh` and any scripts reading `pulumi stack output workspaceDeployPublicKey`. Scripts would need updating to `pulumi stack output workspaceDeployKeys --json | jq -r '.main.publicKey'`.

**Recommendation:** Use Option A (keep individual exports) for backward compatibility. The exports section remains verbose but is generated mechanically.

## What the Refactor Would Cover

Based on risk analysis, the refactor should loop over these per-agent resources:

| Category | Loopable? | Savings |
|----------|-----------|---------|
| GitHub tokens (config reads) | Yes | 5 → 1 loop |
| Workspace repo URLs (config reads) | Yes | 5 → 1 loop |
| Deploy keys (resources) | Yes | 5 → 1 loop |
| Workspace env vars (Ansible) | Yes | 10 → 1 loop |
| GitHub token env vars (Ansible) | Yes | 5 → 1 loop |
| Deploy key exports | Yes | 10 → 1 loop |
| Deploy key validation (provision.sh) | Yes | 5 → 1 loop |
| Status echoes (provision.sh) | Yes | ~10 → 1 loop |
| Python YAML keys (provision.sh) | Yes | ~10 → 1 loop |
| **Telegram config** | **No** | Keep separate (irregularity) |
| **Obsidian config** | **No** | Keep separate (naming mismatch) |

**Net savings: ~100 lines in index.ts, ~60 lines in provision.sh.** Adding a new agent becomes: add ID to array + set Pulumi config values. **2 locations instead of 12.**

## Effort Estimate

**Medium** (3-5 hours).

| Task | Effort |
|------|--------|
| Refactor index.ts config reads + deploy keys + env vars | 1-2h |
| Refactor index.ts exports (preserving API) | 30min |
| Refactor provision.sh env reads + validation + echoes + YAML | 1-2h |
| Run `pulumi preview` to verify zero replacements | 15min |
| Run `./scripts/provision.sh --check --diff` | 15min |
| Full deployment test | 30min |

## Recommendation

**Conditional Go.**

The refactor is feasible and would reduce the per-agent maintenance burden from 12 edit locations to 2. The primary risk (Pulumi resource URN changes) is fully mitigable by preserving the existing logical name pattern (`workspace-deploy-key` for main, `workspace-deploy-key-{id}` for others).

**Prerequisites before implementation:**

1. Run `pulumi preview` before and after the refactor — diff must show `0 to create, 0 to destroy` for all deploy key resources
2. Keep Telegram and Obsidian config outside the loop (irregularity makes a generic loop misleading)
3. Preserve existing Pulumi stack output names for backward compatibility with `setup-workspace.sh`
4. Consider adding Pulumi `aliases` as a safety net even when logical names match — protects against future naming changes

**When to implement:** This is a quality-of-life improvement, not urgent. Best done when adding the next agent (the effort pays for itself immediately). If no new agents are planned, defer.
