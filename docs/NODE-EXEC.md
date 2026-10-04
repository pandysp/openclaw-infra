# Mac command access

Claude-backed agents use **native Bash and pinned SSH**, not `mac_run` MCP
or OpenClaw's node execution tool. Commands run with the Mac account's full
permissions; they are not sandboxed. Read [SECURITY §5](SECURITY.md#5-self-modification-via-node-control).

## Current transport

```text
Claude native Bash → VPS SSH client → dedicated key + pinned host key → Mac account
```

Mac access is optional. Set `openclaw_claude_cli_mac_host` (an SSH alias the
controller can reach) and `openclaw_claude_cli_mac_user` in `openclaw.yml`; with
an empty host, agent containers get no Mac access at all. With a host, the
container role bootstraps SSH in `ansible/roles/claude-cli/tasks/mac-ssh.yml`. It:

- creates a dedicated VPS identity only if absent;
- gets the Mac host's public key through the controller's trusted SSH route;
- pins that key instead of trusting an unverified network scan;
- authorizes only the dedicated public key, without replacing existing keys;
- configures strict host-key checks, a fixed identity/user and batch mode.

It contacts the Mac only when the VPS has no pin yet, so a sleeping Mac does
not block provisioning. To bootstrap again, delete
`~/.ssh/known_hosts_openclaw_mac_air` on the VPS while the Mac is awake.

Private keys stay on the machines, never in Git. C binds only the selected
SSH configuration, identity and host pins. Mac-to-VPS SSH remains an explicitly
accepted route; this is practical containment, not a hostile-agent boundary.

## Check access

Run from the VPS or from an agent's native Bash tool:

```bash
ssh -o BatchMode=yes <openclaw_claude_cli_mac_host> 'printf "mac-ssh-ok\n"'
```

Use an explicit Mac working directory in remote commands. A VPS workspace path
does not exist on the Mac. If authentication fails, check the dedicated public
key's authorization. If host-key verification fails, verify the new public key
through the trusted controller route before updating the pin.

## Retired node/MCP route

`node-exec-mcp@0.1.1` calls the removed `openclaw nodes run` command.
`nodes invoke` rejects reserved `system.run`; it is not a replacement.
Provisioning no longer generates Mac MCP registrations or their deny patterns.
The next `--tags plugins` run removes live leftovers: it replaces the adapter
configuration and each agent's deny list as a whole.

`node_exec_enabled` and `tools.exec.node` do **not** give Claude-backed agents
OpenClaw's `exec` tool: the MCP adapter excludes it for this harness.
Keep the agreed `tools.exec.security: full` and `tools.exec.ask: off`; changing
those affects Claude's permission mode, not SSH isolation.

An OpenClaw node host may still be paired for other clients. Its LaunchAgent,
pairing and token checks are separate from this SSH route. Do not reinstall
the retired MCP package or reset a node pin to repair Claude's Mac access.
