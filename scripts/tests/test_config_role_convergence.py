#!/usr/bin/env python3
"""Run config-role tasks through Ansible against a fake OpenClaw CLI and fixture files.

Both bugs here only showed on a second provision of a live server: the adapter
was dropped from plugins.allow because an install-path check looked in the
pre-2026.5 location, and every Claude CLI session was reset to the primary
model because its 'claude-cli' provider was treated as foreign. The migration
also edited the session stores under a running gateway, which keeps them in
memory and rewrites them whole; it now stops the gateway around its writes.
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
MIGRATE_REPORT = 'Show which sessions were moved to the primary model'
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
    if node is None or leaf not in node:
        sys.stderr.write('Config path is valid but unset: ' + args[2] + '\\n'); sys.exit(1)
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


FAKE_SYSTEMCTL = '''#!/usr/bin/env python3
# Records each gateway stop/start with the session store as it is at that moment.
import json, os, sys
root = os.environ['FIXTURE_ROOT']
store = os.path.join(root, 'state/agents/main/sessions/sessions.json')
with open(os.path.join(root, 'systemctl'), 'a') as f:
    f.write(json.dumps([sys.argv[1:], open(store).read() if os.path.exists(store) else None]) + '\\n')
'''


class ConfigRoleConvergenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ansible = shutil.which('ansible-playbook')
        if not cls.ansible:
            raise RuntimeError("Install the project's Ansible development dependency first")
        python = shlex.split(Path(cls.ansible).read_text().splitlines()[0].removeprefix('#!'))
        code = 'import json,sys,yaml; print(json.dumps([yaml.safe_load(open(p)) for p in sys.argv[1:]]))'
        tasks, cls.defaults, cls.skill_environment = json.loads(subprocess.run(
            [*python, '-c', code, str(TASKS), str(ROOT / 'ansible/group_vars/all.yml'),
             str(ROOT / 'ansible/roles/config/tasks/skill-environment.yml')],
            capture_output=True, text=True, check=True).stdout)
        cls.temp_files = find_task(tasks, TEMP_FILES)
        cls.configure = find_task(tasks, CONFIGURE)
        cls.report = find_task(tasks, REPORT)
        cls.migrate = find_task(tasks, MIGRATE)
        cls.pin_models = find_task(tasks, 'Pin agent model allowlist with claude-cli runtime mapping')
        cls.migrate_report = find_task(tasks, MIGRATE_REPORT)
        assert cls.temp_files and cls.configure and cls.report and cls.migrate and cls.migrate_report, 'Config role tasks moved'

    def run_task(self, tasks, root, extra_vars=None):
        (root / 'bin').mkdir(exist_ok=True)
        fake = root / 'bin/openclaw'
        fake.write_text(FAKE_OPENCLAW)
        fake.chmod(0o700)
        systemctl = root / 'bin/systemctl'
        systemctl.write_text(FAKE_SYSTEMCTL)
        systemctl.chmod(0o700)
        # Point the tasks' fixed server and temp paths at this fixture tree.
        tasks = json.loads(json.dumps(tasks).replace('/home/ubuntu/.openclaw', str(root / 'state'))
                           .replace('/home/ubuntu/.config', str(root / 'config'))
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

    def test_model_map_is_compared_with_the_written_config(self):
        # Since 2026.9.8 `config get agents.defaults.models` returns the effective map,
        # with every model OpenClaw adds by default; only the file holds what we wrote.
        pin = self.pin_models
        assert pin, 'Config role tasks moved'
        with tempfile.TemporaryDirectory(prefix='config-role-') as tmp:
            root = Path(tmp)
            desired = {model: {'agentRuntime': {'id': 'claude-cli'}, **({'alias': alias} if alias else {})}
                       for model, alias in ((m, (v or {}).get('alias')) for m, v in self.defaults['openclaw_agent_models'].items())}
            (root / 'state').mkdir()
            (root / 'state/openclaw.json').write_text(json.dumps({'agents': {'defaults': {'models': desired}}}))
            effective = {**desired, 'anthropic/claude-added-by-default': {'agentRuntime': {'id': 'claude-cli'}}}
            (root / 'store.json').write_text(json.dumps({'agents': {'defaults': {
                'models': effective, 'modelPolicy': {'allow': list(self.defaults['openclaw_agent_models'])}}}}))
            result = self.run_task([pin], root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse((root / 'writes').exists(), (root / 'writes').read_text() if (root / 'writes').exists() else '')

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

    def test_whatsapp_settings_exist_only_with_a_whatsapp_agent(self):
        # From 2026.7.1, startup migrations after an upgrade install any configured but
        # missing channel plugin at its newest version, which can refuse an older core.
        whatsapp_agent = [{'id': 'main', 'is_default': True, 'deliver_channel': 'whatsapp',
                           'deliver_to': '+15555550100', 'deliver_type': 'dm'}]
        for agents, expected in ((None, None), (whatsapp_agent, {'healthMonitor': {'enabled': False}})):
            with self.subTest(whatsapp=bool(agents)), tempfile.TemporaryDirectory(prefix='config-role-') as tmp:
                root = Path(tmp)
                (root / 'store.json').write_text(json.dumps({'channels': {'whatsapp': {'healthMonitor': {'enabled': False}}}}))
                result = self.run_task([self.temp_files, self.configure], root,
                                       {'openclaw_agents': agents} if agents else None)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                store = json.loads((root / 'store.json').read_text())
                self.assertEqual(store.get('channels', {}).get('whatsapp'), expected)
                self.assertEqual('whatsapp' in store['plugins']['allow'], bool(agents))

    def test_elevenlabs_plugin_is_allowed_only_with_an_elevenlabs_key(self):
        for key in ('', 'fixture-elevenlabs-key'):
            with self.subTest(elevenlabs=bool(key)), tempfile.TemporaryDirectory(prefix='config-role-') as tmp:
                root = Path(tmp)
                result = self.run_task([self.temp_files, self.configure], root,
                                       {'elevenlabs_api_key': key, 'groq_api_key': 'fixture-groq-key'})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn('fixture-elevenlabs-key', result.stdout + result.stderr)
                writes = self.json_writes(root)
                allowed = writes['plugins.allow']
                self.assertEqual('elevenlabs' in allowed, bool(key))
                self.assertNotIn('openrouter', allowed)
                self.assertNotIn('groq', allowed)
                if key:
                    self.assertEqual(writes['tools.media.models'][0]['provider'], 'elevenlabs')
                self.assertNotIn('fixture-elevenlabs-key', (root / 'store.json').read_text())

    def test_exa_credential_reaches_gateway_environment_but_not_container_allowlist(self):
        self.assertNotIn('EXA_API_KEY', self.defaults['openclaw_claude_cli_skill_env'])
        # Run only the directory and file tasks; systemd is outside this fixture.
        tasks = [task for task in self.skill_environment
                 if task.get('ansible.builtin.file', {}).get('path') == '/home/ubuntu/.config/openclaw'
                 or task.get('ansible.builtin.copy', {}).get('dest') == '/home/ubuntu/.config/openclaw/claude-skills.env']
        self.assertEqual(len(tasks), 2)
        with tempfile.TemporaryDirectory(prefix='config-role-') as tmp:
            root = Path(tmp)
            for key in ('fixture-exa-key', ''):
                with self.subTest(configured=bool(key)):
                    result = self.run_task(tasks, root, {'exa_api_key': key})
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    env_file = root / 'config/openclaw/claude-skills.env'
                    values = dict(line.split('=', 1) for line in shlex.split(env_file.read_text()))
                    self.assertEqual(values['EXA_API_KEY'], key)
                    self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(env_file.parent.stat().st_mode & 0o777, 0o700)
                    self.assertNotIn('fixture-exa-key', result.stdout + result.stderr)

    def test_exa_replaces_grok_without_removing_x_search_credentials(self):
        with tempfile.TemporaryDirectory(prefix='config-role-') as tmp:
            root = Path(tmp)
            (root / 'store.json').write_text(json.dumps({
                'tools': {'web': {'search': {'provider': 'grok', 'enabled': False, 'timeoutSeconds': 30}}},
                'plugins': {'entries': {'xai': {'config': {'webSearch': {'apiKey': 'fixture-xai-key'}}}}},
            }))
            result = self.run_task([self.temp_files, self.configure], root,
                                   {'exa_api_key': 'fixture-exa-key', 'xai_api_key': 'fixture-xai-key'})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            store = json.loads((root / 'store.json').read_text())
            search = store['tools']['web']['search']
            self.assertEqual(search['provider'], 'exa')
            self.assertEqual(str(search['enabled']).lower(), 'true')
            self.assertEqual(int(search['timeoutSeconds']), 60)
            self.assertEqual(str(store['plugins']['entries']['exa']['enabled']).lower(), 'true')
            self.assertIn('exa', store['plugins']['allow'])
            self.assertIn('xai', store['plugins']['allow'])
            self.assertEqual(store['plugins']['entries']['xai']['config']['webSearch']['apiKey'], 'fixture-xai-key')
            self.assertNotIn('fixture-exa-key', (root / 'store.json').read_text())
            self.assertNotIn('fixture-exa-key', result.stdout + result.stderr)

    def test_an_absent_exa_key_disables_search_instead_of_falling_back_to_grok(self):
        with tempfile.TemporaryDirectory(prefix='config-role-') as tmp:
            root = Path(tmp)
            (root / 'store.json').write_text(json.dumps({
                'tools': {'web': {'search': {'provider': 'grok', 'enabled': True, 'timeoutSeconds': 60}}},
                'plugins': {'entries': {'exa': {'enabled': True}}},
            }))
            result = self.run_task([self.temp_files, self.configure], root,
                                   {'exa_api_key': '', 'xai_api_key': 'fixture-xai-key'})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            store = json.loads((root / 'store.json').read_text())
            search = store['tools']['web']['search']
            self.assertEqual(str(search['enabled']).lower(), 'false')
            self.assertNotIn('provider', search)
            self.assertNotIn('timeoutSeconds', search)
            self.assertNotIn('exa', store['plugins']['allow'])
            self.assertEqual(str(store['plugins']['entries']['exa']['enabled']).lower(), 'false')
            self.assertIn('xai', store['plugins']['allow'])

    def test_exa_and_keyless_firecrawl_converge_then_can_be_disabled(self):
        self.assertIs(self.defaults['openclaw_firecrawl_enabled'], True)
        with tempfile.TemporaryDirectory(prefix='config-role-') as tmp:
            root = Path(tmp)
            tasks = [self.temp_files, self.configure, self.report]
            extra = {'exa_api_key': 'fixture-exa-key'}
            first = self.run_task(tasks, root, extra)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            store = json.loads((root / 'store.json').read_text())
            fetch = store['tools']['web']['fetch']
            self.assertEqual(fetch['provider'], 'firecrawl')
            self.assertEqual(str(fetch['enabled']).lower(), 'true')
            self.assertIn('firecrawl', store['plugins']['allow'])
            self.assertEqual(str(store['plugins']['entries']['firecrawl']['enabled']).lower(), 'true')
            self.assertNotIn('apiKey', store['plugins']['entries']['firecrawl'].get('config', {}).get('webFetch', {}))
            (root / 'writes').unlink()
            second = self.run_task(tasks, root, extra)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertFalse((root / 'writes').exists(), (root / 'writes').read_text() if (root / 'writes').exists() else '')

            disabled = self.run_task(tasks, root, {'exa_api_key': '', 'openclaw_firecrawl_enabled': False})
            self.assertEqual(disabled.returncode, 0, disabled.stdout + disabled.stderr)
            store = json.loads((root / 'store.json').read_text())
            self.assertNotIn('provider', store['tools']['web']['search'])
            self.assertEqual(str(store['tools']['web']['search']['enabled']).lower(), 'false')
            self.assertNotIn('provider', store['tools']['web']['fetch'])
            # Disabling the hosted fallback must not disable ordinary page reads.
            self.assertEqual(str(store['tools']['web']['fetch']['enabled']).lower(), 'true')
            for plugin in ('exa', 'firecrawl'):
                self.assertNotIn(plugin, store['plugins']['allow'])
                self.assertEqual(str(store['plugins']['entries'][plugin]['enabled']).lower(), 'false')
            (root / 'writes').unlink()
            repeated = self.run_task(tasks, root, {'exa_api_key': '', 'openclaw_firecrawl_enabled': False})
            self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
            self.assertFalse((root / 'writes').exists(), (root / 'writes').read_text() if (root / 'writes').exists() else '')

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
            sessions.chmod(0o640)
            result = self.run_task([self.migrate, self.migrate_report], root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            migrated = json.loads(sessions.read_text())
            self.assertEqual(sessions.stat().st_mode & 0o777, 0o640)
            # The gateway rewrites the store from memory, so it is stopped while the
            # file changes and started again afterwards.
            calls = [json.loads(line) for line in (root / 'systemctl').read_text().splitlines()]
            self.assertEqual([c[0] for c in calls], [['--user', 'stop', 'openclaw-gateway'], ['--user', 'start', 'openclaw-gateway']])
            self.assertEqual(json.loads(calls[0][1]), original)
            self.assertEqual(json.loads(calls[1][1]), migrated)
            (root / 'systemctl').unlink()
            primary_provider, primary_model = self.defaults['openclaw_model_primary'].split('/', 1)
            self.assertEqual(migrated['runtime'], original['runtime'])
            self.assertEqual(migrated['canonical'], original['canonical'])
            self.assertEqual(migrated['foreign'], {'modelProvider': primary_provider, 'model': primary_model})
            self.assertRegex(result.stdout, r'localhost\s+: ok=2\s+changed=1 ')
            # The run names what it moved (agent and old provider), never content.
            self.assertIn('MIGRATED: main (openai)', result.stdout)
            # A second pass has nothing left to migrate.
            result = self.run_task([self.migrate, self.migrate_report], root)
            self.assertRegex(result.stdout, r'localhost\s+: ok=1\s+changed=0 ')
            self.assertFalse((root / 'systemctl').exists(), 'gateway touched with nothing to migrate')

    def test_an_unreadable_session_store_fails_before_the_gateway_stops(self):
        with tempfile.TemporaryDirectory(prefix='config-role-') as tmp:
            root = Path(tmp)
            sessions = root / 'state/agents/main/sessions/sessions.json'
            sessions.parent.mkdir(parents=True)
            sessions.write_text('{"truncated": ')
            result = self.run_task([self.migrate], root)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn('sessions.json', result.stdout + result.stderr)
            self.assertFalse((root / 'systemctl').exists())


if __name__ == '__main__':
    unittest.main()
