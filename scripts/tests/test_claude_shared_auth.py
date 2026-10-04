"""Actual Ansible authentication cutover; systemd/Claude/ps are fixture boundaries."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / 'ansible/roles/openclaw/tasks/shared-auth.yml'
HELPER = ROOT / 'ansible/roles/openclaw/files/claude-oauth-seed.cjs'


class SharedAuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ansible = shutil.which('ansible-playbook')
        if not cls.ansible:
            raise RuntimeError('Install Ansible before running the shared-auth tests')
        python = shlex.split(Path(cls.ansible).read_text().splitlines()[0].removeprefix('#!'))
        cls.tasks, cls.restart_tasks, cls.handlers = json.loads(subprocess.check_output([*python, '-c',
            'import json,sys,yaml; print(json.dumps([yaml.safe_load(open(p)) for p in sys.argv[1:]]))', str(TASKS),
            str(ROOT / 'ansible/roles/openclaw/tasks/restart-gateway.yml'),
            str(ROOT / 'ansible/roles/openclaw/handlers/main.yml')], text=True))

    def test_persistence_precedes_dependency_activation_and_cutover(self):
        names = [task['name'] for task in self.tasks]
        ordered = [
            'Install SDK-compatible credential provisioning and checksum helpers',
            'Persist provisioning code before installing a startup dependency',
            'Install persistent start-time login recovery independent of transient timers',
            'Persist login recovery before requiring it at gateway startup',
            'Point the gateway at the host-only authentication environment',
            'Persist startup references before any login mutation',
            'Reload systemd after durable authentication drop-in changes',
            'Arm host-owned recovery before any login mutation',
            'Apply shared authentication and recover independently on failure',
        ]
        positions = [names.index(name) for name in ordered]
        self.assertEqual(positions, sorted(positions))
        cutover = next(task['block'] for task in self.tasks if task['name'] == ordered[-1])
        cutover_names = [task['name'] for task in cutover]
        self.assertLess(cutover_names.index('Move or resume the current login under both SDK locking domains'),
                        cutover_names.index('Select and journal the current login location under SDK locks'))

    def fixture(self, legacy=False, unit=True, partial=False, invalid_source=False, dropin_failure=False, auth_status_failure=False, crash_after_seed=False, late_unit=False, delayed_callback=False, rotation=False):
        temporary = tempfile.TemporaryDirectory(prefix='claude-auth-')
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name).resolve()
        installed = home / '.npm-global/lib/node_modules/openclaw'
        installed.mkdir(parents=True)
        (installed / 'node_modules').symlink_to(ROOT / 'node_modules', target_is_directory=True)
        (home / '.claude').mkdir()
        storage = home / '.claude/shared/auth'
        old = home / '.claude/.credentials.json'
        live = storage / '.credentials.json'
        private = 'synthetic-private-credential'
        data = {'claudeAiOauth': {'accessToken': private, 'refreshToken': ''}}
        if legacy:
            old.write_text(json.dumps(data))
        else:
            storage.mkdir(parents=True)
            live.write_text(json.dumps({'claudeAiOauth': {'accessToken': 'synthetic-old', 'refreshToken': ''}}))
        if unit:
            service = home / '.config/systemd/user/openclaw-gateway.service'
            service.parent.mkdir(parents=True)
            service.write_text('[Unit]\nDescription=fixture\n')
        bin_dir = home / 'bin'
        bin_dir.mkdir()
        systemctl = bin_dir / 'systemctl'
        systemctl.write_text('''#!/usr/bin/env python3
import pathlib,sys,os
h=pathlib.Path(os.environ['HOME']);args=sys.argv[1:];timer=any(x.endswith('.timer') for x in args);state=h/('timer-state' if timer else 'service-state')
current=state.read_text() if state.exists() else 'active'
recovery=any(x.startswith('openclaw-claude-auth-') and x.endswith('.service') for x in args)
if recovery and 'show' in args and '--value' in args:
 if (h/'delayed-callback').exists():
  counter=h/'callback-probes';n=int(counter.read_text())+1 if counter.exists() else 1;counter.write_text(str(n))
  if n<3:print('active')
  else:
   print('inactive')
   if n==3:
    with (h/'service-events').open('a') as f:f.write('restart\\n')
 else:print('inactive')
elif 'show' in args:
 print('LoadState=loaded\\nActiveState='+current+'\\nSubState=running\\nUnitFileState=enabled\\nCanStart=yes\\nNeedDaemonReload=no')
elif any(x in args for x in ['start','restart','stop']):
 action=next(x for x in args if x in ['start','restart','stop'])
 state.write_text('inactive' if action=='stop' else 'active')
 if not timer:
  with (h/'service-events').open('a') as f:f.write(action+'\\n')
elif 'is-enabled' in args:print('enabled')
elif 'is-active' in args:
 print(current);sys.exit(0 if current=='active' else 3)
elif 'list-unit-files' in args:print('openclaw-gateway.service enabled')
''')
        systemctl.chmod(0o700)
        if delayed_callback:
            (home / 'delayed-callback').touch()
        systemd_run = bin_dir / 'systemd-run'
        systemd_run.write_text('#!/bin/sh\nprintf armed\\n\n')
        systemd_run.chmod(0o700)
        ps = bin_dir / 'ps'
        ps.write_text('#!/bin/sh\nexit 1\n')
        ps.chmod(0o700)
        claude = bin_dir / 'claude'
        claude.write_text('''#!/usr/bin/env python3
import json,os,pathlib,sys
p=pathlib.Path(os.environ['CLAUDE_SECURESTORAGE_CONFIG_DIR'])/'.credentials.json'
json.loads(p.read_bytes());print(json.dumps({'loggedIn':True,'email':'synthetic-private-email'}))
'''.replace("'loggedIn':True", "'loggedIn':False" if auth_status_failure else "'loggedIn':True"))
        claude.chmod(0o700)
        (home / 'tmp').mkdir()
        helper_source = home / 'helper.cjs'
        source = HELPER.read_text()
        if partial:
            source = source.replace("const fs = require('node:fs/promises');", """const fs = require('node:fs/promises');
const rename = fs.rename;
fs.rename = async (a,b) => { if (b.endsWith('.credentials-seed.sha256')) throw new Error('synthetic metadata failure'); return rename(a,b); };
""")
        if crash_after_seed:
            source = source.replace("const fs = require('node:fs/promises');", """const fs = require('node:fs/promises');
const unlink = fs.unlink;
fs.unlink = async p => { await unlink(p); if (p.endsWith('/.rotation.json')) process.exit(99); };
""")
        if late_unit:
            source = source.replace("const fs = require('node:fs/promises');", """const fs = require('node:fs/promises');
const rename = fs.rename;
fs.rename = async (a,b) => {
 if (b.endsWith('/.credentials.json')) {
  const root = require('node:os').homedir();
  await fs.access(root + '/.config/openclaw/claude-auth-restart');
  await fs.mkdir(root + '/.config/systemd/user', {recursive:true});
  await fs.writeFile(root + '/.config/systemd/user/openclaw-gateway.service', '[Unit]\\n');
 }
 return rename(a,b);
};
""")
        helper_source.write_text(source)
        (home / 'claude-oauth-seed.py').write_text(HELPER.with_suffix('.py').read_text())
        startup_template = home / 'claude-auth-startup.service.j2'
        startup_template.write_text((ROOT / 'ansible/roles/openclaw/templates/claude-auth-startup.service.j2').read_text().replace('/home/ubuntu', str(home)))
        tasks = json.loads(json.dumps(self.tasks).replace('/home/ubuntu', str(home)))
        def patch_tasks(items):
            for task in items:
                for nested in ['block', 'rescue', 'always']:
                    if nested in task:
                        patch_tasks(task[nested])
                if task.get('ansible.builtin.copy', {}).get('src') == '{{ item.src }}':
                    task['loop'] = [{'src': str(helper_source), 'dest': 'claude-oauth-seed'},
                                    {'src': str(home / 'claude-oauth-seed.py'), 'dest': 'claude-oauth-seed.py'}]
                if task.get('ansible.builtin.template', {}).get('src') == 'claude-auth-startup.service.j2':
                    task['ansible.builtin.template']['src'] = str(startup_template)
                if 'ansible.builtin.tempfile' in task:
                    task['ansible.builtin.tempfile']['path'] = str(home / 'tmp')
                if dropin_failure and task.get('ansible.builtin.copy', {}).get('dest', '').endswith('/claude-auth.conf'):
                    task['ansible.builtin.copy'].pop('content')
                    task['ansible.builtin.copy']['src'] = str(home / 'deliberately-missing-dropin-source')
                if task.get('ansible.builtin.copy', {}).get('src') == 'claude-oauth-seed.py':
                    task['ansible.builtin.copy']['src'] = str(home / 'claude-oauth-seed.py')
                if task.get('ansible.builtin.command') == 'claude auth status':
                    task['ansible.builtin.command'] = str(claude) + ' auth status'
        patch_tasks(tasks)
        restart_tasks = json.loads(json.dumps(self.restart_tasks).replace('/home/ubuntu', str(home)))
        restart_file = home / 'restart-gateway.yml'
        restart_file.write_text(json.dumps(restart_tasks))
        handlers = json.loads(json.dumps(self.handlers))
        handlers[0]['ansible.builtin.include_tasks']['file'] = str(restart_file)
        shared_file = home / 'shared-auth.yml'
        shared_file.write_text(json.dumps(tasks))
        play = [{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                 'vars': {'claude_secure_storage_dir': str(storage), 'claude_oauth_rotate_credentials': rotation, 'claude_oauth_credentials': '{}' if invalid_source else json.dumps(data),
                          'ansible_facts': {'env': {'PATH': str(bin_dir) + os.pathsep + os.environ['PATH']}}},
                 'tasks': [{'ansible.builtin.include_tasks': {'file': str(shared_file), 'apply': {'tags': ['claude-auth']}}, 'tags': ['claude-auth']}],
                 'handlers': handlers}]
        playbook = home / 'play.json'
        playbook.write_text(json.dumps(play))
        env = {**os.environ, 'HOME': str(home), 'PATH': str(bin_dir) + os.pathsep + os.environ['PATH'],
               'ANSIBLE_NOCOLOR': '1', 'ANSIBLE_LOCAL_TEMP': str(home / 'local'),
               'ANSIBLE_REMOTE_TEMP': str(home / 'remote')}
        def run(expect_success=True):
            result = subprocess.run([self.ansible, '-i', 'localhost,', str(playbook), '--tags', 'claude-auth'],
                                    env=env, capture_output=True, text=True, timeout=80)
            for secret in [private, 'synthetic-private-email']:
                self.assertNotIn(secret, result.stdout + result.stderr)
            self.assertEqual(result.returncode == 0, expect_success, result.stdout + result.stderr)
            self.assertFalse(list((home / 'tmp').glob('ansible-claude-seed-*.json')))
            return result.stdout
        return home, storage, old, live, helper_source, run

    def test_stable_reset_id_reaches_the_helper_and_converges(self):
        home, storage, old, live, helper, run = self.fixture(rotation='request-one')
        run()
        history = home/'.config/openclaw/claude-auth-rotation'
        self.assertIn('request-one', json.loads(history.read_text())['requests'])
        refreshed = json.dumps({'claudeAiOauth': {'accessToken': 'synthetic-later-refresh', 'refreshToken': ''}})
        candidate = home/'sdk-refresh'
        candidate.write_text(refreshed)
        os.replace(candidate, live)
        self.assertIn('changed=0', run())
        self.assertEqual(live.read_text(), refreshed)
        playbook = home/'play.json'
        play = json.loads(playbook.read_text())
        play[0]['vars']['claude_oauth_rotate_credentials'] = 'request-two'
        playbook.write_text(json.dumps(play))
        run()
        self.assertEqual(set(json.loads(history.read_text())['requests']), {'request-one', 'request-two'})
        self.assertNotEqual(live.read_text(), refreshed)

    def test_alias_preparation_cannot_bypass_provisioner_writer_verification(self):
        home, storage, old, live, helper, run = self.fixture(legacy=True)
        storage.mkdir(parents=True)
        os.link(old, live)
        (storage/'.migration.json').write_text(json.dumps({'legacy': str(old.parent), 'directory': str(storage), 'phase': 'moving'}))
        (home/'bin/ps').write_text('#!/bin/sh\nprintf "Sl\\n"\n')
        before = old.read_bytes()
        run(expect_success=False)
        self.assertTrue(old.exists())
        self.assertEqual(old.read_bytes(), before)
        self.assertEqual(live.read_bytes(), before)
        self.assertTrue((storage/'.migration.json').exists())

    def test_ps_nonempty_stdout_with_exit_one_never_moves_the_login(self):
        home, storage, old, live, helper, run = self.fixture(legacy=True)
        (home/'bin/ps').write_text('#!/bin/sh\nprintf "Sl\\n"\nexit 1\n')
        before = old.read_bytes()
        run(expect_success=False)
        self.assertEqual(old.read_bytes(), before)
        self.assertFalse(live.exists())
        self.assertFalse((storage/'.migration.json').exists())
        self.assertEqual((home/'service-state').read_text(), 'active')

    def test_ps_diagnostics_with_zero_exit_never_move_the_login(self):
        home, storage, old, live, helper, run = self.fixture(legacy=True)
        (home/'bin/ps').write_text('#!/bin/sh\nprintf "synthetic process diagnostics\\n" >&2\nexit 0\n')
        before = old.read_bytes()
        run(expect_success=False)
        self.assertEqual(old.read_bytes(), before)
        self.assertFalse(live.exists())
        self.assertFalse((storage/'.migration.json').exists())
        self.assertEqual((home/'service-state').read_text(), 'active')

    def test_completed_migration_receipt_never_stops_a_healthy_shared_gateway(self):
        home, storage, old, live, helper, run = self.fixture(legacy=True)
        run()
        version = (storage/'.credentials-seed.sha256').read_text().strip()
        (storage/'.migration.json').write_text(json.dumps({'directory': str(storage), 'legacy': str(home/'.claude'),
                                                        'phase': 'seeding', 'seedHash': version, 'operation': 'adopt', 'committed': True}))
        before = (home/'service-events').read_text().splitlines()
        self.assertIn('changed=0', run())
        self.assertEqual((home/'service-events').read_text().splitlines(), before)
        self.assertFalse((storage/'.migration.json').exists())

    def test_inflight_owned_recovery_is_drained_before_completion(self):
        home, storage, old, live, helper, run = self.fixture(delayed_callback=True)
        run()
        self.assertGreaterEqual(int((home/'callback-probes').read_text()), 3)
        self.assertEqual((home/'timer-state').read_text(), 'inactive')
        self.assertEqual((home/'service-events').read_text().splitlines(), ['restart', 'restart'])

    def test_bootstrap_recovery_callback_finishes_without_a_gateway_unit(self):
        home, storage, old, live, helper, run = self.fixture(unit=False)
        run()
        result = subprocess.run(['node', str(home/'.local/bin/claude-oauth-seed'), 'recover', str(home/'.claude'), str(storage)],
                                env={**os.environ, 'HOME': str(home), 'PATH': str(home/'bin') + os.pathsep + os.environ['PATH']},
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'GATEWAY_NOT_INSTALLED')
        self.assertFalse((home/'service-events').exists())
        self.assertEqual((home/'timer-state').read_text(), 'inactive')

    def test_native_skill_environment_reuses_provider_credentials_privately(self):
        home, storage, old, live, helper, run = self.fixture()
        run()
        python = shlex.split(Path(self.ansible).read_text().splitlines()[0].removeprefix('#!'))
        tasks = json.loads(subprocess.check_output([*python, '-c',
            'import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))',
            str(ROOT / 'ansible/roles/config/tasks/skill-environment.yml')], text=True).replace('/home/ubuntu', str(home)))
        for task in tasks:
            task['tags'] = ['claude-auth']
        playbook = home / 'play.json'
        play = json.loads(playbook.read_text())
        play[0]['tasks'] = tasks
        play[0]['vars'].update(groq_api_key='synthetic-groq-key', gemini_api_key='synthetic-gemini-key')
        playbook.write_text(json.dumps(play))
        before = (home / 'service-events').read_text().splitlines()
        output = run()
        for private in ['synthetic-groq-key', 'synthetic-gemini-key']:
            self.assertNotIn(private, output)
        environment = home / '.config/openclaw/claude-skills.env'
        self.assertEqual(environment.stat().st_mode & 0o777, 0o600)
        self.assertEqual(environment.read_text(), 'GROQ_API_KEY="synthetic-groq-key"\nGEMINI_API_KEY="synthetic-gemini-key"\n')
        self.assertEqual((home / 'service-events').read_text().splitlines(), before + ['restart'])
        self.assertIn('changed=0', run())
        self.assertEqual((home / 'service-events').read_text().splitlines(), before + ['restart'])

    def test_restart_capture_failure_still_restarts_and_never_acknowledges(self):
        home, storage, old, live, helper, run = self.fixture()
        run()
        marker = home / '.config/openclaw/claude-auth-restart'
        marker.write_text('synthetic-pending-generation\n')
        storage.rename(storage.with_name('held'))
        playbook = home / 'play.json'
        play = json.loads(playbook.read_text())
        play[0]['tasks'] = [{'ansible.builtin.debug': {'msg': 'fixture refresh'}, 'changed_when': True,
                             'notify': 'restart openclaw-gateway', 'tags': ['claude-auth']}]
        playbook.write_text(json.dumps(play))
        before = (home / 'service-events').read_text().splitlines()
        output = run(expect_success=False)
        self.assertIn('restart was attempted', output)
        self.assertEqual((home / 'service-events').read_text().splitlines(), before + ['restart'])
        self.assertEqual(marker.read_text(), 'synthetic-pending-generation\n')

    def test_late_unit_installation_refreshes_and_acknowledges(self):
        home, storage, old, live, helper, run = self.fixture(unit=False, late_unit=True)
        run()
        self.assertEqual((home / 'service-events').read_text().splitlines(), ['restart'])
        self.assertFalse((home / '.config/openclaw/claude-auth-restart').exists())
        self.assertEqual((home / 'timer-state').read_text(), 'inactive')
        self.assertIn('changed=0', run())
        self.assertEqual((home / 'service-events').read_text().splitlines(), ['restart'])

    def test_paused_provisioner_cannot_restore_a_stale_pointer_or_stop_again(self):
        import time
        home, storage, old, live, helper, run = self.fixture(legacy=True)
        shared_file = home / 'shared-auth.yml'
        tasks = json.loads(shared_file.read_text())
        index = next(i for i,t in enumerate(tasks) if t['name'].startswith('Validate pointer preparation'))
        pause = home / 'pause.py'
        pause.write_text("import pathlib,time\np=pathlib.Path(" + repr(str(home)) + ")\n(p/'paused').write_text('1')\nwhile not (p/'release').exists():time.sleep(.05)\n")
        tasks.insert(index, {'name': 'Pause only the stale-stat provisioner', 'ansible.builtin.command': {'argv': ['python3', str(pause)]},
                            'when': "lookup('env', 'CLAUDE_AUTH_FIXTURE_PAUSE') == '1'", 'changed_when': False})
        shared_file.write_text(json.dumps(tasks))
        env = {**os.environ, 'HOME': str(home), 'PATH': str(home/'bin') + os.pathsep + os.environ['PATH'],
               'CLAUDE_AUTH_FIXTURE_PAUSE': '1', 'ANSIBLE_LOCAL_TEMP': str(home/'b-local'), 'ANSIBLE_REMOTE_TEMP': str(home/'b-remote')}
        process = subprocess.Popen([self.ansible, '-i', 'localhost,', str(home/'play.json'), '--tags', 'claude-auth'],
                                   env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 30
            while not (home/'paused').exists():
                self.assertIsNone(process.poll(), 'Paused provisioner exited too early')
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.05)
            run()
            before = (home/'service-events').read_text().splitlines()
            (home/'release').write_text('1')
            stdout, stderr = process.communicate(timeout=80)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            self.assertNotIn('synthetic-private-credential', stdout + stderr)
            self.assertEqual((home/'service-events').read_text().splitlines(), before)
            self.assertIn(str(storage), (home/'.config/openclaw/claude-auth.env').read_text())
            self.assertFalse(old.exists())
        finally:
            (home/'release').write_text('1')
            if process.poll() is None:
                process.terminate(); process.communicate(timeout=10)

    def test_cutover_preserves_login_restarts_once_and_converges(self):
        home, storage, old, live, helper, run = self.fixture(legacy=True)
        old.write_text(json.dumps({'claudeAiOauth': {'accessToken': 'synthetic-current-live', 'refreshToken': ''}}))
        before = old.read_bytes()
        run()
        self.assertEqual(live.read_bytes(), before)
        self.assertFalse(old.exists())
        self.assertEqual((home / 'service-events').read_text().splitlines(), ['stop', 'restart'])
        environment = home / '.config/openclaw/claude-auth.env'
        self.assertIn(str(environment), (home / '.config/systemd/user/openclaw-gateway.service.d/claude-auth.conf').read_text())
        self.assertIn(str(storage), environment.read_text())
        for name in ['.bashrc', '.profile']:
            self.assertIn(str(environment), (home / name).read_text())
        output = run()
        self.assertIn('changed=0', output)
        self.assertEqual((home / 'service-events').read_text().splitlines(), ['stop', 'restart'])

    def test_auth_bootstrap_does_not_stop_or_restart_a_missing_unit(self):
        home, storage, old, live, helper, run = self.fixture(legacy=True, unit=False)
        run()
        self.assertTrue(live.exists())
        self.assertFalse((home / 'service-events').exists())
        self.assertIn('changed=0', run())

    def test_failed_environment_preparation_never_stops_or_moves_the_login(self):
        home, storage, old, live, helper, run = self.fixture(legacy=True, dropin_failure=True)
        before = old.read_bytes()
        run(expect_success=False)
        self.assertEqual(old.read_bytes(), before)
        self.assertFalse(live.exists())
        self.assertFalse((home / 'service-events').exists())

    def test_failed_bootstrap_still_selects_the_surviving_login(self):
        home, storage, old, live, helper, run = self.fixture(legacy=True, unit=False, invalid_source=True)
        before = old.read_bytes()
        run(expect_success=False)
        self.assertEqual(live.read_bytes(), before)
        self.assertFalse(old.exists())
        self.assertIn(str(storage), (home / '.config/openclaw/claude-auth.env').read_text())
        self.assertFalse((home / 'service-events').exists())

    def test_auth_status_failure_still_flushes_a_successful_rotation_restart(self):
        home, storage, old, live, helper, run = self.fixture(auth_status_failure=True)
        live.write_text(json.dumps({'claudeAiOauth': {'accessToken': 'synthetic-previous', 'refreshToken': ''}}))
        (storage / '.credentials-seed.sha256').write_text('0' * 64 + '\n')
        before = live.read_bytes()
        run(expect_success=False)
        self.assertNotEqual(live.read_bytes(), before)
        self.assertEqual((home / 'service-events').read_text().splitlines(), ['restart'])

    def test_dead_seeder_restart_intent_survives_and_is_acknowledged(self):
        home, storage, old, live, helper, run = self.fixture(crash_after_seed=True)
        environment = home / '.config/openclaw/claude-auth.env'
        environment.parent.mkdir(parents=True)
        environment.write_text('CLAUDE_SECURESTORAGE_CONFIG_DIR=' + str(storage) + '\n')
        environment.chmod(0o600)
        dropin = home / '.config/systemd/user/openclaw-gateway.service.d/claude-auth.conf'
        dropin.parent.mkdir()
        dropin.write_text('[Service]\nEnvironmentFile=' + str(environment) + '\n')
        dropin.chmod(0o600)
        before = live.read_bytes()
        run(expect_success=False)
        self.assertNotEqual(live.read_bytes(), before)
        self.assertEqual((home / 'service-events').read_text().splitlines(), ['restart'])
        self.assertFalse((home / '.config/openclaw/claude-auth-restart').exists())
        helper.write_text(HELPER.read_text())
        run()
        self.assertIn('changed=0', run())
        self.assertEqual((home / 'service-events').read_text().splitlines(), ['restart'])

    def test_partial_replacement_flushes_restart_and_retry_recovers(self):
        home, storage, old, live, helper, run = self.fixture(partial=True)
        run(expect_success=False)
        self.assertIn('restart', (home / 'service-events').read_text().splitlines())
        self.assertFalse((storage / '.credentials-seed.sha256').exists())
        self.assertEqual(json.loads(live.read_bytes())['claudeAiOauth']['accessToken'], 'synthetic-private-credential')
        helper.write_text(HELPER.read_text())
        run()
        self.assertTrue((storage / '.credentials-seed.sha256').exists())
        self.assertIn('changed=0', run())


if __name__ == '__main__':
    unittest.main()
