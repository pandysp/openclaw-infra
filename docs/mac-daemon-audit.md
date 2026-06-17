# Mac Daemon Audit — Prioritized Remediation Plan

**Goal:** every macOS account on every machine (spannagel + tl on the Mac Studio, andreasspannagel on the MacBook Air) runs an **identical** set of workspace daemons — git-sync, obsidian-headless, qmd-watch, qmd-http for all agents — all **ACTIVE**, login-independent, with full redundancy. Identical beats minimal: the Air runs system daemons for *uniformity*, not because it needs them.

**Synthesized from three dimension audits:** Homebrew multi-user hazard, Node-major / native-module fragility, and daemon-architecture migration. All live readings below are verified on the Air (`andreasspannagel`, node 26.3.0, ABI 147, umask 022) and cross-referenced against the Studio readings in the dimension reports.

---

## Execution status (live)

- **P0 DONE** — both generators trimmed to prep-only (workspaces also gains a positional agent filter and pins Node 23.11.0 for its `ob` calls; ob's better-sqlite3 12.6.2 is ABI-locked to Node 23). Committed alongside the deployer.
- **Air (`andreasspannagel`) DONE** — 8/9 agents × 4 daemons live as system LaunchDaemons (qmd-http ports 8391–8399), all `loaded` + qmd-http `responding`, superseded GUI LaunchAgents torn down. Idle footprint ≈ 1.8 GB on 24 GB (system 65% free).
- **`volki` BLOCKED on every Mac** — 2 Notion-export files carry invalid-UTF-8 (Latin-1) bytes in their names (`notion-contacts/Renée Ruch …`, `Sven Förstmann …`); APFS rejects them, aborting clone checkout, and a partial clone would make git-sync push mass deletions. Quarantined (not cloned). Fix at source: rename via `iconv -f latin1 -t utf8`, commit, push — then it deploys like the rest.
- **PENDING** — Studio accounts `spannagel` + `tl` (main done; the other 8 agents need prep + deploy). P1/P2/P3 hygiene remain deferred: the deployer's Node-23 pin works, so P2 is not blocking the rollout.

---

## The centerpiece already exists

A prior session wrote **`scripts/deploy-mac-daemons.sh`** (548 lines, currently **uncommitted**). It already solves the hard parts of the daemon-architecture dimension, cleanly:

- per-account daemon labels `com.openclaw.<svc>.<account>.<agent>` (no system-domain collision),
- account-indexed qmd-http ports `8191 + accountIndex*100 + agentPosition` (no cross-account bind collision; retires the hand-dodged 8291 PoC),
- per-account qmd-watch locks `/tmp/qmd-watch-<account>-<agent>.lock` (no cross-account `/tmp` deadlock),
- a **scan-based system-domain uninstall** (the old generators' uninstall only scanned `gui/N` and could never see system daemons),
- **teardown of the superseded GUI LaunchAgents** *after* the daemons are up (closes the double-run-on-login trap),
- `UserName` + `HOME` pinned in every plist (daemon runs **as** the account, writes the right home),
- correct main targeting: `workspace_dir_for` special-cases the default agent to `$HOME/main-workspace` (verified in `lib/agents.sh:55`) — main's `INDEX_PATH` is **not** mis-pointed under `dev/personal/workspaces`.

**This plan is built around adopting that script, not replacing it.** What remains is (a) making it safe to run by neutering the old generators, (b) committing it, (c) fixing the two fragility/hazard dimensions it doesn't touch, and (d) rolling it out to all accounts and agents.

**Leverage = (what it gates) × (blast radius if skipped).** That discriminator, not a fixed order, sets the sequence below.

---

## P0 — Commit the deployer **and** de-conflict the old generators (one indivisible change)

**Problem (one line):** the deploy script supersedes the plist halves of `setup-mac-workspaces.sh` and `setup-mac-qmd.sh`, but those generators still emit GUI LaunchAgents — the next prep run re-creates them and double-runs main's qmd/obsidian against a live system daemon = **sqlite corruption, not wasted CPU**.

**Why these ship together:** committing the deployer alone is *more* dangerous than not committing it (it invites prep re-runs that resurrect the agents); de-conflicting alone leaves the deployer uncommitted and un-reproducible. Ship as one commit.

**Clean fix:**

1. In `scripts/setup-mac-workspaces.sh` and `scripts/setup-mac-qmd.sh`, **keep** workspace prep + helper-script install (`workspace-git-sync-<agent>.sh`, `qmd-watch-<agent>.sh` in `~/.local/bin`, vault link, index build) — the deployer *reuses* those helpers — and **remove only** the plist emit / `launchctl bootstrap gui` / agent-load blocks. Surgical excision, not deletion. Leave a one-line pointer at the top of each: workspace + helper prep only; daemon deployment is `deploy-mac-daemons.sh`.
2. Commit the deployer + the trimmed generators together, staging **named files only** (the tree carries unrelated in-flight work — the opus-4-8 model bump in `group_vars/all.yml` and the heartbeat-shadow fix in `roles/agents/tasks/main.yml`):

```bash
cd ~/dev/personal/openclaw-infra
git checkout -b mac-system-daemons
git add scripts/deploy-mac-daemons.sh \
        scripts/setup-mac-workspaces.sh \
        scripts/setup-mac-qmd.sh \
        scripts/templates/qmd-watch-mac.sh.tmpl   # see P3
# NEVER `git add -A` here — it would sweep in the unrelated model/heartbeat work.
```

**Risk:** the trimmed generators must still install the helper scripts the deployer depends on; if a refactor drops a helper, the deployer warns-and-skips that service (it already guards each prereq) rather than failing loud — verify each service deploys after the trim, don't trust the skip path.

**One-time or encoded?** **Encoded.** This *is* the encoding of the migration into the repo. The migration currently exists only as hand-applied live plists on the Studio; this is what makes it reproducible.

---

## P1 — Homebrew multi-user: close the umask hole on `/opt/homebrew`

**Problem (one line):** a single shared `/opt/homebrew` prefix; runtime-born dirs (`var/homebrew/locks/`, new keg dirs, prefix root) are created under umask 022 → `0755`, so the *other* admin account can't create/delete entries and `brew` aborts (the `python@3.14` lock collision).

**Root cause — corrected from the original brief:** it is **not** a missing setgid bit. macOS BSD semantics already force group `admin` onto everything Homebrew creates (proven in isolation in the dimension report; documented in Homebrew's own FAQ), so group *ownership* is solved. setgid is redundant on macOS. The missing thing is the **group-write mode bit**, and the generator that strips it is **umask 022**. Files (`0644`) are irrelevant — multi-user contention is over **directory** entry-management rights only.

**Live state (Air, original/tight — the disease):**
```
drwxr-xr-x  andreasspannagel:admin  /opt/homebrew                     (0755, NOT g-writable)
drwxr-xr-x  andreasspannagel:admin  /opt/homebrew/var/homebrew/locks  (0755, NOT g-writable) ← the blocker
umask = 022
```
Studio is in a *different* state: already hand-patched (`locks` 0777, root 0775) — a band-aid that still decays on the next new-formula install because umask is unchanged. **The fix must be applied uniformly to all three homes**, or band-aid and disease coexist and drift.

**Clean fix — two parts:**

1. **One-time repair** of the current tree (root is fine for a chmod/chgrp sweep; Homebrew's no-sudo guard is about `brew` build scripts, not `chmod`):
```bash
sudo chgrp -R admin /opt/homebrew
sudo chmod -R g+w   /opt/homebrew   # fixes prefix root, Homebrew/Library, var/homebrew/locks
```
This restores the current tree but does **not** hold — the next `brew install <new-formula>` mints fresh `0755` dirs.

2. **Durability — inherited ACL scoped to the prefix** (preferred over a global umask change, see risk):
```bash
sudo chmod -R +a "group:admin allow \
read,write,execute,delete,add_file,add_subdirectory,file_inherit,directory_inherit" /opt/homebrew
# VERIFY before trusting: ls -le /opt/homebrew, then a real two-account `brew install <new formula>` test.
```

**Risk (keep these hedges — they are the honest signal):**
- The **ACL command syntax is reconstructed**, not run. Verify with `ls -le` and a two-account install before committing it as canonical.
- The alternative durability lever — global `umask 002` — has a **large blast radius**: the primary group on macOS is `staff` (gid 20, ~all local users), so global 002 makes *every* file you create anywhere group-writable by staff. That broad loosening is the decisive reason to prefer the path-scoped ACL.
- Whole-tree `g+w` also loosens `bin/`; both accounts already `sudo`, so on a 2-admin box this is minor, but name it: either admin can replace an installed binary without escalation.
- Multi-user Homebrew is **officially unsupported** ("does not work well in multi-user configurations" — Homebrew FAQ). This is a community workaround on an unsupported topology; a future Homebrew change could alter the inheritance/mode assumptions. `brew doctor` may flag non-standard perms/ACLs.

**One-time or encoded?** **Both.** One-time repair now on all three homes; the chgrp + ACL must be **encoded** as an ansible task / setup-script step in the Homebrew role so a fresh box and every re-provision re-applies it (otherwise it decays back to the band-aid-or-disease split).

---

## P2 — Native-module fragility: make the build survive a reinstall (the real durable fix)

**Problem (one line):** both ob and qmd load a V8-ABI `better_sqlite3.node`; ob's is a **hand-placed, unreproducible** artifact (12.6.2 has no node-26 prebuild and won't compile on node 26), and qmd's `better-sqlite3` isn't in bun's trust list — so any `pnpm/bun install` or store GC strips the `.node` and the daemon crashloops.

**Correction to the brief:** qmd is **not** shielded by bun. `~/.bun/bin/qmd`'s shebang is `#!/usr/bin/env node` with `exec node` primary — qmd runs under PATH node, so its better-sqlite3 is V8-ABI-keyed exactly like ob's. The fragility is **symmetric**.

**Two distinct failure classes — don't conflate:**
- **ABI-on-bump (better-sqlite3 only):** binary keyed to NODE_MODULE_VERSION; node major bump → `ERR_DLOPEN_FAILED` crashloop (the 2.1 GB-log incident). Only better-sqlite3 dies on a major; node-llama-cpp and tree-sitter are N-API/prebuild and survive.
- **Build-survives-reinstall (llama, tree-sitter, better-sqlite3's *build*):** lifecycle scripts skipped unless the dep is trusted; pnpm store GC can orphan an out-of-band `.node`.

### P2a — qmd: trust `better-sqlite3` in the bun global manifest

**Live state:** bun global `trustedDependencies` lists node-llama-cpp + tree-sitters but **not** `better-sqlite3` — which is why its `.node` was rebuilt-in-place and why every `bun install -g` reinstall restores a stale/wrong-ABI build.

```bash
cd ~/.bun/install/global
bun pm trust better-sqlite3   # adds to trustedDependencies + builds now
bun pm trusted                # verify better-sqlite3 appears
```

### P2b — ob: bump the dependency, don't freeze the runtime

**The Node-23 pin is the wrong axis.** The deploy script hard-pins Node 23.11.0 for obsidian (`OBSIDIAN_NODE_BIN`, lines 116–120) purely to satisfy ob's stale 12.6.2 binary — coupling a daemon's runtime to one transitive dep's stale ABI, and the binary is *still* hand-built. The durable fix is to **bump better-sqlite3 to 12.10.0** (same major → API-stable by semver; has a node-26 prebuild) and **stop installing globally** so a real manifest carries the pin.

`pnpm -g` has no project manifest you can put `overrides`/`onlyBuiltDependencies` in — that's why the global install is unfixable in place. Replace it with a managed pin-project (one per machine, templated into the repo):

```jsonc
// ~/.openclaw/ob-runtime/package.json
{
  "name": "ob-runtime", "private": true,
  "dependencies": { "obsidian-headless": "0.0.12" },
  "pnpm": {
    "overrides": { "better-sqlite3": "12.10.0" },
    "onlyBuiltDependencies": ["better-sqlite3"]
  }
}
```
```bash
echo "26.3.0" > ~/.openclaw/ob-runtime/.node-version
cd ~/.openclaw/ob-runtime && pnpm install
# artifact is now DERIVABLE: rm -rf node_modules && pnpm install reproduces it on any node major
# 12.10.0 prebuilds (24/25/26+). No hand-placed .node, no node-version coupling.
```

**Sequencing the runtime transition (the unverified step gates the flip):**
1. P2a + build the ob-runtime project (keep the Node-23 pin in the deploy script meanwhile — interim that the durable fix retires).
2. **Smoke-test `ob sync-status` against the 12.10.0 build** on one account.
3. Only then point the deploy script at the new binary — 3 edits: add `~/.openclaw/ob-runtime/node_modules/.bin/ob` to `resolve_ob_bin`'s candidate list, drop `OBSIDIAN_NODE_BIN` from `OBSIDIAN_PATH`, update the pin comment.

**Also fix the stale version pins** (so the repo stops disagreeing with reality):
- `ansible/group_vars/all.yml:142` — `qmd_version: "2.0.1"` is stale; installed is **2.5.3**. Bump it.
- `nodejs_major_version: 25` (all.yml) disagrees with live node 26 and mise's `26.3.0` pin. Pick one SoT.

**Risk (keep the hedges):**
- **12.10.0 as a drop-in for ob 0.0.12 is unverified.** Same major → low risk, but smoke-test before rolling to all agents (this is the gate above).
- ob declares `engines.node >=22.0.0` — node 26 satisfies it, no engine fight.

**One-time or encoded?** **Encoded.** P2a → a `bun pm trust better-sqlite3` task in `roles/qmd`. P2b → template the `ob-runtime` project + `.node-version` in `roles/obsidian-headless` / `setup-mac-workspaces.sh`, replacing the bare `pnpm install -g`; plist points at the project's `.bin/ob`. Plus a post-install `qmd status` / `ob sync-status` gate that fails loud on `ERR_DLOPEN_FAILED` instead of crashlooping.

---

## P3 — Clean win: namespace the qmd-watch lock at the template, not post-hoc

**Problem (one line):** `scripts/templates/qmd-watch-mac.sh.tmpl:12` emits `AGENT_LOCK="/tmp/qmd-watch-__AGENT_ID__.lock"` (no account), so the deploy script mutates it afterward with an in-place `sed` — a fragile post-hoc rewrite anchored on `^AGENT_LOCK=`.

**Clean fix:** add an account token to the template so the helper is born correctly namespaced, and drop `namespace_qmd_watch_lock` + its `sed` call from the deploy script:
```
AGENT_LOCK="/tmp/qmd-watch-__ACCOUNT__-__AGENT_ID__.lock"
```
The generator that renders the template substitutes `__ACCOUNT__` = `$(id -un)`. `EMBED_LOCK="/tmp/qmd-embed-global.lock"` stays machine-global by design (one 314 MB embedding model load at a time across all accounts) — leave it.

**Risk:** low. Verify the renderer substitutes the new token; a missed substitution would leave a literal `__ACCOUNT__` in the lock path (harmless but ugly — catch it in the deploy `--status` smoke run).

**One-time or encoded?** **Encoded** (template change), and it *removes* live mutation logic — net simplification.

---

## P4 — Roll out to all accounts and all agents (depends on P0–P3 being safe)

**Problem (one line):** coverage is 1/9 agents on the Studio (spannagel.main full; tl.main = qmd-http PoC only on 8291), 0 system daemons on the Air; 7–8 of 9 agents on spannagel are **not active at all**. "Identical + active everywhere" is far off.

**Why last:** rollout multiplies whatever state P0–P3 leave behind. Run it only after the deployer is safe to run (P0), tools are installable on both Studio accounts (P1), and a reinstall won't strip the `.node` (P2). The agent set is the 9 in `lib/agents.sh`: `main, manon, tl, henning, ph, nici, volki, franca, cama`.

**Clean fix:** run the deployer per account, per machine. It is idempotent and re-runnable; it tears down the matching GUI LaunchAgents in the same pass.
```bash
# On each account (spannagel, tl on Studio; andreasspannagel on Air), after prep has run:
~/dev/personal/openclaw-infra/scripts/deploy-mac-daemons.sh           # all agents
~/dev/personal/openclaw-infra/scripts/deploy-mac-daemons.sh --status  # verify loaded + health
```
Account → port-base index is already mapped (spannagel 0, tl 1, andreasspannagel 2), overridable via `OPENCLAW_ACCOUNT_INDEX`; unknown accounts hard-fail rather than silently colliding on base 8191.

**Risk:**
- **tl double-runs the instant a system daemon loads** while its matching GUI LaunchAgent is still live (tl is always-logged-in). The deployer's teardown handles this *for agents it knows*, but verify no stray tl agent for `main` survives the PoC era — and retire the 8291 PoC daemon explicitly (it's off the positional scheme; `--uninstall` for tl then redeploy).
- **spannagel's dormant GUI agents become a footgun on first console login** — they'd double-write main's index. The deployer's `rm` of the old plists neutralizes this; confirm `~/Library/LaunchAgents` is clean of `com.openclaw.*`/`com.qmd.*` after deploy.
- workspace/index prereqs must exist per agent or the deployer warns-and-skips — run prep first; a silent skip means that agent has no daemon.

**One-time or encoded?** Execution is per-machine one-time, but the *deployer itself* is the encoding. Ideally wrap the per-account invocation in the ansible play so re-provision re-runs it.

---

## Ordered rollout checklist

Run top-to-bottom. Each Studio step is done on **both** Studio accounts (spannagel, tl); Air steps on `andreasspannagel`.

**Phase A — encode (repo, on the Air; no live daemon impact yet)**
1. `git checkout -b mac-system-daemons`.
2. Trim `setup-mac-workspaces.sh` + `setup-mac-qmd.sh` to prep+helpers only (remove plist emit/load) — **P0**.
3. Namespace `qmd-watch-mac.sh.tmpl` with `__ACCOUNT__`; drop the deploy script's `namespace_qmd_watch_lock` sed — **P3**.
4. Bump `qmd_version` 2.0.1 → 2.5.3 and reconcile `nodejs_major_version` 25 → 26 in `group_vars/all.yml` — **P2**.
5. `git add` the **named** daemon/template/version files only (never `-A` — unrelated opus-4-8 + heartbeat work is in the tree). Commit.

**Phase B — fragility & hazard groundwork (each account, each machine)**
6. **P1** one-time repair: `sudo chgrp -R admin /opt/homebrew && sudo chmod -R g+w /opt/homebrew` on all three homes; then apply the inherited ACL, **verify with `ls -le` + a two-account `brew install`** before trusting it.
7. **P2a:** `cd ~/.bun/install/global && bun pm trust better-sqlite3 && bun pm trusted`.
8. **P2b:** create `~/.openclaw/ob-runtime` (manifest + `.node-version`), `pnpm install`, then **smoke-test `ob sync-status`** on the 12.10.0 build. Gate: do not proceed to step 9 until this passes.
9. Point the deploy script's `resolve_ob_bin` at `~/.openclaw/ob-runtime/node_modules/.bin/ob`; drop `OBSIDIAN_NODE_BIN` from `OBSIDIAN_PATH`. Re-commit.

**Phase C — deploy (each account, each machine; prep first)**
10. Run the trimmed prep (`setup-mac-workspaces.sh`, `setup-mac-qmd.sh`) so helpers/indexes exist for all agents.
11. Retire the tl 8291 PoC: `deploy-mac-daemons.sh --uninstall` as tl, confirm 8291 down.
12. `deploy-mac-daemons.sh` (all agents) on each account.
13. `deploy-mac-daemons.sh --status` — every service `loaded`, qmd-http `responding`. Confirm `~/Library/LaunchAgents` has no leftover `com.openclaw.*` / `com.qmd.*`.
14. Cross-account sanity: a single curl sweep of `localhost:8191–8199, 8291–8299, 8391+` shows each account's ports up and **no port served twice**.

**Phase D — verify redundancy & ship**
15. Reboot one machine (or `launchctl bootout`/`bootstrap` cycle); confirm daemons come back **without any GUI login** — the redundancy guarantee.
16. Re-run `pnpm install` in `ob-runtime` and `bun install -g` for qmd; confirm both daemons stay up (the `.node` is reproduced, not stripped) — proves P2 holds.
17. Open PR on `mac-system-daemons`; encode the per-account `deploy-mac-daemons.sh` invocation + P1/P2 tasks into the ansible plays so re-provision is fully reproducible.

---

## Caveats (carried forward verbatim from the dimension audits)

- **ACL syntax (P1) is reconstructed, not executed** — verify with `ls -le` and a two-account install before treating it as canonical.
- **better-sqlite3 12.10.0 as a drop-in for ob 0.0.12 is unverified** — same major, low risk, but the `ob sync-status` smoke-test (step 8) is a hard gate.
- **Multi-user Homebrew is officially unsupported** — this is a workaround on an unsupported topology; `brew doctor` may complain and a future Homebrew release could break the inheritance/mode assumptions.
- **Studio daemons' `launchctl list` "loaded" state** was not re-verified with root this session; "responding" (curl health) is proven for 8191/8291, which is the half that matters for "active."
- The tree carries **unrelated in-flight work** (opus-4-8 model bump, heartbeat-shadow fix) — stage daemon files by name, never `git add -A`.
