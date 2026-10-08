#!/usr/bin/env python3
"""Run the agents role's heartbeat tasks through Ansible against a fake OpenClaw 2026.9.8 CLI.

OpenClaw 2026.9.8 keys agents under agents.entries and reports a valid but unset
path on stderr with exit 1 ("Config path is valid but unset: ..."). The fake
copies that behaviour, so a role that still expects 2026.6's "Config path not
found" or agents.list indexes fails here instead of on a live server.
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
HEARTBEAT = ROOT / 'ansible/roles/agents/tasks/heartbeat.yml'
FAKE_OPENCLAW = '''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
root = os.environ['FIXTURE_ROOT']
store_path = os.path.join(root, 'store.json')
store = json.load(open(store_path)) if os.path.exists(store_path) else {}
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
        sys.stderr.write('Config path is valid but unset: ' + args[2] + '. The runtime default applies.\\n'); sys.exit(1)
    print(json.dumps(node[leaf])); sys.exit(0)
if args[:2] in (['config', 'set'], ['config', 'unset']):
    with open(os.path.join(root, 'writes'), 'a') as f:
        f.write(json.dumps(args) + '\\n')
    node, leaf = walk(args[2], create=args[1] == 'set')
    if args[1] == 'set':
        node[leaf] = json.loads(args[args.index('--json') + 1])
    else:
        del node[leaf]
    json.dump(store, open(store_path, 'w')); sys.exit(0)
sys.exit('unexpected openclaw call: ' + ' '.join(args))
'''


class AgentHeartbeatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ansible = shutil.which('ansible-playbook')
        if not cls.ansible:
            raise RuntimeError("Install the project's Ansible development dependency first")
        python = shlex.split(Path(cls.ansible).read_text().splitlines()[0].removeprefix('#!'))
        code = 'import json,sys,yaml; print(json.dumps([yaml.safe_load(open(p)) for p in sys.argv[1:]]))'
        cls.tasks, cls.defaults = json.loads(subprocess.run(
            [*python, '-c', code, str(HEARTBEAT), str(ROOT / 'ansible/group_vars/all.yml')],
            capture_output=True, text=True, check=True).stdout)

    def run_heartbeat(self, root, enabled):
        (root / 'bin').mkdir(exist_ok=True)
        fake = root / 'bin/openclaw'
        fake.write_text(FAKE_OPENCLAW)
        fake.chmod(0o700)
        agents = root / 'agents.json'
        agents.write_text(json.dumps([{'id': 'main'}, {'id': 'henning'}]))
        tasks = json.loads(json.dumps(self.tasks).replace('/tmp/ansible_agents_current.json', str(agents)))
        for task in tasks:
            for inner in task.get('block', []):
                inner.pop('notify', None)
                inner.pop('no_log', None)
        variables = {**self.defaults, '_openclaw_scheduled_automation_by_agent': {'henning': enabled},
                     '_heartbeat_agent': {'id': 'henning', 'deliver_channel': 'telegram', 'deliver_to': '123',
                                          'heartbeat_every': '12h'}}
        play = [{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                 'vars': variables, 'tasks': tasks}]
        (root / 'play.json').write_text(json.dumps(play))
        env = {'PATH': f"{root / 'bin'}:{os.environ['PATH']}", 'HOME': str(root), 'FIXTURE_ROOT': str(root),
               'ANSIBLE_NOCOLOR': '1', 'ANSIBLE_LOCAL_TEMP': str(root / 'local'),
               'ANSIBLE_REMOTE_TEMP': str(root / 'remote')}
        return subprocess.run([self.ansible, '-i', 'localhost,', str(root / 'play.json')],
                              env=env, capture_output=True, text=True, timeout=120)

    def writes(self, root):
        path = root / 'writes'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_enabled_agent_gets_a_keyed_heartbeat_once(self):
        with tempfile.TemporaryDirectory(prefix='heartbeat-') as tmp:
            root = Path(tmp)
            first = self.run_heartbeat(root, enabled=True)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual(self.writes(root), [['config', 'set', 'agents.entries.henning.heartbeat', '--json',
                                                  '{"target": "telegram", "to": "123", "every": "12h"}']])
            (root / 'writes').unlink()
            second = self.run_heartbeat(root, enabled=True)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertEqual(self.writes(root), [])

    def test_disabled_agent_without_heartbeat_needs_no_write(self):
        with tempfile.TemporaryDirectory(prefix='heartbeat-') as tmp:
            root = Path(tmp)
            result = self.run_heartbeat(root, enabled=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(self.writes(root), [])


if __name__ == '__main__':
    unittest.main()
