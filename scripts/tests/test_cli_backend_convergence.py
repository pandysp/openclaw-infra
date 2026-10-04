"""Run the shared backend writer through Ansible with the CLI boundary faked."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / 'ansible/roles/config/tasks/cli-backends.yml'
COMMAND = '/home/ubuntu/.openclaw/claude-cli-container'


class BackendConvergenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ansible = shutil.which('ansible-playbook')
        if not cls.ansible:
            raise RuntimeError('Install the project Ansible dependency')
        python = shlex.split(Path(cls.ansible).read_text().splitlines()[0].removeprefix('#!'))
        cls.tasks = json.loads(subprocess.check_output([*python, '-c',
            'import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))', str(TASKS)], text=True))

    def fixture(self, root, current):
        """Owned HOME, private TMPDIR and a fake OpenClaw CLI that records applied requests."""
        (root / '.openclaw').mkdir()
        (root / 'tmp').mkdir()
        protected = {'channels': {'discord': {'token': 'synthetic-private-value'}}}
        config = root / '.openclaw/openclaw.json'
        config.write_text(json.dumps({**protected, 'agents': {'defaults': {'cliBackends': current}}}))
        bin_dir = root / 'bin'
        bin_dir.mkdir()
        fake = bin_dir / 'openclaw'
        fake.write_text('''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
p = pathlib.Path.home() / '.openclaw/openclaw.json'
if sys.argv[1:] == ['config', 'unset', 'agents.defaults.cliBackends']:
    c = json.loads(p.read_text())
    c['agents']['defaults'].pop('cliBackends', None)
    p.write_text(json.dumps(c))
    with (pathlib.Path.home() / 'writes').open('a') as f: f.write('null\\n')
    sys.exit(0)
assert sys.argv[1:4] == ['config', 'set', '--batch-file']
# Interleave: a peer provisioner runs completely while this one is applying.
peer = os.environ.pop('PEER_PLAY', None)
if peer:
    subprocess.run([os.environ['ANSIBLE_PLAYBOOK'], '-i', 'localhost,', peer], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
c = json.loads(p.read_text())
batch = json.loads(pathlib.Path(sys.argv[4]).read_text())
assert batch[0]['path'] == 'agents.defaults.cliBackends'
c['agents']['defaults']['cliBackends'] = batch[0]['value']
p.write_text(json.dumps(c))
with (pathlib.Path.home() / 'writes').open('a') as f: f.write(json.dumps(batch[0]['value']) + '\\n')
''')
        fake.chmod(0o700)
        env = {**os.environ, 'HOME': str(root), 'PATH': str(bin_dir) + os.pathsep + os.environ['PATH'],
               'TMPDIR': str(root / 'tmp'), 'ANSIBLE_PLAYBOOK': self.ansible,
               'ANSIBLE_NOCOLOR': '1', 'ANSIBLE_LOCAL_TEMP': str(root / 'local'),
               'ANSIBLE_REMOTE_TEMP': str(root / 'remote')}
        return config, protected, env

    def play(self, root, name, desired, active=True):
        play = [{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                 'vars': {'_desired_cli_backends': desired, 'openclaw_claude_cli_command': COMMAND,
                          'openclaw_claude_cli_active': active},
                 'tasks': self.tasks, 'handlers': [{'name': 'restart openclaw-gateway',
                                                    'ansible.builtin.debug': {'msg': 'restart-boundary'}}]}]
        path = root / f'{name}.json'
        path.write_text(json.dumps(play))
        return path

    def run_play(self, root, path, env):
        result = subprocess.run([self.ansible, '-i', 'localhost,', str(path)],
                                env=env, capture_output=True, text=True, timeout=80)
        self.assertNotIn('synthetic-private-value', result.stdout + result.stderr)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # The run removed its own batch and nothing else is left behind.
        self.assertEqual(list((root / 'tmp').iterdir()), [])

    def writes(self, root):
        path = root / 'writes'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def check_backend(self, current, desired, expected, active=True):
        with tempfile.TemporaryDirectory(prefix='cli-backend-') as directory:
            root = Path(directory)
            config, protected, env = self.fixture(root, current)
            path = self.play(root, 'play', desired, active)
            self.run_play(root, path, env)
            after = json.loads(config.read_text())
            self.assertEqual(after['agents']['defaults'].get('cliBackends'), expected)
            self.assertEqual(after['channels'], protected['channels'])
            writes = self.writes(root)
            self.run_play(root, path, env)
            self.assertEqual(self.writes(root), writes)

    def test_concurrent_provisioners_apply_only_their_own_request(self):
        with tempfile.TemporaryDirectory(prefix='cli-backend-') as directory:
            root = Path(directory)
            config, _, env = self.fixture(root, None)
            first = {'claude-cli': {'modelArg': '--first'}}
            peer = {'claude-cli': {'modelArg': '--peer'}}
            # The peer stages, applies and cleans up between this run's staging and apply.
            self.run_play(root, self.play(root, 'first', first),
                          {**env, 'PEER_PLAY': str(self.play(root, 'peer', peer))})
            self.assertEqual(self.writes(root), [peer, first])
            self.assertEqual(json.loads(config.read_text())['agents']['defaults']['cliBackends'], first)

    def test_normal_configuration_preserves_active_c_command(self):
        self.check_backend({'claude-cli': {'command': COMMAND}}, {'claude-cli': {'modelArg': '--model'}},
                           {'claude-cli': {'command': COMMAND, 'modelArg': '--model'}})

    def test_normal_configuration_does_not_activate_c_early(self):
        desired = {'claude-cli': {'modelArg': '--model'}}
        self.check_backend(None, desired, desired)

    def test_explicit_replacement_command_wins(self):
        desired = {'claude-cli': {'command': '/usr/local/bin/claude'}}
        self.check_backend({'claude-cli': {'command': COMMAND}}, desired, desired)

    def test_deactivation_returns_to_native_defaults(self):
        self.check_backend({'claude-cli': {'command': COMMAND}}, {}, None, active=False)

    def test_deactivation_keeps_other_backend_settings(self):
        self.check_backend({'claude-cli': {'command': COMMAND, 'modelArg': '--model'}},
                           {'claude-cli': {'modelArg': '--model'}}, {'claude-cli': {'modelArg': '--model'}}, active=False)

    def test_native_host_without_overrides_stays_unset(self):
        self.check_backend(None, {}, None, active=False)

    def test_activation_is_idempotent(self):
        desired = {'claude-cli': {'command': COMMAND}}
        self.check_backend(None, desired, desired)


if __name__ == '__main__':
    unittest.main()
