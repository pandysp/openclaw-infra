# Container Claude CLI live-acceptance harnesses

Scoped, reversible trials against the real VPS. They are **not** deployment
assets and **not** part of `python3 -m unittest discover -s scripts/tests`.
Run them from a copy of this directory on the VPS (`ubuntu@openclaw-vps`),
using `rsync -a` to preserve executable modes and keeping the layout intact: scripts locate `trial-backend.py`, `cancel-bin/`
and their evidence files relative to themselves.

Before any trial that restarts the gateway: check running cron jobs and the
Night Shift windows (Berlin 23:45 henning, 00:00 nici, 00:15 volki), and never
cancel another session's staging run. Shipping, activation and deletion still
need explicit keyboard-user permission; a passing trial is not rollout approval.

## Installed-asset trials

| Script | What it proves | Restarts gateway |
|---|---|---|
| `trial-backend.py apply\|restore` | Reversible `agents.defaults.cliBackends` override to `restricted-dispatcher.py`; requires the container backend and restores it | yes |
| `restricted-dispatcher.py` | Runs every session through the installed launcher; `cwrapper-cancel-*` sessions also get `cancel-bin/` first in `PATH` | — |
| `restricted-gateway-trial.py` | Per-agent feature suite through real gateway turns (native tools, scoped MCP, skills, Git read and push to a throwaway branch, strict Mac SSH, warm/cold sessions) on the installed container backend; needs containers enabled | no |
| `gateway-cancellation-trial.py chat\|kill` | Foreground `chat.abort` cleanup, or explicit launcher SIGKILL via one validated pidfd; requires an actual live sleep PID; 5-minute rollback timer | yes |
| `cancel-bin/docker` | Fault shim: an attach client that ignores quick cleanup, selected only for `cwrapper-cancel-*` | — |
| `guard-launch-race-trial.py` | Stopped guard cannot admit a late `docker create` (real Docker, real guard service) | no |
| `guard-held-create-trial.py LAUNCHER.py` | Holds the client return after real stopped creation; teardown waits on its lease, revoked admission refuses start, fixture removed. Not late daemon-completion proof; refuses teardown with other labelled runtimes | no |
| `docker-cancellation-trial.py [LAUNCHER.py]` | SIGTERM/SIGINT during a real container run: exit 143/130 and container removed | no |
| `guard-close-window-trial.py [--expect-closed]` | Linux-only kernel lock regression: final readiness-write EIO leaves byte `1`, but lifetime revocation before the exclusive wait still refuses new admission. Loads the **installed** guard and launcher; Docker, systemd and nft are mocked. Run with `sudo -n` | no |
| `guard-recovery-trial.py` | A second guard on its own fixture table restarts and restores both tables after table deletion or flushed rules, removing only its labelled containers | no |
| `guard-slow-cleanup-trial.py` | Slow Docker/nft boundaries: systemd stop still restores the policy past the old 25-second deadline | no |
| `bridge-guard-trial.py` | Same- and custom-bridge peers, metadata, user data and direct qmd are blocked; proxy and public web stay reachable | no |

## Mac access expectations

The gateway trial checks the runtime's permitted agents against an explicit
expectation. For main-only Mac access:

```bash
python3 restricted-gateway-trial.py --expect-mac-agent main
# To run only main's checks:
python3 restricted-gateway-trial.py --expect-mac-agent main main
```

Repeat `--expect-mac-agent` for additional permitted agents. Omit it only when
Mac access is disabled for everyone. An unexpected grant, or missing access
for an expected agent, fails before any gateway turn. Denied agents are checked directly for
key/pin content, SSH configuration and Mac host entries; empty Docker mountpoint
files and unrelated host entries are not credentials. When no Mac hostname is
known, the host-entry check is listed under `unverified_checks`, not claimed as
passing.

Passing per-agent results do not prove cleanup finished. Require the trial to
exit successfully. It attempts independent cleanup even after a failure and
reports all cleanup errors together; a timed-out Git push still attempts to
delete its throwaway branch.

## Evidence

Each script writes a small JSON evidence file next to itself with booleans and
timings only. Never print HTTP bodies, transcripts, tokens or environment
values.
