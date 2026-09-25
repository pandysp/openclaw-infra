#!/bin/bash
# Trusted bootstrap only. No workspace content is executed until privileges drop.
set -euo pipefail
: "${WORKSPACE_AGENT_ID:?Missing agent ID}"
[[ "$WORKSPACE_AGENT_ID" =~ ^[a-zA-Z][a-zA-Z0-9_-]*$ ]] || exit 1
GITHUB_IP=$(python3 -I - <<'PY'
import ipaddress
import socket
address = socket.getaddrinfo('github.com', 22, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
if not ipaddress.ip_address(address).is_global:
    raise SystemExit('ERROR: GitHub must resolve to a public IPv4 address')
print(address)
PY
)

iptables -P INPUT DROP
iptables -P OUTPUT DROP
ip6tables -P INPUT DROP
ip6tables -P OUTPUT DROP
iptables -A OUTPUT -d "$GITHUB_IP/32" -p tcp --dport 22 -j ACCEPT
iptables -A INPUT -s "$GITHUB_IP/32" -p tcp --sport 22 -m conntrack --ctstate ESTABLISHED -j ACCEPT

# Host aliases stay compatible with existing repositories, without host ~/.ssh.
# This directory is a root-owned tmpfs, not writable by workspace Git hooks.
cat > /run/workspace-sync/ssh_config <<EOF
Host github.com github-workspace-${WORKSPACE_AGENT_ID}
    HostName ${GITHUB_IP}
    HostKeyAlias github.com
    User git
    IdentityFile /run/credentials/key
    IdentitiesOnly yes
    UserKnownHostsFile /run/credentials/known_hosts
    StrictHostKeyChecking yes
    BatchMode yes
    ConnectTimeout 15
    ForwardAgent no
EOF
chmod 644 /run/workspace-sync/ssh_config
export GIT_SSH_COMMAND='ssh -F /run/workspace-sync/ssh_config'
exec setpriv --reuid=1000 --regid=1000 --clear-groups --bounding-set=-all \
    --inh-caps=-all --ambient-caps=-all --no-new-privs "$@"
