#!/usr/bin/env bash
#
# Restart this user's provisioned services that still run a program replaced on disk.
#
# apt upgrades Node.js (and other packages) under running user services, but nothing
# restarts user services afterwards: the gateway kept running the old Node and its
# trust roots until something else restarted it. A replaced program shows up as
# "/proc/<pid>/exe -> <path> (deleted)". Every process in the unit's control group
# counts, so a stale child also restarts its service.
#
# Scope: running, long-lived services whose unit file lives in ~/.config/systemd/user
# (the ones provisioning installs). Oneshot units are left alone; restarting them
# would run their job again.
#
# Prints "RESTARTED <unit> <program>" per restart. Exits 1 if any service still runs
# a replaced program afterwards.
#
# Usage: restart-replaced-programs.sh   (as the service user, XDG_RUNTIME_DIR set)
# CGROUP_ROOT and UNIT_DIR exist for the regression test only.

set -euo pipefail

CGROUP_ROOT="${CGROUP_ROOT:-/sys/fs/cgroup}"
UNIT_DIR="${UNIT_DIR:-$HOME/.config/systemd/user}"

# Prints "<unit> <replaced program>" for every provisioned service running one.
stale_services() {
    local unit cgroup pid exe
    systemctl --user list-units --type=service --state=running --no-legend --plain |
        awk '{print $1}' |
        while read -r unit; do
            [[ $(systemctl --user show -P FragmentPath "$unit") == "$UNIT_DIR"/* ]] || continue
            [[ $(systemctl --user show -P Type "$unit") != oneshot ]] || continue
            cgroup=$(systemctl --user show -P ControlGroup "$unit")
            [[ -n $cgroup && -d $CGROUP_ROOT$cgroup ]] || continue
            while read -r pid; do
                # A process can exit between listing and reading; that is not staleness.
                exe=$(readlink "/proc/$pid/exe" 2>/dev/null) || continue
                if [[ $exe == *" (deleted)" ]]; then
                    echo "$unit ${exe% (deleted)}"
                    break
                fi
            done < <(find "$CGROUP_ROOT$cgroup" -name cgroup.procs -exec cat {} +)
        done
}

stale=$(stale_services)
while read -r unit program; do
    [[ -n $unit ]] || continue
    systemctl --user restart "$unit"
    echo "RESTARTED $unit $program"
done <<< "$stale"

remaining=$(stale_services)
if [[ -n $remaining ]]; then
    echo "ERROR: still running a replaced program after restart:" >&2
    echo "$remaining" >&2
    exit 1
fi
