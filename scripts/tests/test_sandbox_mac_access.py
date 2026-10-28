#!/usr/bin/env python3
"""Run the sandbox role's Mac access tasks against a fake OpenClaw CLI and fixture files.

Since 2026.10, shell commands run in OpenClaw's sandbox, so the Mac key must reach
exactly the sandboxes of agents with `mac_access: true`, and leave when access ends.
"""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / 'ansible/roles/sandbox/tasks/mac-access.yml'
FAKE_OPENCLAW = '''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
config_path = os.path.join(os.environ['FIXTURE_ROOT'], 'state/openclaw.json')
config = json.load(open(config_path))
with open(os.path.join(os.environ['FIXTURE_ROOT'], 'calls'), 'a') as f:
    f.write(json.dumps(args) + '\\n')
action, key = args[1], args[2].split('.')
node = config
for part in key[:-1]:
    node = node.setdefault(part, {})
if args[:2] == ['config', 'set']:
    assert args[3] == '--json'
    node[key[-1]] = json.loads(args[4])
elif args[:2] == ['config', 'unset']:
    if key[-1] not in node:
        sys.exit('Config path not found')  # 2026.10.8 exits 1 here
    node.pop(key[-1])
else:
    sys.exit('unexpected openclaw call')
json.dump(config, open(config_path, 'w'))
'''


class SandboxMacAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ansible = shutil.which('ansible-playbook')
        if not cls.ansible:
            raise RuntimeError("Install the project's Ansible development dependency first")
        python = shlex.split(Path(cls.ansible).read_text().splitlines()[0].removeprefix('#!'))
        code = 'import json,sys,yaml; print(json.dumps([yaml.safe_load(open(p)) for p in sys.argv[1:]]))'
        cls.tasks, cls.defaults = json.loads(subprocess.run(
            [*python, '-c', code, str(TASKS), str(ROOT / 'ansible/group_vars/all.yml')],
            capture_output=True, text=True, check=True).stdout)

    def run_tasks(self, root, agents, mac_host='mac-air'):
        (root / 'bin').mkdir(exist_ok=True)
        for name, body in (('openclaw', FAKE_OPENCLAW),
                           ('getent', '#!/bin/sh\necho "100.64.0.7      STREAM $2"\n')):
            (root / 'bin' / name).write_text(body)
            (root / 'bin' / name).chmod(0o700)
        ssh = root / 'ssh'
        ssh.mkdir(exist_ok=True)
        (ssh / 'id_ed25519_openclaw_mac_air').write_text('fixture-private-key\n')
        (ssh / 'known_hosts_openclaw_mac_air').write_text('mac-air ssh-ed25519 AAAAfixture\n')
        # The Mac bootstrap (key creation, host pin, authorization) is the claude-cli role's own test.
        tasks = [t for t in self.tasks if 'ansible.builtin.include_role' not in t]
        tasks = json.loads(json.dumps(tasks).replace('/home/ubuntu/.openclaw', str(root / 'state'))
                           .replace('/home/ubuntu/.ssh', str(ssh))
                           .replace('"mac-ssh-config.j2"', json.dumps(str(TASKS.parents[1] / 'templates/mac-ssh-config.j2'))))
        variables = {**self.defaults, 'openclaw_agents': agents, 'openclaw_claude_cli_mac_host': mac_host,
                     'openclaw_claude_cli_mac_user': 'owner' if mac_host else '',
                     'sandbox_ssh_dir': str(root / 'state/sandbox-ssh')}
        play = [{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False, 'vars': variables, 'tasks': tasks}]
        (root / 'play.json').write_text(json.dumps(play))
        env = {'PATH': f"{root / 'bin'}:{os.environ['PATH']}", 'HOME': str(root), 'FIXTURE_ROOT': str(root),
               'ANSIBLE_NOCOLOR': '1', 'ANSIBLE_LOCAL_TEMP': str(root / 'local'), 'ANSIBLE_REMOTE_TEMP': str(root / 'remote')}
        return subprocess.run([self.ansible, '-i', 'localhost,', str(root / 'play.json')],
                              env=env, capture_output=True, text=True, timeout=120)

    def test_only_agents_with_mac_access_get_the_key_and_lose_it_with_access(self):
        with tempfile.TemporaryDirectory(prefix='sandbox-mac-') as tmp:
            root = Path(tmp)
            (root / 'state').mkdir()
            config = root / 'state/openclaw.json'
            config.write_text(json.dumps({'agents': {'entries': {'main': {}, 'tl': {'sandbox': {'mode': 'all'}}}}}))
            agents = [{'id': 'main', 'is_default': True, 'mac_access': True}, {'id': 'tl', 'is_default': False}]
            result = self.run_tasks(root, agents)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn('fixture-private-key', result.stdout + result.stderr)
            entries = json.loads(config.read_text())['agents']['entries']
            self.assertEqual(entries['main']['sandbox']['docker'], {
                'binds': [f'{root}/state/sandbox-ssh/main:/home/node/.ssh:ro'],
                'extraHosts': ['mac-air:100.64.0.7']})
            self.assertEqual(entries['tl'], {'sandbox': {'mode': 'all'}})
            folder = root / 'state/sandbox-ssh/main'
            self.assertEqual(sorted(p.name for p in folder.iterdir()),
                             ['config', 'id_ed25519_openclaw_mac', 'known_hosts_openclaw_mac'])
            self.assertEqual({p.name: p.stat().st_mode & 0o777 for p in folder.iterdir()},
                             dict.fromkeys(['config', 'id_ed25519_openclaw_mac', 'known_hosts_openclaw_mac'], 0o600))
            self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
            ssh_config = (folder / 'config').read_text()
            self.assertIn('Host mac-air\n', ssh_config)
            self.assertIn('StrictHostKeyChecking yes', ssh_config)
            self.assertFalse((root / 'state/sandbox-ssh/tl').exists())

            # A second run changes nothing.
            calls = (root / 'calls').read_text()
            result = self.run_tasks(root, agents)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertRegex(result.stdout, r'localhost\s+: ok=\d+\s+changed=0 ')
            self.assertEqual((root / 'calls').read_text(), calls)

            # Taking access away removes both the mount and the key copy.
            result = self.run_tasks(root, [{'id': 'main', 'is_default': True}, agents[1]])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            entries = json.loads(config.read_text())['agents']['entries']
            self.assertEqual(entries['main']['sandbox']['docker'], {})
            self.assertFalse(folder.exists())

    def test_access_removal_survives_a_half_removed_mount(self):
        # An interrupted run or a manual edit can leave only one of the two settings.
        with tempfile.TemporaryDirectory(prefix='sandbox-mac-') as tmp:
            root = Path(tmp)
            (root / 'state').mkdir()
            config = root / 'state/openclaw.json'
            config.write_text(json.dumps({'agents': {'entries': {'main': {'sandbox': {'docker': {
                'binds': ['/old:/home/node/.ssh:ro']}}}}}}))
            result = self.run_tasks(root, [{'id': 'main', 'is_default': True}])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(config.read_text())['agents']['entries']['main']['sandbox']['docker'], {})
            calls = [json.loads(line) for line in (root / 'calls').read_text().splitlines()]
            self.assertEqual(calls, [['config', 'unset', 'agents.entries.main.sandbox.docker.binds']])

    def test_no_mac_host_means_no_mac_access(self):
        with tempfile.TemporaryDirectory(prefix='sandbox-mac-') as tmp:
            root = Path(tmp)
            (root / 'state').mkdir()
            (root / 'state/openclaw.json').write_text(json.dumps({'agents': {'entries': {'main': {}}}}))
            result = self.run_tasks(root, [{'id': 'main', 'is_default': True, 'mac_access': True}], mac_host='')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse((root / 'calls').exists())
            self.assertEqual(list((root / 'state/sandbox-ssh').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
