"""Reject obsolete/manual MCP definitions before any role can change the server."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK = ROOT / 'ansible/playbook.yml'
VALIDATE = 'Validate supported MCP server definitions before provisioning'


class McpServerInputsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ansible = shutil.which('ansible-playbook')
        if not cls.ansible:
            raise RuntimeError('Install the project Ansible dependency')
        python = shlex.split(Path(cls.ansible).read_text().splitlines()[0].removeprefix('#!'))
        plays, plugins = json.loads(subprocess.check_output([*python, '-c',
            'import json,sys,yaml; print(json.dumps([yaml.safe_load(open(p)) for p in sys.argv[1:]]))',
            str(PLAYBOOK), str(ROOT / 'ansible/roles/plugins/tasks/main.yml')], text=True))
        cls.validate = next(task for play in plays for task in play.get('pre_tasks', [])
                            if task.get('name') == VALIDATE)
        cls.proxy = next(task for task in plugins if task.get('name') == 'Set proxy_required fact')

    def run_validation(self, servers):
        with tempfile.TemporaryDirectory(prefix='mcp-inputs-') as directory:
            root = Path(directory)
            marker = root / 'role-started'
            play = root / 'play.json'
            play.write_text(json.dumps([{
                'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                'vars': {'openclaw_agents': [{'id': 'main'}], '_openclaw_mcp_servers': servers},
                'tasks': [self.validate, self.proxy,
                          {'ansible.builtin.copy': {'content': 'started', 'dest': str(marker)}}]}]))
            env = {**os.environ, 'ANSIBLE_NOCOLOR': '1', 'ANSIBLE_LOCAL_TEMP': str(root / 'local')}
            result = subprocess.run([self.ansible, '-i', 'localhost,', str(play)], env=env,
                                    capture_output=True, text=True, timeout=60)
            return result, marker.exists()

    def test_supported_definitions_and_empty_list_pass(self):
        for servers in ([], [{'name': 'github', 'type': 'github', 'agent_id': 'main', 'token_var': 'github_token'},
                            {'name': 'qmd', 'type': 'qmd', 'agent_id': 'main'}]):
            with self.subTest(servers=servers):
                result, started = self.run_validation(servers)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(started)

    def test_unsupported_type_fails_before_roles_start(self):
        result, started = self.run_validation([{'name': 'old', 'type': 'retired', 'agent_id': 'main'}])
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(started)
        self.assertIn('Remove unsupported server types', result.stdout + result.stderr)

    def test_legacy_shared_manual_shape_has_a_clear_error(self):
        result, started = self.run_validation([{'name': 'old', 'type': 'retired'}])
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(started)
        self.assertIn('update manual openclaw_mcp_servers overrides', result.stdout + result.stderr)
        self.assertNotIn("has no attribute 'agent_id'", result.stdout + result.stderr)

    def test_github_needs_a_nonempty_string_token_variable(self):
        for value in ({}, {'token_var': ''}, {'token_var': None}, {'token_var': 42}):
            with self.subTest(value=value):
                entry = {'name': 'github', 'type': 'github', 'agent_id': 'main', **value}
                result, started = self.run_validation([entry])
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(started)
                self.assertIn('string token_var', result.stdout + result.stderr)

    def test_missing_name_missing_agent_and_unknown_agent_fail(self):
        for entry in ({'type': 'github', 'agent_id': 'main'}, {'name': 'github', 'type': 'github'},
                      {'name': 'github', 'type': 'github', 'agent_id': 'unknown'}):
            with self.subTest(entry=entry):
                result, started = self.run_validation([entry])
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(started)


if __name__ == '__main__':
    unittest.main()
