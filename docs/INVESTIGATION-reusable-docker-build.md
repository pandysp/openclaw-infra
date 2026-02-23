# Investigation: Reusable Docker Image Build Task

**Story:** US-015
**Date:** 2026-02-23
**Status:** Go (with caveats)

## Current Duplication

Three near-identical Docker image build blocks exist in `ansible/roles/plugins/tasks/main.yml`:

| Block | Lines | Task Count | Purpose |
|-------|-------|------------|---------|
| Codex MCP | 700-852 | 13 tasks | Build `codex-mcp:latest` |
| Claude Code MCP | 853-978 | 12 tasks | Build `claude-code-mcp:latest` |
| Pi MCP | 979-1145 | 15 tasks | Build `pi-mcp:latest` |

**Total duplicated lines:** ~446 lines across 40 tasks.

## Structural Comparison

### Identical Steps (present in all 3 blocks)

| Step | Pattern |
|------|---------|
| 1. Check if image exists | `docker image inspect` |
| 2. Create build directory | `ansible.builtin.file` with `state: directory` |
| 3. Template Dockerfile | `ansible.builtin.template` from `Dockerfile.<type>.j2` |
| 4. Build Docker image | `docker build -t` (conditional on image missing, Dockerfile changed, or force rebuild) |
| 5. Verify image was built | `docker image inspect` (idempotent check) |
| 6. Remove containers after rebuild | `docker ps -aq --filter "label=openclaw-role=<type>"` + `xargs docker rm -f` |
| 7. Verify no containers remain | Count containers by label, fail if > 0 |
| 8. Restart gateway | `systemctl --user restart openclaw-gateway` |
| 9. Wait for gateway health | `openclaw health` with retries |
| 10. Rescue: capture gateway logs | `journalctl --user -u openclaw-gateway` |
| 11. Rescue: fail with diagnostic context | `ansible.builtin.fail` with log output |

These 11 steps are structurally identical across all 3 blocks. Only variable names and label values differ.

### Differing Steps (smoke tests)

| Step | Codex | Claude Code | Pi |
|------|-------|-------------|-----|
| Basic smoke test | `codex --version` | `claude --version` | `which pi-mcp-server` |
| Hardened smoke test | Checks no auth.json + `no-creds-ok` | Checks `claude` + `claude-code-mcp` binaries | Checks `pi-mcp-server` binary |
| Proxy connectivity | Node.js HTTP GET to proxy `/health` | (covered by pre-build validation) | (covered by pre-build validation) |
| Auth smoke test | N/A | N/A | Pi-specific: starts server with `timeout 5`, checks "pi-mcp-server started" |
| Auth smoke debug output | N/A | N/A | `debug: var: pi_auth_smoke.stdout_lines` |

**Key difference:** Pi has 2 extra tasks (auth startup smoke test + debug output) that Codex and Claude Code don't have.

### Differing Conditions

| Parameter | Codex | Claude Code | Pi |
|-----------|-------|-------------|-----|
| When condition | `codex_auth_json \| length > 0` | `claude_setup_token \| length > 0` | `claude_setup_token \| length > 0` |
| Force rebuild var | `force_codex_rebuild` | `force_claude_code_rebuild` | `force_pi_mcp_rebuild` |
| Container label | `openclaw-role=codex-mcp` | `openclaw-role=claude-code-mcp` | `openclaw-role=pi-mcp` |

## Parameters for Reusable `build-mcp-image.yml`

A parameterized include would need these variables:

| Parameter | Example (Codex) | Example (Claude Code) | Example (Pi) |
|-----------|----------------|----------------------|-------------|
| `mcp_type` | `codex` | `claude-code` | `pi` |
| `mcp_image_name` | `{{ openclaw_mcp_adapter.codex_docker_image }}` | `{{ openclaw_mcp_adapter.claude_code_docker_image }}` | `{{ openclaw_mcp_adapter.pi_mcp_docker_image }}` |
| `mcp_build_dir` | `/home/ubuntu/.openclaw/codex-mcp-build` | `/home/ubuntu/.openclaw/claude-code-mcp-build` | `/home/ubuntu/.openclaw/pi-mcp-build` |
| `mcp_dockerfile_template` | `Dockerfile.codex-mcp.j2` | `Dockerfile.claude-code-mcp.j2` | `Dockerfile.pi-mcp.j2` |
| `mcp_container_label` | `codex-mcp` | `claude-code-mcp` | `pi-mcp` |
| `mcp_force_rebuild_var` | `force_codex_rebuild` | `force_claude_code_rebuild` | `force_pi_mcp_rebuild` |
| `mcp_when_condition` | `codex_auth_json \| default('') \| length > 0` | `claude_setup_token \| default('') \| length > 0` | `claude_setup_token \| default('') \| length > 0` |
| `mcp_smoke_tests` | List of smoke test commands | List of smoke test commands | List of smoke test commands |
| `mcp_rescue_name` | `Codex` | `Claude Code` | `Pi MCP` |

### Smoke Test Parameterization

The smoke tests are the hardest part to parameterize. Options:

1. **List of shell commands**: Pass `mcp_smoke_tests` as a list of `{name, shell, when}` dicts. The include iterates and runs each. This handles Pi's extra auth test naturally.

2. **Separate include file per type**: Keep `smoke-test-codex.yml`, `smoke-test-claude-code.yml`, `smoke-test-pi.yml` and `include_tasks` them by name. Simpler but still 3 files.

3. **Skip smoke tests in shared include**: Move smoke tests out of the build block entirely. Run them after the include. This is the cleanest separation but changes the rescue scope (smoke test failures currently trigger the rescue block).

**Recommendation:** Option 1 (list of commands) is the most maintainable. The smoke test list is passed as a parameter, and the shared include iterates over it with `loop`. Each entry has `name`, `shell`, `when` (optional, defaults to `image_build is changed`).

## Risks

### Low Risk
- **Variable naming collision**: The include uses namespaced params (`mcp_*`), avoiding collision with role vars.
- **Handler scope**: `notify: restart openclaw-gateway` works the same from an included task file.
- **Register variable scope**: Variables registered in `include_tasks` are accessible in the calling scope. No issue.

### Medium Risk
- **Rescue block scope**: Ansible `block/rescue` cannot span an `include_tasks` boundary. The shared include must define its own `block/rescue` internally — which is exactly what it would do (the rescue is part of the shared pattern).
- **Conditional rescue variables**: Pi's rescue references `pi_auth_smoke.stdout` which may not exist in the Codex/Claude Code case. The shared rescue must use `| default('(did not run or not reached)')` on all smoke test variables. This is already done for Pi, so the pattern exists.

### Low Risk (but worth noting)
- **Jinja2 `when` evaluation**: The `mcp_when_condition` parameter would be a string evaluated by Ansible's `when:` clause. This works with `include_tasks` + `when:` on the include statement — the condition is evaluated before including.
- **`force_*_rebuild` variable lookup**: The force rebuild variable name differs per type. The include can use `lookup('vars', mcp_force_rebuild_var, default=false)` to dereference it dynamically.

### No Risk
- The Dockerfiles remain separate files (they have genuinely different content). Only the build/verify/cleanup task sequence is shared.
- No changes to the plugin config generation (lines 1176-1455) — that section is intentionally different per server type.

## Effort Estimate

**Small-Medium** (2-4 hours of focused work).

- Extract ~11 shared tasks into `build-mcp-image.yml` (~80 lines)
- Define smoke test lists for each type (~30 lines each, 3 types)
- Replace 3 blocks with 3 `include_tasks` calls (~15 lines each)
- Test with `--check --diff` and a real provision run

**Net reduction:** ~446 lines reduced to ~170 lines (build-mcp-image.yml + 3 include calls + 3 smoke test lists). Saves ~276 lines (~62% reduction).

**Future savings:** Adding a 4th MCP server type would require only a Dockerfile template, a smoke test list, and a 15-line `include_tasks` call — instead of copying 150 lines and adapting variable names.

## Recommendation

**Go.** The duplication is real, the differences are well-understood and parameterizable, and the risks are low. The smoke test list approach handles Pi's extra auth test without special-casing. The effort is small-medium and the payoff is immediate for the next MCP server addition.

### Suggested Implementation Order

1. Create `ansible/roles/plugins/tasks/build-mcp-image.yml` with the shared tasks
2. Replace the Codex block first (simplest smoke tests)
3. Replace Claude Code block
4. Replace Pi block (most smoke tests — validates the list approach)
5. Run `./scripts/provision.sh --check --diff` to verify no behavioral change
6. Deploy with `./scripts/provision.sh --tags plugins -e force_codex_rebuild=true -e force_claude_code_rebuild=true -e force_pi_mcp_rebuild=true` to verify all 3 images build correctly
