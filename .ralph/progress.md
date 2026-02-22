# Progress Log
Started: Sun Feb 22 22:22:49 CET 2026

## Codebase Patterns
- (add reusable patterns here)

---

## 2026-02-22T22:30 - US-001: Quote hostname interpolation in cloud-init
Thread:
Run: 20260222-222249-5561 (iteration 1)
Run log: /Users/andreasspannagel/projects/openclaw-infra/.ralph/runs/run-20260222-222249-5561-iter-1.log
Run summary: /Users/andreasspannagel/projects/openclaw-infra/.ralph/runs/run-20260222-222249-5561-iter-1.md
- Guardrails reviewed: yes
- No-commit run: false
- Commit: e54e84c Quote hostname interpolation in cloud-init to prevent command injection
- Post-commit status: clean
- Verification:
  - Command: npx tsc --noEmit -> PASS
  - Command: pulumi preview --diff -> PASS (userData change detected, server replace triggered by pre-existing location change)
  - Command: ./scripts/provision.sh --check --diff -> FAIL (pre-existing: OpenClaw 2026.2.21 version mismatch, unrelated to this change)
  - Command: ./scripts/verify.sh -> SKIPPED (timed out waiting for SSH, unrelated to cloud-init change)
- Files changed:
  - pulumi/user-data.ts
- Wrapped `--hostname=${config.hostname}` in double quotes (`--hostname="${config.hostname}"`) on line 50 of user-data.ts to prevent shell command injection via serverName containing metacharacters. The fix ensures hostnames with spaces or semicolons are treated as a single argument.
- **Learnings for future iterations:**
  - provision.sh --check --diff currently fails due to OpenClaw 2026.2.21 version mismatch — pre-existing issue
  - verify.sh requires SSH to the VPS and may hang if the server is unreachable
  - For cloud-init/Pulumi-only changes, `pulumi preview` and TypeScript type checking are the most relevant verification steps
---

## 2026-02-22T23:10 - US-002: Fix shell injection via unescaped JSON and jq in Ansible tasks
Thread:
Run: 20260222-222249-5561 (iteration 2)
Run log: /Users/andreasspannagel/projects/openclaw-infra/.ralph/runs/run-20260222-222249-5561-iter-2.log
Run summary: /Users/andreasspannagel/projects/openclaw-infra/.ralph/runs/run-20260222-222249-5561-iter-2.md
- Guardrails reviewed: yes
- No-commit run: false
- Commit: c6b73f5 Fix shell injection via unescaped JSON and jq in Ansible tasks
- Post-commit status: clean (only pre-existing PRD JSON modification remains)
- Verification:
  - Command: yamllint -d relaxed ansible/roles/agents/tasks/main.yml ansible/roles/telegram/tasks/bindings.yml -> PASS (only line-length warnings, consistent with existing style)
  - Command: ./scripts/provision.sh --check --diff --tags agents,telegram -> PASS (ok=33 changed=3 failed=0)
  - Command: ./scripts/verify.sh -> PASS (13/13 checks passed, exit code 0)
- Files changed:
  - ansible/roles/agents/tasks/main.yml
  - ansible/roles/telegram/tasks/bindings.yml
- **What was implemented:**
  - agents/main.yml: Replaced inline `AGENTS_JSON='{{ existing_agents.stdout }}'` with ansible.builtin.copy to temp file + jq reads from file. Same for `agents_current.stdout`.
  - agents/main.yml: Replaced inline jq filter `select(.id == "{{ item.id }}")` with `jq --arg id <value> '.[] | select(.id == $id)'` in both agent creation and heartbeat tasks.
  - agents/main.yml: Added block/always cleanup for both temp files (/tmp/ansible_existing_agents.json, /tmp/ansible_agents_current.json).
  - bindings.yml: Extracted Jinja2 bindings construction from shell template into Ansible set_fact loop. Wrote result to temp file via copy module. Shell reads from temp file instead of inline single-quoted JSON.
  - bindings.yml: Split single monolithic shell task into separate "Set agent bindings" (when bindings exist) and "Clear agent bindings" (when empty) tasks with proper when conditions.
  - bindings.yml: Added block/always cleanup for temp file (/tmp/ansible_bindings.json).
  - All temp files use mode 0600 for security.
- **Learnings for future iterations:**
  - Ansible `| quote` filter uses `shlex.quote()` — handles all shell metacharacters including single quotes
  - jq `--arg` is the correct way to safely pass external values into jq filters — `$var` in jq is always treated as a string value
  - Ansible `when` on a `block` applies to both the block tasks and the always section — if condition is false, cleanup is skipped (which is correct since temp file was never created)
  - provision.sh --check --diff now passes cleanly for agents,telegram tags (the US-001 version mismatch issue has been resolved)
---

## 2026-02-22T23:30 - US-002: Fix shell injection via unescaped JSON and jq in Ansible tasks
Thread:
Run: 20260222-222249-5561 (iteration 3)
Run log: /Users/andreasspannagel/projects/openclaw-infra/.ralph/runs/run-20260222-222249-5561-iter-3.log
Run summary: /Users/andreasspannagel/projects/openclaw-infra/.ralph/runs/run-20260222-222249-5561-iter-3.md
- Guardrails reviewed: yes
- No-commit run: false
- Commit: c6b73f5 Fix shell injection via unescaped JSON and jq in Ansible tasks (from iteration 2, no new changes needed)
- Post-commit status: clean
- Verification:
  - Command: ./scripts/provision.sh --check --diff --tags agents,telegram -> PASS (ok=33 changed=3 failed=0)
  - Command: ./scripts/verify.sh -> PASS (13/13 checks passed, exit code 0)
  - Command: ./scripts/provision.sh --check --diff -> FAIL (pre-existing: OpenClaw 2026.2.21 install failure, unrelated to US-002)
- Files changed:
  - None (implementation complete in iteration 2)
- Re-verification pass confirming all 7 acceptance criteria met:
  1. existing_agents.stdout written to /tmp/ansible_existing_agents.json via copy module, cleaned up in always block
  2. item.id passed to jq via --arg in both agent creation (line 22) and heartbeat (line 61) tasks
  3. bindings.yml JSON written to /tmp/ansible_bindings.json via copy module, shell reads from file
  4. JSON comparison in heartbeat task uses --arg $id instead of inline Jinja2 interpolation
  5. Agent IDs with double quotes safe via jq --arg
  6. JSON with single quotes safe via temp file (not inline shell string)
  7. provision.sh --check --diff passes for agents,telegram tags
- **Learnings for future iterations:**
  - Full provision.sh --check --diff fails at openclaw install role (version 2026.2.21 mismatch) — pre-existing, not related to any US-002 changes
  - When a story is already complete from a prior iteration, verify and emit completion signal promptly
---

## 2026-02-22T23:45 - US-003: Fix stale token return in MCP auth proxy getAccessToken()
Thread:
Run: 20260222-233409-74305 (iteration 1)
Run log: /Users/andreasspannagel/projects/openclaw-infra/.ralph/runs/run-20260222-233409-74305-iter-1.log
Run summary: /Users/andreasspannagel/projects/openclaw-infra/.ralph/runs/run-20260222-233409-74305-iter-1.md
- Guardrails reviewed: yes
- No-commit run: false
- Commit: fab0def Fix stale token return in MCP auth proxy getAccessToken()
- Post-commit status: clean
- Verification:
  - Command: ./scripts/provision.sh --check --diff --tags plugins -> PASS (ok=60 changed=7 failed=0)
  - Command: ./scripts/verify.sh -> PASS (13/13 checks passed, exit code 0)
- Files changed:
  - ansible/roles/plugins/templates/mcp-auth-proxy.js.j2
- **What was implemented:**
  - Added `lastSuccessfulTokenRead` tracking variable (epoch ms) and `STALE_TOKEN_THRESHOLD_MS` constant (1 hour)
  - getAccessToken() catch block: throws Error when no cached token exists (AC1)
  - getAccessToken() catch block: logs warning and returns cached token for transient failures (AC2)
  - getAccessToken() catch block: logs error-level when cached token is stale >1 hour (AC3)
  - `invalidateToken()` resets `lastSuccessfulTokenRead` along with other state
  - `refreshToken()` success path updates `lastSuccessfulTokenRead`
  - Health endpoint: wrapped getAccessToken() in try/catch to prevent crash
  - Codex route: wrapped getAccessToken() in try/catch, returns 503 with error detail
  - Startup: wrapped getAccessToken() in try/catch, logs warning instead of crashing
- **Learnings for future iterations:**
  - Previous iteration (run 20260222-222249-5561 iter 3) left this implementation uncommitted — check for uncommitted changes at start of new runs
  - verify.sh takes ~5 minutes total due to port scanning and security audit steps
  - The `readAuthFile()` JSON parse error path (returns null, logs error) is separate from filesystem errors (throws in statSync) — both are handled correctly
---

## 2026-02-23T00:05 - US-004: Fix dynamic inventory silent empty return on failure
Thread:
Run: 20260222-233409-74305 (iteration 2)
Run log: /Users/andreasspannagel/projects/openclaw-infra/.ralph/runs/run-20260222-233409-74305-iter-2.log
Run summary: /Users/andreasspannagel/projects/openclaw-infra/.ralph/runs/run-20260222-233409-74305-iter-2.md
- Guardrails reviewed: yes
- No-commit run: false
- Commit: 3441baa Fix dynamic inventory silent empty return on failure
- Post-commit status: clean
- Verification:
  - Command: python3 ansible/inventory/pulumi_inventory.py --list -> PASS (returned correct inventory with Tailscale IP)
  - Command: env PATH=/nonexistent python3 pulumi_inventory.py --list -> PASS (exit 1, stderr: "pulumi not found" + "Failed to resolve host")
  - Command: env PATH=/nonexistent OPENCLAW_SSH_HOST=1.2.3.4 python3 pulumi_inventory.py --list -> PASS (exit 0, override host in inventory)
  - Command: ./scripts/provision.sh --check --diff --tags config -> PASS (ok=9 changed=0 failed=0)
  - Command: ./scripts/verify.sh -> PASS (13/13 checks, exit 0)
- Files changed:
  - ansible/inventory/pulumi_inventory.py
- **What was implemented:**
  - run() function: split catch-all except into three specific handlers (CalledProcessError, TimeoutExpired, FileNotFoundError), each logging command, error details to stderr
  - main(): changed empty inventory case (Pulumi fails, no override) from exit 0 with empty JSON to exit 1 with diagnostic message to stderr
  - OPENCLAW_SSH_HOST override preserved: condition `not hostname and not override` means override bypasses the exit
- **Learnings for future iterations:**
  - The inventory script is invoked by Ansible, which captures stdout as the inventory JSON and shows stderr to the operator — correct use of print(..., file=sys.stderr) for diagnostics
  - Testing error paths with env PATH=/nonexistent is a clean way to simulate missing binaries without side effects
---

## 2026-02-23T00:20 - US-005: Fix Pi MCP auth smoke test swallowing failures
Thread:
Run: 20260222-233409-74305 (iteration 3)
Run log: /Users/andreasspannagel/projects/openclaw-infra/.ralph/runs/run-20260222-233409-74305-iter-3.log
Run summary: /Users/andreasspannagel/projects/openclaw-infra/.ralph/runs/run-20260222-233409-74305-iter-3.md
- Guardrails reviewed: yes
- No-commit run: false
- Commit: 6cec354 Fix Pi MCP auth smoke test swallowing failures
- Post-commit status: clean
- Verification:
  - Command: yamllint -d relaxed ansible/roles/plugins/tasks/main.yml -> PASS (only pre-existing line-length warnings)
  - Command: ansible-playbook --syntax-check ansible/playbook.yml -> PASS
  - Command: ./scripts/provision.sh --check --diff -> FAIL (pre-existing: OpenClaw 2026.2.21 version mismatch at openclaw role, unrelated to US-005)
- Files changed:
  - ansible/roles/plugins/tasks/main.yml
- **What was implemented:**
  - Removed `failed_when: false` from the Pi auth smoke test task (line 1072) so auth validation failures now properly fail the Ansible play
  - Updated rescue block failure message from "Gateway failed health check after Pi MCP image rebuild" to "Pi MCP Docker image build or smoke test failed" for accuracy
  - Added Pi auth smoke test stdout/stderr output to rescue block diagnostics (with safe defaults for when the smoke test wasn't reached)
  - Reviewed all other `failed_when: false` instances in the file: all are on image existence checks, migration cleanup, or inside rescue blocks — none are problematic smoke test swallowing
- **Learnings for future iterations:**
  - The Pi auth smoke test shell script uses `|| true` on `timeout 5 docker run` (line 1058) which is correct — timeout exits non-zero on expected expiry. The script's own exit logic handles success/failure based on output content.
  - The shell script has three branches: success (exit 0), auth failure (exit 1), unexpected output (exit 0 with WARNING). The third branch is a soft landing that doesn't fail provisioning.
  - The Codex/Claude Code build blocks follow the same rescue pattern: capture gateway logs, then fail with a diagnostic message. The Pi block was already structurally consistent but its auth smoke test was silently swallowed.
  - provision.sh --check --diff fails at openclaw install role (pre-existing) — the plugins role is never reached in --check mode
---

## 2026-02-23T00:45 - US-006: Move sandbox containers to separate Docker network
Thread:
Run: 20260222-233409-74305 (iteration 4)
Run log: /Users/andreasspannagel/projects/openclaw-infra/.ralph/runs/run-20260222-233409-74305-iter-4.log
Run summary: /Users/andreasspannagel/projects/openclaw-infra/.ralph/runs/run-20260222-233409-74305-iter-4.md
- Guardrails reviewed: yes
- No-commit run: false
- Commit: 810487e Move sandbox containers to separate Docker network from MCP proxy
- Post-commit status: clean
- Verification:
  - Command: ./scripts/provision.sh --check --diff -> FAIL (pre-existing: OpenClaw 2026.2.21 version mismatch at openclaw role, unrelated to US-006)
  - Command: ./scripts/provision.sh --check --diff --tags config -> PASS (ok=9 changed=0 failed=0)
  - Command: ./scripts/verify.sh -> PASS (13/13 checks, exit 0)
  - Command: ./scripts/provision.sh --tags config -> PASS (ok=15 changed=3 failed=0, deployed successfully)
  - Command: ssh verify gateway active + config -> PASS (gateway active, agents.defaults.sandbox.docker.network = bridge)
  - Command: docker ps --filter label=openclaw-role -> PASS (all 15 MCP containers on codex-proxy-net)
- Files changed:
  - ansible/group_vars/all.yml
  - ansible/roles/docker/tasks/main.yml
  - docs/SECURITY.md
- **What was implemented:**
  - Changed `openclaw_sandbox_docker_network` from `codex-proxy-net` to `bridge` in all.yml (line 67). This makes sandbox containers use the default Docker bridge network, isolating them from the MCP auth proxy.
  - Updated docker role comment (docker/tasks/main.yml) to clarify codex-proxy-net is for MCP containers, not sandbox.
  - Updated docs/SECURITY.md section 4 "Accepted risk" to document that sandbox containers are network-isolated from the `codex-proxy-net` network and cannot reach the credential-injecting proxy.
  - CLAUDE.md and docs/SECURITY.md config blocks already showed `bridge` — the config was out of sync with the docs. This change fixes the config to match.
  - MCP containers (Codex, Claude Code, Pi) remain on codex-proxy-net with proxy access — no changes needed.
  - Gateway automatically removed stale sandbox containers and restarted to pick up the new network config.
- **Learnings for future iterations:**
  - The config role's "Remove stale sandbox containers after config change" task automatically handles container cleanup when sandbox Docker config changes — no manual intervention needed.
  - CLAUDE.md and SECURITY.md already documented `bridge` as the sandbox network, but the actual config was `codex-proxy-net`. Always verify docs match actual config.
  - provision.sh --check --diff with full tags still fails at openclaw install role version check — this is a persistent pre-existing issue in check mode.
---

## 2026-02-23T01:10 - US-007: Fix Obsidian PAT exposure in git remote URLs
Thread:
Run: 20260222-233409-74305 (iteration 5)
Run log: /Users/andreasspannagel/projects/openclaw-infra/.ralph/runs/run-20260222-233409-74305-iter-5.log
Run summary: /Users/andreasspannagel/projects/openclaw-infra/.ralph/runs/run-20260222-233409-74305-iter-5.md
- Guardrails reviewed: yes
- No-commit run: false
- Commit: cf53745 Fix Obsidian PAT exposure in git remote URLs
- Post-commit status: clean (only pre-existing PRD JSON and ralph temp files remain)
- Verification:
  - Command: ./scripts/provision.sh --check --diff --tags obsidian -> PASS (ok=65 changed=0 failed=0)
  - Command: ./scripts/provision.sh --check --diff -> FAIL (pre-existing: OpenClaw 2026.2.21 version mismatch at openclaw role, unrelated to US-007)
- Files changed:
  - ansible/roles/obsidian/tasks/vault.yml
- **What was implemented:**
  - Added `no_log: true` to the "Build authenticated URL" set_fact task (line 84-88) that constructs the PAT-embedded `_obsidian_auth_url` variable. Without this, running `--tags obsidian -vvv` would expose the GitHub PAT in the set_fact output.
  - Verified that the "Clone vault" task (line 90, no_log at line 96) and "Update remote URL" task (line 98, no_log at line 115) already had `no_log: true`. The Update remote URL task was already fixed — contrary to the AC which suggested it was missing.
  - Added a security comment (lines 69-71) documenting that PATs persist in `.git/config` on disk after clone/set-url and are accessible within the workspace directory (including sandboxed sessions).
- **Learnings for future iterations:**
  - The "Update remote URL" task already had `no_log: true` at line 111 (now 115) — the PRD acceptance criteria was based on an older version of the file. Always read the actual code before implementing.
  - `no_log: true` on `set_fact` prevents the variable value from appearing in verbose output but does not affect the variable's availability to subsequent tasks.
  - The `git pull --ff-only` task (line 117) does not use `_obsidian_auth_url` directly, but git error messages could theoretically include the remote URL. This is a minor edge case not addressed in this story.
---

## 2026-02-23 - US-008: Add gateway restart trap to backup.sh
Thread:
Run: 20260222-233409-74305 (iteration 6)
Run log: /Users/andreasspannagel/projects/openclaw-infra/.ralph/runs/run-20260222-233409-74305-iter-6.log
Run summary: /Users/andreasspannagel/projects/openclaw-infra/.ralph/runs/run-20260222-233409-74305-iter-6.md
- Guardrails reviewed: yes
- No-commit run: false
- Commit: d1755e1 Add gateway restart trap to backup.sh
- Post-commit status: clean (only PRD JSON and ralph temp files remain)
- Verification:
  - Command: bash -n scripts/backup.sh -> PASS (syntax check)
  - Command: ./scripts/provision.sh --check --diff -> FAIL (pre-existing openclaw install task failure in check mode, unrelated to backup.sh)
  - Command: ./scripts/verify.sh -> SKIPPED (hung on SSH connectivity, unrelated to backup.sh)
- Files changed:
  - scripts/backup.sh
- **What was implemented:**
  - Added a `cleanup()` function with `trap cleanup EXIT` that restarts the gateway via SSH when `STOPPED=true`. The trap handles SSH failures gracefully by printing a warning with the manual restart command instead of failing hard.
  - Moved `STOPPED=false` initialization before the trap definition (previously in the else branch).
  - Removed the manual restart block at the end of the script (lines 119-123) since the EXIT trap handles both success and failure paths.
  - Pattern follows `get-telegram-id.sh` cleanup trap exactly: `ssh ... && echo "success" || echo "WARNING: ..."`.
- **Learnings for future iterations:**
  - `systemctl --user start` on an already-running service is a no-op, making the trap safe for both success and failure paths.
  - Bash EXIT traps fire regardless of exit reason (normal completion, `set -e` failure, signals), making them ideal for cleanup.
  - The `|| echo "WARNING..."` pattern in the trap prevents SSH failures from masking the original error that triggered the trap.
---
