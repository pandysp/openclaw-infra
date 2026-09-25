#!/usr/bin/env python3
"""Run the agents role's real creation tasks through Ansible with a fake OpenClaw CLI.

Only a fresh server has agents missing, so production never exercises the
"absent" branch; this test does.
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
ROLE_TASKS = ROOT / 'ansible/roles/agents/tasks/main.yml'
SELECTED = ('Get existing agents', 'Write existing agents JSON to temp file')
FAKE_OPENCLAW = '''#!/bin/sh
if [ "$1 $2 $3" = "agents list --json" ]; then
    printf '%s\\n' "$FIXTURE_AGENTS_LIST"
    exit 0
fi
if [ "$1 $2" = "agents add" ]; then
    printf '%s\\n' "$*" >> "$FIXTURE_ROOT/added"
    exit 0
fi
echo "unexpected openclaw call: $*" >&2
exit 97
'''


class AgentCreationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ansible = shutil.which('ansible-playbook')
        if not cls.ansible:
            raise RuntimeError("Install the project's Ansible development dependency first")
        # Parse with the YAML library that ships with Ansible itself.
        python = shlex.split(Path(cls.ansible).read_text().splitlines()[0].removeprefix('#!'))
        code = 'import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))'
        tasks = json.loads(subprocess.run([*python, '-c', code, str(ROLE_TASKS)],
                                          capture_output=True, text=True, check=True).stdout)
        cls.tasks = [task for task in tasks
                     if task.get('name') in SELECTED
                     or any(inner.get('name') in SELECTED for inner in task.get('block', []))]
        assert len(cls.tasks) == 2, 'The agents role no longer has the expected creation tasks'

    def run_role(self, agents_list):
        with tempfile.TemporaryDirectory(prefix='agent-creation-') as tmp:
            root = Path(tmp)
            (root / 'bin').mkdir()
            fake = root / 'bin/openclaw'
            fake.write_text(FAKE_OPENCLAW)
            fake.chmod(0o700)
            play = [{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                     'vars': {'openclaw_agents': [{'id': 'main', 'is_default': True},
                                                  {'id': 'test', 'is_default': False}]},
                     'tasks': self.tasks,
                     'handlers': [{'name': 'restart openclaw-gateway',
                                   'ansible.builtin.debug': {'msg': 'restart requested'}}]}]
            playbook = root / 'play.json'
            playbook.write_text(json.dumps(play))
            env = {'PATH': f"{root / 'bin'}:{os.environ['PATH']}", 'HOME': tmp,
                   'FIXTURE_ROOT': tmp, 'FIXTURE_AGENTS_LIST': agents_list,
                   'ANSIBLE_LOCAL_TEMP': str(root / 'ansible-local'),
                   'ANSIBLE_REMOTE_TEMP': str(root / 'ansible-remote'), 'ANSIBLE_NOCOLOR': '1'}
            result = subprocess.run([self.ansible, '-i', 'localhost,', str(playbook)],
                                    env=env, capture_output=True, text=True, timeout=120)
            added = (root / 'added').read_text().splitlines() if (root / 'added').exists() else []
            return result, added

    def test_a_fresh_server_creates_the_missing_agent(self):
        result, added = self.run_role('[{"id": "main"}]')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(added, ['agents add test --non-interactive --workspace /home/ubuntu/.openclaw/workspace-test'])
        self.assertRegex(result.stdout, r'localhost\s+: ok=\d+\s+changed=1 ')

    def test_an_existing_agent_is_left_alone(self):
        result, added = self.run_role('[{"id": "main"}, {"id": "test"}]')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(added, [])
        # changed_when keys on 'already exists', so an unchanged recap proves that branch.
        self.assertRegex(result.stdout, r'localhost\s+: ok=\d+\s+changed=0 ')

    def test_an_invalid_agent_list_fails_before_creating_anything(self):
        for agents_list in ('not json', '{"id": "main"}'):
            with self.subTest(agents_list=agents_list):
                result, added = self.run_role(agents_list)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(added, [])


if __name__ == '__main__':
    unittest.main()
