#!/usr/bin/env python3
"""Consume the exact peer and authenticated host keys prepared by provision.sh."""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--list', action='store_true')
    mode.add_argument('--host')
    args = parser.parse_args()
    if args.host:
        print('{}')
        return

    host = os.environ.get('OPENCLAW_SSH_HOST', '')
    keys = os.environ.get('OPENCLAW_SSH_KNOWN_HOSTS', '')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', host) or not keys:
        sys.exit('Inventory error: run scripts/provision.sh to resolve the host and authenticated SSH keys')
    path = Path(keys)
    try:
        info = path.lstat()
        valid = stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600
        lines = path.read_text().splitlines() if valid else []
    except OSError:
        sys.exit('Inventory error: authenticated SSH host-key file is unreadable')
    if not lines or not all(re.fullmatch(re.escape(host) + r' (ssh-ed25519|ssh-rsa|ecdsa-sha2-[A-Za-z0-9-]+) [A-Za-z0-9+/=]+', line) for line in lines):
        sys.exit('Inventory error: expected a private host-key file containing only the resolved peer')

    # The filename crosses both shell splitting and OpenSSH's option parser.
    ssh_args = '-o StrictHostKeyChecking=yes -o ' + shlex.quote(
        'UserKnownHostsFile=' + json.dumps(str(path), ensure_ascii=False))
    ssh_args += ' -o GlobalKnownHostsFile=/dev/null -o UpdateHostKeys=no'
    ssh_args += ' -o ProxyCommand=' + shlex.quote('tailscale nc %h %p')
    print(json.dumps({
        'openclaw': {'hosts': [host]},
        '_meta': {'hostvars': {host: {
            'ansible_user': 'ubuntu',
            'ansible_host_key_checking': True,
            'ansible_ssh_common_args': ssh_args,
            # Never reuse a connection authenticated under a different policy.
            'ansible_ssh_args': '-o ControlMaster=no -o ControlPath=none',
        }}},
    }, indent=2))


if __name__ == '__main__':
    main()
