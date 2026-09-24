#!/usr/bin/env bash
# Native Linux/systemd proof on the run-owned VPS. Writes only the public staging workspace.
set -euo pipefail
: "${STAGING_HOST:?STAGING_HOST is required}"
[[ "$STAGING_HOST" =~ ^openclaw-staging-[0-9]+-[0-9]+\.[a-zA-Z0-9.-]+$ ]] || exit 1
tailscale ssh "ubuntu@$STAGING_HOST" "python3 -I - '${STAGING_HOST%%.*}'" <<'PY'
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request

expected = sys.argv[1]
assert subprocess.check_output(['hostname'], text=True).strip() == expected
os.environ['XDG_RUNTIME_DIR'] = '/run/user/1000'
home = Path.home()
service = 'workspace-git-sync-main.service'
timer = 'workspace-git-sync-main.timer'
unit = (home/'.config/systemd/user'/service).read_text()
repository = 'pandysp/openclaw-staging-workspace'
assert 'WORKSPACE_REPOSITORY=git@github-workspace-main:'+repository+'.git' in unit
with urllib.request.urlopen('https://api.github.com/repos/'+repository, timeout=20) as response:
    metadata = json.load(response)
assert metadata['private'] is False and metadata['full_name'] == repository

proxy_unit = (home/'.config/systemd/user/mcp-auth-proxy.service').read_text()
port = int(re.search(r'^Environment=CODEX_PROXY_PORT=([0-9]+)$', proxy_unit, re.M).group(1))
network = json.loads(subprocess.check_output(['docker','network','inspect','codex-proxy-net']))
gateway = network[0]['IPAM']['Config'][0]['Gateway']
# No workspace or credentials in the unrestricted control container.
subprocess.run(['docker','run','--rm','--network','bridge','--entrypoint','python3',
                'openclaw-workspace-sync:latest','-I','-c',
                f'import socket;socket.create_connection(({gateway!r},{port}),timeout=5).close()'],
               check=True, timeout=30, capture_output=True)
print('PASS: unrestricted control reaches the live host credential proxy')

workspace = home/'.openclaw/workspace'
hooks = workspace/'.git/hooks'
assert hooks.resolve().is_relative_to(workspace.resolve())
hook = hooks/'pre-commit'
probe = hooks/'phoenix-isolation.py'
proof = workspace/'.git/phoenix-isolation-proof'
marker = workspace/('.phoenix-isolation-'+expected+'.txt')
assert all(not path.exists() for path in [hook, probe, proof, marker])

def systemctl(*args):
    result = subprocess.run(['systemctl','--user',*args], timeout=660, capture_output=True, text=True)
    if result.returncode != 0:
        # Staging-only diagnostics: this unit touches the public staging workspace.
        sys.stderr.write(subprocess.run(['journalctl','--user','-u',service,'-n','40','--no-pager'],
                                        capture_output=True, text=True, timeout=30).stdout)
        raise SystemExit(f'systemctl {" ".join(args)} failed with {result.returncode}')

def remote_marker_status():
    request = urllib.request.Request('https://api.github.com/repos/'+repository+'/contents/'+marker.name,
                                     headers={'Cache-Control': 'no-cache'})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, None

systemctl('stop', timer)
created = []
try:
    # Drain any prior invocation before installing the controlled hook.
    systemctl('start', service)
    payload = f'''import os,pathlib,socket,subprocess
assert os.getuid()==1000
status=dict(line.split(':',1) for line in pathlib.Path('/proc/self/status').read_text().splitlines() if ':' in line)
assert status['NoNewPrivs'].strip()=='1'
assert all(int(status[k].strip(),16)==0 for k in ['CapInh','CapPrm','CapEff','CapBnd','CapAmb'])
assert not pathlib.Path('/var/run/docker.sock').exists()
assert not pathlib.Path('/home/ubuntu/.openclaw/openclaw.json').exists()
assert subprocess.run(['iptables','-P','OUTPUT','ACCEPT'],capture_output=True).returncode!=0
try:
 socket.create_connection(({gateway!r},{port}),timeout=2).close()
except (OSError,TimeoutError):
 pass
else:
 raise AssertionError('Hook reached the credential proxy')
pathlib.Path('.git/phoenix-isolation-proof').write_text('isolated')
'''
    for path, content in [(probe, payload), (hook, '#!/bin/sh\nexec python3 -I .git/hooks/phoenix-isolation.py\n'),
                          (marker, expected+'\n')]:
        with path.open('x') as file:
            file.write(content)
        created.append(path)
    hook.chmod(0o700)
    systemctl('start', service)
    assert proof.read_text() == 'isolated'
    status, remote = remote_marker_status()
    assert status == 200 and base64.b64decode(remote['content']).decode() == expected+'\n'
    print('PASS: actual sync unit executes hooks without host privileges or proxy access')
    print('PASS: repository-scoped SSH fetch/push and remote readback')
finally:
    for path in reversed(created):
        path.unlink(missing_ok=True)
    proof.unlink(missing_ok=True)
    systemctl('start', service)
    systemctl('start', timer)
assert remote_marker_status()[0] == 404, 'Marker removal did not reach the remote'
print('PASS: test files removed locally and remotely; the existing timer is restored')
PY
