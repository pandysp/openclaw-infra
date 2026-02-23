# Investigation: Handler Consolidation (US-017)

**Date:** 2026-02-23
**Status:** Complete
**Recommendation:** GO — small effort, low risk

## Problem

The `restart openclaw-gateway` handler is duplicated across 6 role handler files. Any change to the handler (e.g., adding a delay, changing the service name) requires editing 6 files.

## Current State

### Roles defining `restart openclaw-gateway`

| # | Role | Handler file | Other handlers in file | Notified by tasks? |
|---|------|-------------|----------------------|-------------------|
| 1 | config | `roles/config/handlers/main.yml` | None | Yes (3 tasks) |
| 2 | agents | `roles/agents/handlers/main.yml` | None | Yes (2 tasks) |
| 3 | telegram | `roles/telegram/handlers/main.yml` | None | Yes (3 tasks in bindings.yml, session-cleanup.yml, channel.yml) |
| 4 | openclaw | `roles/openclaw/handlers/main.yml` | None | Yes (1 task in patch-heartbeat-group-fix.yml) |
| 5 | plugins | `roles/plugins/handlers/main.yml` | `restart mcp-auth-proxy` | Yes (4 tasks) |
| 6 | qmd | `roles/qmd/handlers/main.yml` | `reload user systemd`, `enable qmd-watch services` | **No** (dead handler) |

### All 6 definitions are identical

```yaml
- name: restart openclaw-gateway
  ansible.builtin.systemd:
    name: openclaw-gateway
    state: restarted
    scope: user
  environment:
    XDG_RUNTIME_DIR: "/run/user/1000"
```

### Roles WITHOUT the handler

- `workspace` — has `reload user systemd` and `enable workspace timers`
- `system`, `docker`, `ufw`, `sandbox`, `obsidian` — no handler files

### Handler flush points

Three tasks use `meta: flush_handlers`:
- `telegram/tasks/cron.yml:3` — ensures gateway restart before cron operations
- `plugins/tasks/main.yml:510` — ensures proxy restart before image build
- `plugins/tasks/main.yml:1457` — ensures gateway restart before plugin verification

## Ansible Handler Scoping Rules

Key findings from [Ansible documentation](https://docs.ansible.com/projects/ansible/latest/playbook_guide/playbooks_handlers.html) and [ansible/ansible#15476](https://github.com/ansible/ansible/issues/15476):

1. **Handlers are play-scoped, not role-scoped.** Role handlers are inserted into the global play scope when the role is loaded. A handler defined in any role is accessible from any other role in the same play.

2. **Play-level handlers work from roles.** A handler defined in the `handlers:` section of a play is accessible from all tasks in all roles listed in the `roles:` section. This is the standard pattern for shared handlers.

3. **Handler ordering:** Handlers from `roles:` are added first (in role order), then handlers from the play `handlers:` section. For identically-named handlers, the last definition wins.

4. **No `include_role` concerns.** This playbook uses only static `roles:` section — no `include_role` or `import_role` in any task file. Dynamic inclusion would change handler scoping, but this codebase doesn't use it.

5. **No role dependencies.** No `meta/main.yml` files exist, so no implicit role inclusion.

## Proposed Consolidation

**Approach:** Add a `handlers:` section to `playbook.yml` with the single handler definition. Remove the duplicate from all 6 role handler files.

### Changes needed

| File | Action |
|------|--------|
| `playbook.yml` | Add `handlers:` section with `restart openclaw-gateway` |
| `roles/config/handlers/main.yml` | Remove file (was only handler) |
| `roles/agents/handlers/main.yml` | Remove file (was only handler) |
| `roles/telegram/handlers/main.yml` | Remove file (was only handler) |
| `roles/openclaw/handlers/main.yml` | Remove file (was only handler) |
| `roles/plugins/handlers/main.yml` | Remove `restart openclaw-gateway` only (keep `restart mcp-auth-proxy`) |
| `roles/qmd/handlers/main.yml` | Remove `restart openclaw-gateway` only (keep other two handlers; was dead code anyway) |

### Resulting `playbook.yml` handlers section

```yaml
handlers:
  - name: restart openclaw-gateway
    ansible.builtin.systemd:
      name: openclaw-gateway
      state: restarted
      scope: user
    environment:
      XDG_RUNTIME_DIR: "/run/user/1000"
```

## Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Handler flushing order changes | **None** | `meta: flush_handlers` flushes all pending handlers regardless of where they're defined. Play-scoped handler behaves identically. |
| Role-specific handler interaction | **None** | `restart mcp-auth-proxy` (plugins) and `reload user systemd` / `enable qmd-watch services` (qmd) are separate handlers with separate names. They are unaffected. |
| `include_role` scope isolation | **N/A** | No `include_role` or `import_role` is used anywhere in this codebase. |
| Future role additions | **Low** | New roles that need the restart handler simply `notify: restart openclaw-gateway` — no handler file needed. This is strictly better than the current pattern. |
| Empty `handlers/` directories | **None** | Ansible does not require the directory to exist. Removing it is clean. |

## Dead Code Finding

The `qmd` role defines `restart openclaw-gateway` but no task in the qmd role notifies it. This is dead code that should be removed regardless of whether consolidation happens.

## Effort Estimate

**Small** — approximately 7 file changes, all mechanical. The handler definitions are confirmed identical, so this is a pure deduplication with no logic changes.

## Recommendation

**GO.** Play-level handler consolidation is safe and straightforward for this codebase because:
1. All 6 definitions are identical
2. No `include_role` / `import_role` usage (the main risk factor)
3. No role dependencies that could change handler resolution
4. `meta: flush_handlers` works identically with play-level handlers
5. Reduces maintenance burden from 6 files to 1 location
