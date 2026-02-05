#!/usr/bin/env python3
"""Dynamic Ansible inventory that reads the Tailscale hostname from Pulumi
and resolves it to a Tailscale IP via `tailscale status --json`.

Falls back to MagicDNS FQDN if the IP cannot be resolved.
"""

import json
import os
import subprocess
import sys


def run(cmd):
    """Run a command and return stdout, or None on failure."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=15)
        return result.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None


def get_tailscale_hostname():
    """Read tailscaleHostname from Pulumi stack output."""
    raw = run(["pulumi", "stack", "output", "tailscaleHostname", "-C", os.path.join(os.path.dirname(__file__), "..", "..", "pulumi")])
    if raw:
        return raw.strip().strip('"')
    return None


def resolve_tailscale_ip(hostname):
    """Resolve a Tailscale hostname to its IP via `tailscale status --json`."""
    raw = run(["tailscale", "status", "--json"])
    if not raw:
        return None
    try:
        data = json.loads(raw)
        for peer in (data.get("Peer") or {}).values():
            if peer.get("HostName", "").lower() == hostname.lower():
                addrs = peer.get("TailscaleIPs", [])
                # Prefer IPv4
                for addr in addrs:
                    if "." in addr:
                        return addr
                if addrs:
                    return addrs[0]
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def get_magic_dns_suffix():
    """Get the MagicDNS suffix from tailscale status."""
    raw = run(["tailscale", "status", "--json"])
    if not raw:
        return None
    try:
        data = json.loads(raw)
        suffix = data.get("MagicDNSSuffix", "")
        if suffix:
            return suffix
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--list":
        hostname = get_tailscale_hostname()
        if not hostname:
            print(json.dumps({"_meta": {"hostvars": {}}}))
            sys.exit(0)

        # Try to resolve to Tailscale IP
        ip = resolve_tailscale_ip(hostname)

        if ip:
            ansible_host = ip
        else:
            # Fallback to MagicDNS FQDN
            suffix = get_magic_dns_suffix()
            if suffix:
                ansible_host = f"{hostname}.{suffix}"
            else:
                ansible_host = hostname

        inventory = {
            "openclaw": {
                "hosts": [ansible_host],
            },
            "_meta": {
                "hostvars": {
                    ansible_host: {
                        "ansible_user": "ubuntu",
                        "ansible_ssh_common_args": "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
                    }
                }
            },
        }
        print(json.dumps(inventory, indent=2))

    elif len(sys.argv) == 2 and sys.argv[1] == "--host":
        print(json.dumps({}))
    else:
        print(json.dumps({"_meta": {"hostvars": {}}}))


if __name__ == "__main__":
    main()
