#!/usr/bin/env python3
"""Run config-role tasks through Ansible against a fake OpenClaw CLI and fixture files.

Both bugs here only showed on a second provision of a live server: the adapter
was dropped from plugins.allow because an install-path check looked in the
pre-2026.5 location, and every Claude CLI session was reset to the primary
model because its 'claude-cli' provider was treated as foreign. The migration
also edited the session stores under a running gateway, which keeps them in
memory and rewrites them whole; it now stops the gateway around its writes.
"""
import http.server
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / 'ansible/roles/config/tasks/main.yml'
CONFIGURE = 'Configure gateway with secret cleanup'
TEMP_FILES = 'Write managed gateway settings to a private file'
REPORT = 'Show which gateway settings were updated'
MIGRATE_REPORT = 'Show which sessions were moved to the primary model'
MIGRATE = 'Migrate session model provider to match primary model'
FAKE_OPENCLAW = '''#!/usr/bin/env python3
# Boundary stand-in for one validated OpenClaw patch; no config get is allowed.
import json, os, subprocess, sys
args = sys.argv[1:]
root = os.environ['FIXTURE_ROOT']
store_path = os.path.join(root, 'store.json')
store = json.load(open(store_path))
if args == ['plugins', 'list', '--json']:
    if os.path.exists(os.path.join(root, 'inventory-fails')):
        sys.exit('fixture inventory failure')
    # JavaScript trim matches the boundary policy, including U+FEFF. Production
    # asks OpenClaw; it must not maintain its own copied ID normalizer.
    code = "const fs=require('node:fs'),p=JSON.parse(fs.readFileSync(0,'utf8')).plugins||{}; let e={}; for(const [k,v] of Object.entries(p.entries||{}))if(k.trim().toLowerCase()==='anthropic')e={...e,...v}; const enabled=p.enabled!==false && !(p.deny||[]).some(k=>k.trim().toLowerCase()==='anthropic') && (!(p.allow||[]).length || p.allow.some(k=>k.trim().toLowerCase()==='anthropic')) && e.enabled===true; console.log(JSON.stringify({plugins:[{id:'anthropic',enabled}]}));"
    sys.exit(subprocess.run(['node', '-e', code], input=json.dumps(store), text=True).returncode)
if args[:2] != ['config', 'patch']:
    sys.exit('unexpected openclaw call: ' + ' '.join(args))
if not os.path.exists(os.path.join(root, 'gateway-stopped')):
    sys.exit('Gateway must be stopped before config writes')
patch = json.load(open(args[args.index('--file') + 1]))
replace = {args[i + 1] for i, value in enumerate(args) if value == '--replace-path'}
def merge(target, value, path=''):
    for key, child in value.items():
        name = path + '.' + key if path else key
        if child is None:
            target.pop(key, None)
        elif isinstance(child, dict) and name not in replace:
            if not isinstance(target.get(key), dict):
                target[key] = {}
            merge(target[key], child, name)
        else:
            target[key] = child
if os.path.exists(os.path.join(root, 'patch-fails')):
    sys.exit('fixture-private-key: rejected patch')
merge(store, patch)
with open(os.path.join(root, 'writes'), 'a') as f:
    f.write(json.dumps({'args': args, 'patch': patch}) + '\\n')
json.dump(store, open(store_path, 'w'))
if os.path.exists(os.path.join(root, 'post-write-fails')):
    sys.exit('Config health-state write failed: fixture-private-key')
print('Applied config patch')
'''


def find_task(tasks, name):
    for task in tasks:
        if task.get('name') == name:
            return task
        for section in ('block', 'rescue', 'always'):
            found = find_task(task.get(section, []), name)
            if found:
                return found
    return None


FAKE_SYSTEMCTL = '''#!/usr/bin/env python3
# Records each gateway stop/start with the session store as it is at that moment.
import json, os, sys
root = os.environ['FIXTURE_ROOT']
marker = os.path.join(root, 'gateway-stopped')
for action in ('stop', 'start'):
    if action in sys.argv and os.path.exists(os.path.join(root, action + '-fails')):
        sys.exit('fixture ' + action + ' failure')
if 'stop' in sys.argv:
    open(marker, 'w').close()
elif 'start' in sys.argv and os.path.exists(marker):
    os.unlink(marker)
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
        tasks, gateway_tasks, cls.defaults, cls.skill_environment = json.loads(subprocess.run(
            [*python, '-c', code, str(TASKS), str(TASKS.with_name('gateway-config.yml')),
             str(ROOT / 'ansible/group_vars/all.yml'),
             str(ROOT / 'ansible/roles/config/tasks/skill-environment.yml')],
            capture_output=True, text=True, check=True).stdout)
        cls.temp_files = find_task(gateway_tasks, TEMP_FILES)
        cls.configure = find_task(gateway_tasks, CONFIGURE)
        cls.report = find_task(gateway_tasks, REPORT)
        cls.migrate = find_task(tasks, MIGRATE)
        cls.auth_profiles = find_task(tasks, 'Enforce agent auth-profiles — setup token present, api-key profile absent')
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
        docker = root / 'bin/docker'
        docker.write_text('#!/bin/sh\nexit 0\n')
        docker.chmod(0o700)
        (root / 'state').mkdir(exist_ok=True)
        store = root / 'store.json'
        if not store.exists():
            store.write_text('{}')
        config = root / 'state/openclaw.json'
        if not config.exists():
            config.symlink_to(store)
        class Startup(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"ok":true,"status":"started"}')
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Startup)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # Point the tasks' fixed server and temp paths at this fixture tree.
        tasks = json.loads(json.dumps(tasks).replace('/home/ubuntu/.openclaw', str(root / 'state'))
                           .replace('/home/ubuntu/.config', str(root / 'config'))
                           .replace('/tmp/ansible-', str(root / 'tmp-ansible-'))
                           .replace('http://127.0.0.1:18789', f'http://127.0.0.1:{server.server_port}')
                           .replace("'gateway-config.json.j2'", repr(str(ROOT / 'ansible/roles/config/templates/gateway-config.json.j2'))))
        def remove_notifications(items):
            for task in items:
                task.pop('notify', None)
                for section in ('block', 'rescue', 'always'):
                    remove_notifications(task.get(section, []))
        remove_notifications(tasks)
        variables = {**self.defaults, 'gateway_token': 'fixture-gateway', 'openclaw_agents': [{'id': 'main', 'is_default': True, 'deliver_channel': 'telegram',
                                         'deliver_to': '', 'deliver_type': 'dm'}],
                     **(extra_vars or {})}
        play = [{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                 'vars': variables, 'tasks': tasks}]
        (root / 'play.json').write_text(json.dumps(play))
        env = {'PATH': f"{root / 'bin'}:{os.environ['PATH']}", 'HOME': str(root),
               'FIXTURE_ROOT': str(root), 'ANSIBLE_NOCOLOR': '1',
               'ANSIBLE_LOCAL_TEMP': str(root / 'local'), 'ANSIBLE_REMOTE_TEMP': str(root / 'remote')}
        try:
            return subprocess.run([self.ansible, '-i', 'localhost,', str(root / 'play.json')],
                                  env=env, capture_output=True, text=True, timeout=120)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def json_writes(self, root):
        writes = {}
        def flatten(value, prefix=''):
            for key, child in value.items():
                name = prefix + '.' + key if prefix else key
                if isinstance(child, dict) and name not in ('models', 'agents.defaults.models'):
                    flatten(child, name)
                else:
                    writes[name] = child
        for line in (root / 'writes').read_text().splitlines():
            flatten(json.loads(line)['patch'])
        return writes

    def test_config_writes_are_serialized_with_gateway_lifecycle(self):
        with tempfile.TemporaryDirectory(prefix='config-role-') as tmp:
            root = Path(tmp)
            result = self.run_task([self.configure], root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse((root / 'gateway-stopped').exists())
            self.assertEqual(len((root / 'writes').read_text().splitlines()), 1)
            calls = [json.loads(line)[0] for line in (root / 'systemctl').read_text().splitlines()]
            self.assertEqual(calls, [['--user', 'stop', 'openclaw-gateway'], ['--user', 'start', 'openclaw-gateway']])
            self.assertEqual(list(root.glob('tmp-ansible-*')), [])
            write = json.loads((root / 'writes').read_text())
            self.assertNotIn('fixture-gateway', json.dumps(write['args']))
            self.assertNotIn('fixture-gateway', result.stdout + result.stderr)

    def test_patch_failure_stays_failed_and_restores_gateway_without_advancing_hash(self):
        for failure in ('patch-fails', 'post-write-fails'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory(prefix='config-role-') as tmp:
                root = Path(tmp)
                first = self.run_task([self.configure], root)
                self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
                hash_path = root / 'state/.ansible-hashes/custom-models'
                previous_hash = hash_path.read_text()
                previous_store = (root / 'store.json').read_text()
                (root / failure).touch()
                (root / 'writes').unlink()
                failed = self.run_task([self.configure], root,
                    {'gateway_token': 'fixture-private-key', 'openclaw_custom_models': {'mode': 'replace'}})
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn('provisioning remains failed', failed.stdout)
                self.assertNotIn('fixture-private-key', failed.stdout + failed.stderr)
                self.assertEqual(hash_path.read_text(), previous_hash)
                self.assertFalse((root / 'gateway-stopped').exists())
                self.assertEqual(list(root.glob('tmp-ansible-*')), [])
                if failure == 'patch-fails':
                    self.assertEqual((root / 'store.json').read_text(), previous_store)
                    self.assertFalse((root / 'writes').exists())
                else:
                    self.assertNotEqual((root / 'store.json').read_text(), previous_store)
                    self.assertIn('not a rollback', failed.stdout)

    def test_stop_and_start_failures_are_not_swallowed(self):
        for failure in ('stop-fails', 'start-fails'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory(prefix='config-role-') as tmp:
                root = Path(tmp)
                (root / failure).touch()
                failed = self.run_task([self.configure], root)
                self.assertNotEqual(failed.returncode, 0)
                self.assertEqual(list(root.glob('tmp-ansible-*')), [])
                self.assertFalse((root / 'state/.ansible-hashes/custom-models').exists())
                if failure == 'stop-fails':
                    self.assertFalse((root / 'writes').exists())
                    self.assertIn('Start the gateway after', failed.stdout)
                else:
                    self.assertTrue((root / 'gateway-stopped').exists())

    def test_unreadable_authored_config_fails_without_touching_gateway(self):
        with tempfile.TemporaryDirectory(prefix='config-role-') as tmp:
            root = Path(tmp)
            (root / 'store.json').write_text('{truncated')
            failed = self.run_task([self.configure], root)
            self.assertNotEqual(failed.returncode, 0)
            self.assertFalse((root / 'systemctl').exists())
            self.assertFalse((root / 'writes').exists())
            self.assertEqual(list(root.glob('tmp-ansible-*')), [])

    def test_custom_models_replacement_and_credentials_preserve_unmanaged_settings(self):
        with tempfile.TemporaryDirectory(prefix='config-role-') as tmp:
            root = Path(tmp)
            (root / 'store.json').write_text(json.dumps({'env': {'vars': {'KEEP': 'fixture-keep'}},
                'tools': {'web': {'search': {'apiKey': 'fixture-old', 'grok': {'apiKey': 'fixture-old'}}}}}))
            first = self.run_task([self.configure], root, {'gemini_api_key': 'fixture-google'})
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            store = json.loads((root / 'store.json').read_text())
            self.assertEqual(store['env']['vars']['KEEP'], 'fixture-keep')
            self.assertEqual(store['models']['providers']['google']['apiKey'], 'fixture-google')
            self.assertNotIn('apiKey', store['tools']['web']['search'])
            self.assertNotIn('apiKey', store['tools']['web']['search']['grok'])
            self.assertNotIn('fixture-google', first.stdout + first.stderr)
            for line in (root / 'writes').read_text().splitlines():
                self.assertNotIn('fixture-google', json.dumps(json.loads(line)['args']))
            removed = self.run_task([self.configure], root, {'gemini_api_key': ''})
            self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
            store = json.loads((root / 'store.json').read_text())
            self.assertNotIn('google', store['models'].get('providers', {}))
            self.assertEqual(store['env']['vars']['KEEP'], 'fixture-keep')

    def test_effective_anthropic_backend_is_required_without_overriding_policy(self):
        for policy in ({'enabled': False}, {'deny': ['anthropic']}, {'deny': [' ANTHROPIC ']},
                {'deny': ['\ufeffanthropic']},
                {'entries': {'anthropic': {'enabled': False}, ' Anthropic ': {'enabled': False}}},
                {'entries': {'anthropic': {'enabled': False}, '\ufeffanthropic': {'enabled': False}}}):
            with self.subTest(policy=policy), tempfile.TemporaryDirectory(prefix='config-role-') as tmp:
                root = Path(tmp)
                (root / 'store.json').write_text(json.dumps({'plugins': policy}))
                failed = self.run_task([self.configure], root)
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn('Anthropic backend is not enabled', failed.stdout + failed.stderr)
                stored = json.loads((root / 'store.json').read_text())['plugins']
                for key in ('enabled', 'deny'):
                    if key in policy:
                        self.assertEqual(stored[key], policy[key])
                for key, entry in policy.get('entries', {}).items():
                    if key != 'anthropic':
                        self.assertEqual(stored['entries'][key], entry)
                self.assertFalse((root / 'gateway-stopped').exists())
                self.assertFalse((root / 'state/.ansible-hashes/custom-models').exists())
                self.assertEqual(list(root.glob('tmp-ansible-*')), [])

    def test_backend_assertion_runs_on_noop_and_inventory_errors_fail(self):
        for failure in ('disabled', 'inventory-fails'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory(prefix='config-role-') as tmp:
                root = Path(tmp)
                first = self.run_task([self.configure], root)
                self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
                (root / 'writes').unlink()
                (root / 'systemctl').unlink()
                if failure == 'disabled':
                    store = json.loads((root / 'store.json').read_text())
                    store['plugins']['enabled'] = False
                    (root / 'store.json').write_text(json.dumps(store))
                else:
                    (root / failure).touch()
                failed = self.run_task([self.configure], root)
                self.assertNotEqual(failed.returncode, 0)
                self.assertFalse((root / 'writes').exists())
                self.assertFalse((root / 'systemctl').exists())
                self.assertEqual(list(root.glob('tmp-ansible-*')), [])

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
                (root / 'systemctl').unlink()
                second = self.run_task(tasks, root, extra)
                self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
                self.assertFalse((root / 'writes').exists(), (root / 'writes').read_text() if (root / 'writes').exists() else '')
                self.assertFalse((root / 'systemctl').exists(), 'Converged config stopped or started the gateway')
                self.assertNotIn('UPDATED:', second.stdout)

    def test_model_map_is_compared_with_the_written_config(self):
        # The fixture refuses config get: comparisons must use the authored map,
        # not the effective map enriched with OpenClaw's default models.
        with tempfile.TemporaryDirectory(prefix='config-role-') as tmp:
            root = Path(tmp)
            first = self.run_task([self.configure], root)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            store = json.loads((root / 'store.json').read_text())
            self.assertEqual(store['agents']['defaults']['models'], self.defaults['openclaw_agent_models'])
            store['agents']['defaults']['modelPolicy']['allow'].reverse()
            (root / 'store.json').write_text(json.dumps(store))
            (root / 'writes').unlink()
            second = self.run_task([self.configure], root)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertFalse((root / 'writes').exists())

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

    def test_skill_keys_reach_the_sandbox_only_as_gateway_environment_references(self):
        # Since 2026.10 skill scripts run through OpenClaw's sandboxed exec, not Claude's Bash.
        for keys in ({}, {'groq_api_key': 'fixture-groq-key', 'gemini_api_key': 'fixture-gemini-key'}):
            with self.subTest(keys=sorted(keys)), tempfile.TemporaryDirectory(prefix='config-role-') as tmp:
                root = Path(tmp)
                result = self.run_task([self.temp_files, self.configure], root, keys)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                store = json.loads((root / 'store.json').read_text())
                self.assertEqual(store['agents']['defaults']['sandbox']['docker']['env'],
                                 {name.upper(): '${' + name.upper() + '}' for name in keys})
                # Groq has no provider config: its key must not reach openclaw.json at all.
                self.assertNotIn('fixture-groq-key', (root / 'store.json').read_text())
                self.assertNotIn('fixture-', json.dumps(store['agents']))
                self.assertEqual(store['plugins']['entries']['anthropic']['config']['managedNativeTools'],
                                 {'default': ['Agent', 'Workflow']})

    def test_a_removed_skill_key_leaves_the_sandbox_and_the_config_converges(self):
        # config patch merges objects: without replacing the env map a removed key would stay
        # (as an unresolvable reference) and every run would stop the gateway again.
        with tempfile.TemporaryDirectory(prefix='config-role-') as tmp:
            root = Path(tmp)
            tasks = [self.temp_files, self.configure]
            both = {'groq_api_key': 'fixture-groq-key', 'gemini_api_key': 'fixture-gemini-key'}
            self.assertEqual(self.run_task(tasks, root, both).returncode, 0)
            result = self.run_task(tasks, root, {'groq_api_key': 'fixture-groq-key'})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            store = json.loads((root / 'store.json').read_text())
            self.assertEqual(store['agents']['defaults']['sandbox']['docker']['env'], {'GROQ_API_KEY': '${GROQ_API_KEY}'})
            (root / 'writes').unlink()
            result = self.run_task(tasks, root, {'groq_api_key': 'fixture-groq-key'})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse((root / 'writes').exists(), 'an unchanged config was written again')

    def test_web_credentials_reach_gateway_environment_but_not_container_allowlist(self):
        # Run only the directory and file tasks; systemd is outside this fixture.
        tasks = [task for task in self.skill_environment
                 if task.get('ansible.builtin.file', {}).get('path') == '/home/ubuntu/.config/openclaw'
                 or task.get('ansible.builtin.copy', {}).get('dest') == '/home/ubuntu/.config/openclaw/claude-skills.env']
        self.assertEqual(len(tasks), 2)
        for provider in ('exa', 'firecrawl'):
            env_name = f'{provider.upper()}_API_KEY'
            self.assertNotIn(env_name, self.defaults['openclaw_claude_cli_skill_env'])
            with tempfile.TemporaryDirectory(prefix='config-role-') as tmp:
                root = Path(tmp)
                for phase, key in (('initial', f'fixture-{provider}-key'),
                                   ('repeated', f'fixture-{provider}-key'), ('removed', '')):
                    with self.subTest(provider=provider, phase=phase):
                        result = self.run_task(tasks, root, {f'{provider}_api_key': key})
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        env_file = root / 'config/openclaw/claude-skills.env'
                        values = dict(line.split('=', 1) for line in shlex.split(env_file.read_text()))
                        self.assertEqual(values[env_name], key)
                        for name in self.defaults['openclaw_claude_cli_skill_env']:
                            self.assertIn(name, values, 'a sandbox ${NAME} reference would not resolve')
                        self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)
                        self.assertEqual(env_file.parent.stat().st_mode & 0o777, 0o700)
                        self.assertNotIn(f'fixture-{provider}-key', result.stdout + result.stderr)
                        if phase == 'repeated':
                            self.assertRegex(result.stdout, r'localhost\s+: ok=2\s+changed=0 ')

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
            # Firecrawl stays the fetch fallback only; agents cannot call it directly.
            self.assertEqual(store['tools']['deny'], ['sessions_spawn', 'firecrawl_search', 'firecrawl_scrape'])
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
            # sessions_spawn stays denied: 2026.10.8 refuses it here (Claude's Agent helper replaces it).
            self.assertEqual(store['tools']['deny'], ['sessions_spawn'])
            # Disabling the hosted fallback must not disable ordinary page reads.
            self.assertEqual(str(store['tools']['web']['fetch']['enabled']).lower(), 'true')
            for plugin in ('exa', 'firecrawl'):
                self.assertNotIn(plugin, store['plugins']['allow'])
                self.assertEqual(str(store['plugins']['entries'][plugin]['enabled']).lower(), 'false')
            (root / 'writes').unlink()
            repeated = self.run_task(tasks, root, {'exa_api_key': '', 'openclaw_firecrawl_enabled': False})
            self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
            self.assertFalse((root / 'writes').exists(), (root / 'writes').read_text() if (root / 'writes').exists() else '')

    def test_removed_xai_key_is_cleared_but_configured_x_search_is_kept(self):
        for key in ('', 'fixture-xai-key'):
            with self.subTest(xai=bool(key)), tempfile.TemporaryDirectory(prefix='config-role-') as tmp:
                root = Path(tmp)
                (root / 'store.json').write_text(json.dumps({'plugins': {'entries': {
                    'xai': {'config': {'webSearch': {'apiKey': 'fixture-xai-key'}}}}}}))
                result = self.run_task([self.temp_files, self.configure], root, {'xai_api_key': key})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                store = json.loads((root / 'store.json').read_text())
                search = store['plugins']['entries']['xai']['config']['webSearch']
                self.assertEqual(search.get('apiKey'), key or None)

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

    def test_unreadable_auth_profiles_are_not_replaced_with_empty_data(self):
        with tempfile.TemporaryDirectory(prefix='config-role-') as tmp:
            root = Path(tmp)
            profiles = root / '.openclaw/agents/main/agent/auth-profiles.json'
            profiles.parent.mkdir(parents=True)
            original = '{"profiles": {"valuable": "truncated'
            profiles.write_text(original)
            failed = self.run_task([self.auth_profiles], root, {'claude_setup_token': 'fixture-private-key'})
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(profiles.read_text(), original)
            self.assertNotIn('fixture-private-key', failed.stdout + failed.stderr)

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
