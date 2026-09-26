#!/usr/bin/env python3
"""Run the stale group-session cleanup through Ansible against fixture files.

The gateway keeps each session store in memory and rewrites it whole, so the
cleanup must stop the gateway while it deletes a session, and start it again.
"""
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / 'ansible/roles/telegram/tasks/session-cleanup.yml'
FAKE_SYSTEMCTL = '''#!/usr/bin/env python3
# Records each gateway stop/start with the session store as it is at that moment.
import json, os, sys
root = os.environ['FIXTURE_ROOT']
store = os.path.join(root, 'state/agents/main/sessions/sessions.json')
with open(os.path.join(root, 'systemctl'), 'a') as f:
    f.write(json.dumps([sys.argv[1:], open(store).read() if os.path.exists(store) else None]) + '\\n')
'''
AGENT = {'id': 'main', 'deliver_channel': 'telegram', 'deliver_type': 'group', 'deliver_to': '-100200'}


class SessionCleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ansible = shutil.which('ansible-playbook')
        if not cls.ansible:
            raise RuntimeError("Install the project's Ansible development dependency first")
        python = shlex.split(Path(cls.ansible).read_text().splitlines()[0].removeprefix('#!'))
        code = 'import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))'
        cls.tasks = json.loads(subprocess.run([*python, '-c', code, str(TASKS)],
                                              capture_output=True, text=True, check=True).stdout)
        assert any(t.get('name') == 'Check and fix group agent session delivery targets' for t in cls.tasks), 'Cleanup task moved'

    def run_cleanup(self, root):
        (root / 'bin').mkdir(exist_ok=True)
        systemctl = root / 'bin/systemctl'
        systemctl.write_text(FAKE_SYSTEMCTL)
        systemctl.chmod(0o700)
        tasks = json.loads(json.dumps(self.tasks).replace('/home/ubuntu/.openclaw', str(root / 'state'))
                           .replace('/tmp/ansible-', str(root / 'tmp-ansible-')))
        for task in tasks:
            task.pop('notify', None)
        play = [{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                 'vars': {'openclaw_agents': [AGENT]}, 'tasks': tasks}]
        (root / 'play.json').write_text(json.dumps(play))
        env = {'PATH': f"{root / 'bin'}:/usr/bin:/bin", 'HOME': str(root), 'FIXTURE_ROOT': str(root),
               'ANSIBLE_NOCOLOR': '1', 'ANSIBLE_LOCAL_TEMP': str(root / 'local'),
               'ANSIBLE_REMOTE_TEMP': str(root / 'remote')}
        return subprocess.run([self.ansible, '-i', 'localhost,', str(root / 'play.json')],
                              env=env, capture_output=True, text=True, timeout=120)

    def store(self, root, target):
        sessions = root / 'state/agents/main/sessions/sessions.json'
        sessions.parent.mkdir(parents=True)
        original = {'agent:main:main': {'sessionId': 's1', 'deliveryContext': {'to': target}},
                    'agent:main:other': {'sessionId': 's2'}}
        sessions.write_text(json.dumps(original))
        sessions.chmod(0o640)
        (sessions.parent / 's1.jsonl').write_text('{}\n')
        return sessions, original

    def test_a_stale_session_is_removed_with_the_gateway_stopped(self):
        with tempfile.TemporaryDirectory(prefix='session-cleanup-') as tmp:
            root = Path(tmp)
            sessions, original = self.store(root, 'telegram:-100999')
            result = self.run_cleanup(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(sessions.read_text()), {'agent:main:other': {'sessionId': 's2'}})
            self.assertFalse((sessions.parent / 's1.jsonl').exists())
            self.assertEqual(sessions.stat().st_mode & 0o777, 0o640)
            calls = [json.loads(line) for line in (root / 'systemctl').read_text().splitlines()]
            self.assertEqual([c[0] for c in calls], [['--user', 'stop', 'openclaw-gateway'], ['--user', 'start', 'openclaw-gateway']])
            self.assertEqual(json.loads(calls[0][1]), original)
            self.assertEqual(json.loads(calls[1][1]), {'agent:main:other': {'sessionId': 's2'}})
            self.assertRegex(result.stdout, r'localhost\s+: ok=\d+\s+changed=1 ')

    def test_a_correct_session_leaves_the_gateway_alone(self):
        with tempfile.TemporaryDirectory(prefix='session-cleanup-') as tmp:
            root = Path(tmp)
            sessions, original = self.store(root, 'telegram:-100200')
            result = self.run_cleanup(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(sessions.read_text()), original)
            self.assertFalse((root / 'systemctl').exists())
            self.assertRegex(result.stdout, r'localhost\s+: ok=\d+\s+changed=0 ')


if __name__ == '__main__':
    unittest.main()
