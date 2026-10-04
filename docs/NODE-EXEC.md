# Mac command access

Agents with Mac access use **native Bash and pinned SSH**. Commands run with the
Mac account's full permissions; they are not sandboxed. Read
[SECURITY §5](SECURITY.md#5-self-modification-via-mac-access).

## Current transport

```text
Claude native Bash → VPS SSH client → dedicated key + pinned host key → Mac account
```

Mac access is optional and per agent. Set `openclaw_claude_cli_mac_host` (an SSH
alias the controller can reach) and `openclaw_claude_cli_mac_user` in
`openclaw.yml`, and `mac_access: true` on each agent in `openclaw_agents` that
should reach the Mac. Other agents' containers get no Mac key, pin or host entry.
This holds only with containers on: in native mode every agent runs as `ubuntu`
on the host and can use the Mac key. The container role bootstraps SSH in
`ansible/roles/claude-cli/tasks/mac-ssh.yml`. It:

- creates a dedicated VPS identity only if absent;
- gets the Mac host's public key through the controller's trusted SSH route;
- pins that key instead of trusting an unverified network scan;
- authorizes only the dedicated public key, without replacing existing keys;
- configures strict host-key checks, a fixed identity/user and batch mode.

It contacts the Mac only when the VPS has no pin yet, so a sleeping Mac does
not block provisioning. To bootstrap again, delete
`~/.ssh/known_hosts_openclaw_mac_air` on the VPS while the Mac is awake.

Withholding the key is the whole boundary: a container without access gets no
credential, but nothing blocks the network path. After taking Mac access away
from an agent, rotate the key so a copy it kept stops working: remove the line
holding the key from the VPS's `~/.ssh/id_ed25519_openclaw_mac_air.pub` from the
Mac account's `~/.ssh/authorized_keys` (match the key itself, not its comment),
delete `~/.ssh/id_ed25519_openclaw_mac_air*` and
`~/.ssh/known_hosts_openclaw_mac_air` on the VPS, then run
`./scripts/provision.sh --tags claude-cli` with the Mac awake.

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

Keep the agreed `tools.exec.security: full` and `tools.exec.ask: off`; they
control Claude's permission mode, not SSH isolation.
