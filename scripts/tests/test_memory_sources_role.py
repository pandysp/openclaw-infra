#!/usr/bin/env python3
"""Run the memory-sources role's tasks against fake Docker, systemd and OpenClaw CLIs.

A fresh host has none of the role's folders, and turning memory search off must
take the job, its copies of private text and the per-agent search paths away again.
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
ROLE = ROOT / 'ansible/roles/memory-sources'
FAKE_OPENCLAW = '''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
root = os.environ['FIXTURE_ROOT']
config_path = os.path.join(root, 'home/.openclaw/openclaw.json')
config = json.load(open(config_path))
with open(os.path.join(root, 'openclaw-calls'), 'a') as f:
    f.write(json.dumps(args) + '\\n')
key = args[2].split('.')
node = config
for part in key[:-1]:
    node = node.get(part, {}) if args[1] == 'get' else node.setdefault(part, {})
if args[1] == 'get':
    if key[-1] not in node:
        print(json.dumps({'ok': False, 'error': {'message': 'Config path is valid but unset: ' + args[2]}}))
        sys.exit(1)
    print(json.dumps(node[key[-1]], indent=2))
    sys.exit(0)
if args[1] == 'set':
    node[key[-1]] = json.loads(args[4])
elif args[1] == 'unset':
    node.pop(key[-1])
json.dump(config, open(config_path, 'w'))
'''
RECORD = '''#!/bin/sh
echo "$(basename "$0") $*" >> "$FIXTURE_ROOT/calls"
if [ "$1 $2" = "image inspect" ] && [ ! -e "$FIXTURE_ROOT/image-built" ]; then exit 1; fi
if [ "$1" = build ]; then touch "$FIXTURE_ROOT/image-built"; fi
'''


class MemorySourcesRoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ansible = shutil.which('ansible-playbook')
        if not cls.ansible:
            raise RuntimeError("Install the project's Ansible development dependency first")
        python = shlex.split(Path(cls.ansible).read_text().splitlines()[0].removeprefix('#!'))
        code = 'import json,sys,yaml; print(json.dumps([yaml.safe_load(open(p)) for p in sys.argv[1:]]))'
        cls.enable, cls.disable, cls.defaults = json.loads(subprocess.run(
            [*python, '-c', code, str(ROLE / 'tasks/enable.yml'), str(ROLE / 'tasks/disable.yml'),
             str(ROOT / 'ansible/group_vars/all.yml')], capture_output=True, text=True, check=True).stdout)

    def run_tasks(self, root, tasks):
        home = root / 'home'
        tasks = [t for t in tasks if 'ansible.builtin.meta' not in t]
        for task in tasks:
            task.pop('notify', None)
            # systemd itself is outside this fixture: record what the module would do.
            unit = task.pop('ansible.builtin.systemd', None)
            if unit:
                task['ansible.builtin.command'] = f"systemctl --user {unit['state']} {unit['name']}"
                task['changed_when'] = False
        tasks = json.loads(json.dumps(tasks).replace('/home/ubuntu', str(home))
                           .replace('"src": "{{ item }}.j2"', json.dumps({'src': str(ROLE / 'templates') + '/{{ item }}.j2'})[1:-1])
                           .replace('"src": "openclaw-memory-sources.sh.j2"', json.dumps({'src': str(ROLE / 'templates/openclaw-memory-sources.sh.j2')})[1:-1])
                           .replace('"src": "openclaw-memory-sources-alert.sh.j2"', json.dumps({'src': str(ROLE / 'templates/openclaw-memory-sources-alert.sh.j2')})[1:-1])
                           .replace('"src": "{{ item }}"', json.dumps({'src': str(ROLE / 'files') + '/{{ item }}'})[1:-1]))
        workspaces = [{'agent_id': agent, 'workspace_dir': str(home / ('.openclaw/workspace' + ('' if agent == 'main' else '-' + agent)))}
                      for agent in ('main', 'tl')]
        variables = {**self.defaults, '_openclaw_workspaces': workspaces,
                     'openclaw_agents': [{'id': 'main', 'is_default': True, 'deliver_channel': 'telegram', 'deliver_to': '42'}],
                     'memory_sources_dir': str(home / '.openclaw/memory-sources')}
        play = [{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False, 'vars': variables, 'tasks': tasks}]
        (root / 'play.json').write_text(json.dumps(play))
        env = {'PATH': f"{root / 'bin'}:{os.environ['PATH']}", 'HOME': str(root), 'FIXTURE_ROOT': str(root),
               'ANSIBLE_NOCOLOR': '1', 'ANSIBLE_LOCAL_TEMP': str(root / 'local'), 'ANSIBLE_REMOTE_TEMP': str(root / 'remote')}
        return subprocess.run([self.ansible, '-i', 'localhost,', str(root / 'play.json')],
                              env=env, capture_output=True, text=True, timeout=120)

    def fixture(self, root):
        (root / 'bin').mkdir()
        for name, body in (('openclaw', FAKE_OPENCLAW), ('docker', RECORD), ('systemctl', RECORD)):
            (root / 'bin' / name).write_text(body)
            (root / 'bin' / name).chmod(0o700)
        state = root / 'home/.openclaw'
        state.mkdir(parents=True)
        # A fresh host: no ~/.local or ~/.config/systemd yet (Phoenix found the missing ~/.local/bin).
        (state / 'openclaw.json').write_text(json.dumps({'agents': {'entries': {'main': {}, 'tl': {}}}}))
        return state

    def check_runner_and_alert(self, root):
        home = root / 'home'
        (home / '.openclaw/workspace').mkdir(exist_ok=True)
        (home / '.openclaw/workspace-tl').mkdir(exist_ok=True)
        docker = root / 'bin/docker'
        docker.write_text('''#!/bin/sh
case "$*" in
  "ps -aq --filter name=^openclaw-memory-sources-") echo leftover-container ;;
  "rm -f leftover-container") echo "rm leftover" >> "$FIXTURE_ROOT/calls" ;;
  run*openclaw-memory-sources-tl*) echo "1 copies: 0 written, 0 removed, 1 unreadable"; echo "UNREADABLE docs/a.pdf: broken"; exit 3 ;;
  run*) echo "2 copies: 2 written, 0 removed, 0 unreadable" ;;
esac
''')
        env = {'PATH': f"{root / 'bin'}:{os.environ['PATH']}", 'FIXTURE_ROOT': str(root)}
        # Run copies, so the installed files stay as provisioned for the rerun below.
        runner = root / 'runner.sh'
        runner.write_text((home / '.local/bin/openclaw-memory-sources').read_text().replace('/home/ubuntu', str(home)))
        result = subprocess.run(['bash', str(runner)], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, 'one agent failing must fail the unit')
        self.assertIn('rm leftover', (root / 'calls').read_text(), 'an orphaned container was not removed first')
        self.assertIn('main: 2 copies', result.stdout)
        log = home / '.local/state/openclaw-memory-sources/last-run.log'
        self.assertIn('tl: UNREADABLE docs/a.pdf', log.read_text())
        self.assertIn('ERROR: tl copies failed (exit 3)', log.read_text())

        sent = root / 'sent'
        fake = home / '.npm-global/bin/openclaw'
        fake.parent.mkdir(parents=True)
        fake.write_text(f'#!/bin/sh\necho "$*" >> {sent}\n')
        fake.chmod(0o700)
        alert = root / 'alert.sh'
        alert.write_text((home / '.local/bin/openclaw-memory-sources-alert').read_text().replace('/home/ubuntu', str(home)))
        def fire():
            subprocess.run(['bash', str(alert)], env=env, check=True)
            return sent.read_text().count('message send') if sent.exists() else 0
        self.assertEqual(fire(), 1)
        self.assertEqual(fire(), 1, 'the same failure must not be sent again every run')
        log.write_text(log.read_text() + 'tl: UNREADABLE docs/b.pdf: broken\n')
        self.assertEqual(fire(), 2, 'a new failure must be sent')

    def test_a_fresh_host_gets_the_job_and_per_agent_paths_and_loses_them_when_search_is_off(self):
        with tempfile.TemporaryDirectory(prefix='memory-sources-role-') as tmp:
            root = Path(tmp)
            state = self.fixture(root)
            result = self.run_tasks(root, self.enable)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            entries = json.loads((state / 'openclaw.json').read_text())['agents']['entries']
            for agent in ('main', 'tl'):
                self.assertEqual(entries[agent]['memory']['search']['extraPaths'], [f'{state}/memory-sources/{agent}'])
                self.assertEqual((state / 'memory-sources' / agent).stat().st_mode & 0o777, 0o700)
            calls = (root / 'calls').read_text()
            self.assertIn('docker build --tag openclaw-memory-sources:latest', calls)
            self.assertIn('systemctl --user start --no-block openclaw-memory-sources.service', calls)
            runner = (root / 'home/.local/bin/openclaw-memory-sources').read_text()
            self.assertEqual(runner.count('docker run'), 2)
            self.assertIn('--network none', runner)
            self.assertIn(f'source={state}/workspace-tl,target=/source,readonly', runner)
            units = root / 'home/.config/systemd/user'
            self.assertIn('OnFailure=openclaw-memory-sources-failed.service', (units / 'openclaw-memory-sources.service').read_text())
            self.assertIn('ExecStopPost=', (units / 'openclaw-memory-sources.service').read_text())
            self.assertIn('--target 42', (root / 'home/.local/bin/openclaw-memory-sources-alert').read_text())
            self.check_runner_and_alert(root)

            # A second run builds nothing, starts nothing and writes no config.
            (root / 'calls').unlink()
            (root / 'openclaw-calls').unlink()
            result = self.run_tasks(root, self.enable)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertRegex(result.stdout, r'localhost\s+: ok=\d+\s+changed=0 ')
            self.assertNotIn('build', (root / 'calls').read_text())
            self.assertNotIn('"set"', (root / 'openclaw-calls').read_text())

            (state / 'memory-sources/main/notes').mkdir()
            (state / 'memory-sources/main/notes/private.pdf.md').write_text('private text')
            (units / 'openclaw-memory-sources.timer').touch()
            result = self.run_tasks(root, self.disable)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            entries = json.loads((state / 'openclaw.json').read_text())['agents']['entries']
            self.assertEqual(entries['main']['memory']['search'], {})
            self.assertFalse((state / 'memory-sources').exists())
            self.assertFalse((units / 'openclaw-memory-sources.timer').exists())
            self.assertFalse((root / 'home/.local/bin/openclaw-memory-sources').exists())
            result = self.run_tasks(root, self.disable)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertRegex(result.stdout, r'localhost\s+: ok=\d+\s+changed=0 ')


if __name__ == '__main__':
    unittest.main()
