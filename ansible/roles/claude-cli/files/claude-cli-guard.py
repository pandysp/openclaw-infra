#!/usr/bin/env python3
"""Own the C guard lifecycle; recover from administrator changes within a brief gap."""
from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time


class GuardChanged(RuntimeError):
    pass


def snapshot(table):
    """Kernel handles are allocation details, not policy; ignore nft version metadata."""
    def clean(value):
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items() if key != 'handle'}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    policy = []
    for family in ['inet', 'bridge']:
        raw = subprocess.check_output(['nft', '--json', 'list', 'table', family, table], text=True, timeout=5)
        policy.extend(clean(item) for item in json.loads(raw)['nftables'] if 'metainfo' not in item)
    return policy


def owner_alive(owner):
    if not re.fullmatch(r'[1-9][0-9]*:[0-9]+', owner):
        return False
    pid, started = owner.split(':')
    try:
        fields = Path('/proc', pid, 'stat').read_text().rsplit(')', 1)[1].split()
    except (FileNotFoundError, ProcessLookupError):
        return False
    return fields[0].upper() not in {'Z', 'X'} and fields[19] == started


def reap_artifacts(table, directory=Path('/tmp')):
    pattern = re.compile(r'openclaw-claude-cli-' + re.escape(table) + r'-([0-9]+)-([1-9][0-9]*)-([0-9]+)-[A-Za-z0-9_]+')
    for path in directory.glob('openclaw-claude-cli-' + table + '-*'):
        match = pattern.fullmatch(path.name)
        if not match:
            continue
        uid, pid, started = match.groups()
        try:
            metadata = path.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or path.resolve() != path or metadata.st_uid != int(uid):
                raise RuntimeError('ERROR: Refusing noncanonical CLI host artifact ownership; inspect the owned temporary directory')
            if not owner_alive(pid + ':' + started):
                shutil.rmtree(path)
        except FileNotFoundError:
            # The launcher may finish its own cleanup between listing and removal.
            continue


def stop_containers(table):
    try:
        ids = subprocess.check_output(['docker', 'ps', '-aq', '--filter', 'label=openclaw.claude-guard=' + table],
                                      text=True, timeout=5).split()
        if ids:
            subprocess.run(['docker', 'rm', '-f', *ids], check=True, stdout=subprocess.DEVNULL, timeout=15)
    finally:
        reap_artifacts(table)


def reap_orphans(table):
    try:
        rows = subprocess.check_output(['docker', 'ps', '-a', '--filter', 'label=openclaw.claude-guard=' + table,
            '--format', '{{.ID}} {{.Label "openclaw.claude-owner"}}'], text=True, timeout=5)
        orphans = [row.partition(' ')[0] for row in rows.splitlines() if not owner_alive(row.partition(' ')[2])]
        if orphans:
            subprocess.run(['docker', 'rm', '-f', *orphans], check=True, stdout=subprocess.DEVNULL, timeout=15)
    finally:
        reap_artifacts(table)


def ready():
    address = os.environ.get('NOTIFY_SOCKET')
    if address:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notify:
            notify.connect('\0' + address[1:] if address.startswith('@') else address)
            notify.sendall(b'READY=1')


def main(policy, table, state_path=None):
    if not re.fullmatch(r'[a-zA-Z0-9_]+', table):
        raise SystemExit('ERROR: Invalid container CLI guard table name')

    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    path = Path(state_path or ('/run/openclaw-claude-cli/' + table + '.lock'))
    state = None
    lifetime_fd = None

    def revoke_lifetime():
        nonlocal lifetime_fd
        if lifetime_fd is not None:
            descriptor, lifetime_fd = lifetime_fd, None
            # Closing any FD drops our POSIX lock. The other FD retains the
            # flock creation lease, including when the ready-byte write fails.
            os.close(descriptor)

    def admission(value):
        state.seek(0)
        state.write(value)
        state.truncate()
        state.flush()

    with ExitStack() as resources:
        try:
            path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            # Never unlink this inode: launchers and teardown must share it.
            state = resources.enter_context(open(path, 'r+', opener=lambda name, flags:
                os.open(name, flags | os.O_CREAT | os.O_NOFOLLOW, 0o644)))
            lifetime_fd = os.dup(state.fileno())
            resources.callback(revoke_lifetime)
            fcntl.lockf(state, fcntl.LOCK_EX | fcntl.LOCK_NB)
            admission('0')
            fcntl.flock(state, fcntl.LOCK_EX)
            try:
                stop_containers(table)
            finally:
                # Docker outages cannot prevent reinstating network protection.
                subprocess.run(['nft', '--file', policy], check=True, timeout=15)
            expected = snapshot(table)
            if stopping:
                return
            admission('1')
            fcntl.flock(state, fcntl.LOCK_UN)
            ready()
            while not stopping:
                time.sleep(1)
                if stopping:
                    break
                try:
                    actual = snapshot(table)
                except subprocess.CalledProcessError as error:
                    raise GuardChanged('ERROR: Container CLI network guard is missing; stopping affected runtimes before recovery') from error
                if actual != expected:
                    raise GuardChanged('ERROR: Container CLI network guard changed; stopping affected runtimes before recovery')
                # A launcher can be SIGKILLed. Independent ownership cleanup
                # must not rely on its finally.
                reap_orphans(table)
        finally:
            try:
                # Revoke before waiting, so new readers cannot starve teardown.
                # A failed write must not skip even the lock acquisition.
                if state is not None:
                    try:
                        admission('0')
                    finally:
                        try:
                            revoke_lifetime()
                        finally:
                            fcntl.flock(state, fcntl.LOCK_EX)
            finally:
                # Admission/locking failures cannot bypass containment recovery.
                try:
                    stop_containers(table)
                finally:
                    subprocess.run(['nft', '--file', policy], check=True, timeout=15)


if __name__ == '__main__':
    main(*sys.argv[1:])
