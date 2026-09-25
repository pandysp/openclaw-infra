#!/usr/bin/env python3
"""Run config-role tasks through Ansible against a fake OpenClaw CLI and fixture files.

Both bugs here only showed on a second provision of a live server: the adapter
was dropped from plugins.allow because an install-path check looked in the
pre-2026.5 location, and every Claude CLI session was reset to the primary
model because its 'claude-cli' provider was treated as foreign.
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
TASKS = ROOT / 'ansible/roles/config/tasks/main.yml'
CONFIGURE = 'Configure gateway settings'
TEMP_FILES = 'Write secrets and config to temp files'
REPORT = 'Show which gateway settings were updated'
MIGRATE = 'Migrate session model provider to match primary model'
FAKE_OPENCLAW = '''#!/usr/bin/env python3
# A stateful stand-in for the OpenClaw config CLI: get/set/unset on a JSON
# store, recording every write, so a second pass can prove convergence.
import json, os, sys
args = sys.argv[1:]
root = os.environ['FIXTURE_ROOT']
store_path = os.path.join(root, 'store.json')
store = json.load(open(store_path)) if os.path.exists(store_path) else {}
def record():
    with open(os.path.join(root, 'writes'), 'a') as f:
        f.write(json.dumps(args) + '\\n')
def walk(key, create=False):
    node, parts = store, key.split('.')
    for part in parts[:-1]:
        if part not in node:
            if not create: return None, None
            node[part] = {}
        node = node[part]
    return node, parts[-1]
if args[:2] == ['config', 'get']:
    node, leaf = walk(args[2])
    if node is None or leaf not in node: sys.exit(1)
    value = node[leaf]
    print(value if isinstance(value, str) else json.dumps(value))
    sys.exit(0)
if args[:2] == ['config', 'set']:
    node, leaf = walk(args[2], create=True)
    node[leaf] = json.loads(args[args.index('--json') + 1]) if '--json' in args else args[3]
    record(); json.dump(store, open(store_path, 'w')); sys.exit(0)
if args[:2] == ['config', 'unset']:
    node, leaf = walk(args[2])
    if node is None or leaf not in node:
        print('Config path not found: ' + args[2]); sys.exit(1)
    del node[leaf]; record(); json.dump(store, open(store_path, 'w')); sys.exit(0)
sys.exit('unexpected openclaw call: ' + ' '.join(args))
'''


def find_task(tasks, name):
    for task in tasks:
        if task.get('name') == name:
            return task
        found = find_task(task.get('block', []), name)
        if found:
            return found
    return None


class ConfigRoleConvergenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ansible = shutil.which('ansible-playbook')
        if not cls.ansible:
            raise RuntimeError("Install the project's Ansible development dependency first")
        python = shlex.split(Path(cls.ansible).read_text().splitlines()[0].removeprefix('#!'))
        code = 'import json,sys,yaml; print(json.dumps([yaml.safe_load(open(p)) for p in sys.argv[1:]]))'
        tasks, cls.defaults = json.loads(subprocess.run(
            [*python, '-c', code, str(TASKS), str(ROOT / 'ansible/group_vars/all.yml')],
            capture_output=True, text=True, check=True).stdout)
        cls.temp_files = find_task(tasks, TEMP_FILES)
        cls.configure = find_task(tasks, CONFIGURE)
        cls.report = find_task(tasks, REPORT)
        cls.migrate = find_task(tasks, MIGRATE)
        assert cls.temp_files and cls.configure and cls.report and cls.migrate, 'Config role tasks moved'

    def run_task(self, tasks, root, extra_vars=None):
        (root / 'bin').mkdir(exist_ok=True)
        fake = root / 'bin/openclaw'
        fake.write_text(FAKE_OPENCLAW)
        fake.chmod(0o700)
        # Point the tasks' fixed server and temp paths at this fixture tree.
        tasks = json.loads(json.dumps(tasks).replace('/home/ubuntu/.openclaw', str(root / 'state'))
                           .replace('/tmp/ansible-', str(root / 'tmp-ansible-')))
        for task in tasks:
            task.pop('notify', None)
        variables = {**self.defaults, 'gateway_token': 'fixture-gateway', 'openclaw_agents': [{'id': 'main', 'is_default': True, 'deliver_channel': 'telegram',
                                         'deliver_to': '', 'deliver_type': 'dm'}],
                     **(extra_vars or {})}
        play = [{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                 'vars': variables, 'tasks': tasks}]
        (root / 'play.json').write_text(json.dumps(play))
        env = {'PATH': f"{root / 'bin'}:{os.environ['PATH']}", 'HOME': str(root),
               'FIXTURE_ROOT': str(root), 'ANSIBLE_NOCOLOR': '1',
               'ANSIBLE_LOCAL_TEMP': str(root / 'local'), 'ANSIBLE_REMOTE_TEMP': str(root / 'remote')}
        return subprocess.run([self.ansible, '-i', 'localhost,', str(root / 'play.json')],
                              env=env, capture_output=True, text=True, timeout=120)

    def json_writes(self, root):
        writes = {}
        for line in (root / 'writes').read_text().splitlines():
            parts = json.loads(line)
            if parts[:2] == ['config', 'set'] and '--json' in parts:
                writes[parts[2]] = json.loads(parts[parts.index('--json') + 1])
        return writes

    def test_a_declared_adapter_is_allowed_before_and_after_installation(self):
        # Nothing is installed in this fixture: the policy must not depend on an
        # install location (OpenClaw 2026.6 only warns about a missing allowed plugin).
        with tempfile.TemporaryDirectory(prefix='config-role-') as tmp:
            root = Path(tmp)
            result = self.run_task([self.temp_files, self.configure, self.report], root,
                                   {'openclaw_mcp_adapter': self.defaults['openclaw_mcp_adapter']})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            # The run names the keys it wrote, never their values.
            self.assertRegex(result.stdout, r'UPDATED:.* plugins\.allow')
            self.assertNotIn('fixture-gateway', result.stdout + result.stderr)
            writes = self.json_writes(root)
            self.assertIn('openclaw-mcp-adapter', writes['plugins.allow'])
            self.assertIn('openclaw-mcp-adapter', writes['tools.sandbox.tools.allow'])
            self.assertIn('group:plugins', writes['tools.sandbox.tools.allow'])
            self.assertEqual(writes['tools.alsoAllow'], ['group:plugins'])

    def test_a_second_pass_writes_nothing(self):
        # Includes the empty custom-model set staging uses, whose models object
        # has no 'mode' key: the old existence check rewrote it on every run.
        for custom_models in (self.defaults['openclaw_custom_models'], {}):
            with self.subTest(custom_models=bool(custom_models)), tempfile.TemporaryDirectory(prefix='config-role-') as tmp:
                root = Path(tmp)
                tasks = [self.temp_files, self.configure, self.report]
                extra = {'openclaw_custom_models': custom_models}
                first = self.run_task(tasks, root, extra)
                self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
                (root / 'writes').unlink()
                second = self.run_task(tasks, root, extra)
                self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
                self.assertFalse((root / 'writes').exists(), (root / 'writes').read_text() if (root / 'writes').exists() else '')
                self.assertNotIn('UPDATED:', second.stdout)

    def test_no_adapter_is_allowed_when_none_is_declared(self):
        with tempfile.TemporaryDirectory(prefix='config-role-') as tmp:
            root = Path(tmp)
            defaults = dict(self.defaults)
            defaults.pop('openclaw_mcp_adapter', None)
            self.defaults, saved = defaults, self.defaults
            try:
                result = self.run_task([self.temp_files, self.configure], root)
            finally:
                self.defaults = saved
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            writes = self.json_writes(root)
            self.assertNotIn('openclaw-mcp-adapter', writes['plugins.allow'])
            self.assertNotIn('group:plugins', writes['tools.sandbox.tools.allow'])
            self.assertNotIn('tools.alsoAllow', writes)

    def test_sessions_on_the_primary_models_runtime_are_left_alone(self):
        with tempfile.TemporaryDirectory(prefix='config-role-') as tmp:
            root = Path(tmp)
            sessions = root / 'state/agents/main/sessions/sessions.json'
            sessions.parent.mkdir(parents=True)
            original = {
                'runtime': {'modelProvider': 'claude-cli', 'model': 'claude-sonnet-4-6'},
                'canonical': {'modelProvider': 'anthropic', 'model': 'claude-sonnet-4-6'},
                'foreign': {'modelProvider': 'openai', 'model': 'gpt-5'},
            }
            sessions.write_text(json.dumps(original))
            result = self.run_task([self.migrate], root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            migrated = json.loads(sessions.read_text())
            primary_provider, primary_model = self.defaults['openclaw_model_primary'].split('/', 1)
            self.assertEqual(migrated['runtime'], original['runtime'])
            self.assertEqual(migrated['canonical'], original['canonical'])
            self.assertEqual(migrated['foreign'], {'modelProvider': primary_provider, 'model': primary_model})
            self.assertRegex(result.stdout, r'localhost\s+: ok=1\s+changed=1 ')
            # A second pass has nothing left to migrate.
            result = self.run_task([self.migrate], root)
            self.assertRegex(result.stdout, r'localhost\s+: ok=1\s+changed=0 ')


if __name__ == '__main__':
    unittest.main()
