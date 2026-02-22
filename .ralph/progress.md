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
