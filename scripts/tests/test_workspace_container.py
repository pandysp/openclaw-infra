#!/usr/bin/env python3
"""Real Docker isolation checks; run with WORKSPACE_CONTAINER_TESTS=1.

Native host-service and systemd checks additionally run on the Phoenix VPS.
"""
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]
IMAGE = 'openclaw-workspace-sync:test'
UNIT_TEMPLATE = ROOT/'ansible/roles/workspace/templates/workspace-git-sync.service.j2'


def unit_run_flags(workspace, key, known_hosts):
    """The deployed unit's `docker run` flags, so this test cannot drift from it."""
    text = UNIT_TEMPLATE.read_text()
    exec_start = text[text.index('ExecStart=')+len('ExecStart='):].split('\n\n')[0]
    for placeholder, value in {
        '{{ item.agent_id }}': 'test', '{{ item.workspace_dir }}': str(workspace),
        '/home/ubuntu/.ssh/{{ item.key_file }}': str(key),
        '/home/ubuntu/.ssh/workspace-github-known-hosts': str(known_hosts),
        "{{ item.repo_url | replace('github.com', item.ssh_alias) }}": 'git@github-workspace-test:fixture/repo.git',
        "{{ '1' if workspace_initializing | default(false) else '0' }}": '0',
    }.items():
        assert placeholder in exec_start, placeholder
        exec_start = exec_start.replace(placeholder, value)
    assert '{{' not in exec_start, exec_start
    words = shlex.split(exec_start.replace('\\\n', ' '))
    assert words[:2] == ['/usr/bin/docker', 'run'] and words[-1] == 'openclaw-workspace-sync:latest'
    return [word for word in words[2:-1] if word not in ('--rm',)] + ['--rm']
PROBE = '''
import json, os, pathlib, socket, subprocess
status = dict(line.split(':', 1) for line in pathlib.Path('/proc/self/status').read_text().splitlines() if ':' in line)
assert os.getuid() == 1000
assert all(int(status[key].strip(), 16) == 0 for key in ['CapInh','CapPrm','CapEff','CapBnd','CapAmb'])
assert status['NoNewPrivs'].strip() == '1'
assert subprocess.run(['iptables','-P','OUTPUT','ACCEPT'],capture_output=True).returncode != 0
try:
 os.setuid(0)
except PermissionError:
 pass
else:
 raise AssertionError('Regained root')
assert not pathlib.Path('/var/run/docker.sock').exists()
assert not pathlib.Path('/workspace/root-code-ran').exists()
assert pathlib.Path('/run/credentials/key').read_text() == 'fixture-repository-key'
try:
 pathlib.Path('/run/credentials/key').write_text('overwrite')
except OSError:
 pass
else:
 raise AssertionError('Credential mount is writable')
peer = os.environ['TEST_PEER']
try:
 socket.create_connection((peer,8080),timeout=1).close()
except (TimeoutError,OSError):
 pass
else:
 raise AssertionError('Reached the known-live peer service')
config = pathlib.Path('/run/workspace-sync/ssh_config').read_text()
assert 'StrictHostKeyChecking yes' in config and 'HostKeyAlias github.com' in config
address = next(line.split()[1] for line in config.splitlines() if line.strip().startswith('HostName '))
with socket.create_connection((address,22),timeout=5) as connection:
 assert connection.recv(100).startswith(b'SSH-')
print(json.dumps({'uid':os.getuid(),'capabilities':'none','peer_blocked':True,'github_ssh_reachable':True,'credential_mount':'read-only'}))
'''


@unittest.skipUnless(os.environ.get('WORKSPACE_CONTAINER_TESTS') == '1',
                     'Run WORKSPACE_CONTAINER_TESTS=1 python3 -m unittest scripts.tests.test_workspace_container')
class WorkspaceContainerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run(['docker','build','-t',IMAGE,str(ROOT/'ansible/roles/workspace/files')],
                                capture_output=True,text=True,timeout=300)
        if result.returncode:
            raise RuntimeError(result.stdout[-3000:]+result.stderr[-3000:])

    def docker(self, *args):
        result = subprocess.run(['docker',*args], capture_output=True,text=True,timeout=45)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        return result.stdout.strip()

    def test_untrusted_workspace_cannot_bypass_bootstrap_or_reach_live_peer(self):
        network = 'workspace-sync-test-'+uuid.uuid4().hex[:12]
        self.docker('network','create',network)
        self.addCleanup(self.docker,'network','rm',network)
        peer = self.docker('run','--rm','-d','--network',network,'--entrypoint','python3',IMAGE,
                           '-I','-m','http.server','8080','--bind','0.0.0.0')
        self.addCleanup(self.docker,'rm','-f',peer)
        address = self.docker('inspect','--format','{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}',peer)
        # Same network and target, but no firewall: establish that the service
        # really is reachable before calling a failed connection isolation proof.
        control = '''import socket,time
for attempt in range(30):
 try:
  socket.create_connection(("'''+address+'''",8080),timeout=1).close();break
 except OSError:
  if attempt==29:raise
  time.sleep(.1)
print('known-live peer reached')
'''
        self.assertIn('known-live peer reached',self.docker('run','--rm','--network',network,
            '--user','1000:1000','--cap-drop','ALL','--entrypoint','python3',IMAGE,'-I','-c',control))
        with tempfile.TemporaryDirectory(prefix='workspace-container-') as temporary:
            root = Path(temporary)
            workspace = root/'workspace';workspace.mkdir(mode=0o777);workspace.chmod(0o777)
            poison = "import os\nopen('/workspace/root-code-ran','w').write(str(os.getuid()))\nraise RuntimeError('Untrusted module executed')\n"
            for module in ['ipaddress.py','socket.py']:
                (workspace/module).write_text(poison)
            key = root/'key';key.write_text('fixture-repository-key');key.chmod(0o644)
            known_hosts = root/'known_hosts';known_hosts.write_text('github.com ssh-ed25519 AAAAfixture\n')
            flags = unit_run_flags(workspace, key, known_hosts)
            flags[flags.index('--network')+1] = network
            flags[flags.index('--name')+1] = 'workspace-sync-test-'+uuid.uuid4().hex[:12]
            # The deployed unit trusts the image WORKDIR; an agent-writable cwd
            # is the hostile case the root bootstrap must survive.
            result = self.docker('run',*flags,'--workdir','/workspace',
                '--env',f'TEST_PEER={address}',IMAGE,'python3','-I','-c',PROBE)
            report = json.loads(result)
            self.assertTrue(report['peer_blocked'])
            self.assertTrue(report['github_ssh_reachable'])
            self.assertFalse((workspace/'root-code-ran').exists())


if __name__ == '__main__':
    unittest.main()
