"""Render the real per-agent SSH config through Ansible, with the real Mac-access selection."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / 'ansible/roles/claude-cli/templates/ssh-config.j2'
TASKS = ROOT / 'ansible/roles/claude-cli/tasks/main.yml'
SELECT = 'Select the agents with Mac access'
DERIVE = 'Derive per-agent SSH metadata from workspace definitions'
AGENTS = [{'id': 'main', 'mac_access': True}, {'id': 'other'}, {'id': 'third', 'mac_access': False},
          {'id': 'quoted', 'mac_access': 'false'}]


class SshConfigTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ansible = shutil.which('ansible-playbook')
        if not cls.ansible:
            raise RuntimeError('Install the project Ansible dependency')
        python = shlex.split(Path(cls.ansible).read_text().splitlines()[0].removeprefix('#!'))
        tasks = json.loads(subprocess.check_output([*python, '-c',
            'import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))', str(TASKS)], text=True))
        cls.select = next(task for task in tasks if task.get('name') == SELECT)
        cls.derive = next(task for task in tasks if task.get('name') == DERIVE)

    def render(self, agent, mac_host='mac-air', repo_url=''):
        with tempfile.TemporaryDirectory(prefix='ssh-config-') as directory:
            root = Path(directory)
            play = root / 'play.json'
            play.write_text(json.dumps([{
                'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                'vars': {'openclaw_claude_cli_mac_host': mac_host, 'openclaw_claude_cli_mac_user': 'fixture-user',
                         'openclaw_agents': AGENTS,
                         'item': {'agent_id': agent, 'repo_url': repo_url, 'ssh_alias': 'github-workspace-' + agent}},
                'tasks': [self.select,
                          {'ansible.builtin.template': {'src': str(TEMPLATE), 'dest': str(root / 'out.conf')}}]}]))
            env = {**os.environ, 'ANSIBLE_NOCOLOR': '1', 'ANSIBLE_LOCAL_TEMP': str(root / 'local')}
            result = subprocess.run([self.ansible, '-i', 'localhost,', str(play)], env=env,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout[-2000:])
            return (root / 'out.conf').read_text()

    def test_agent_with_mac_access_gets_the_pinned_block(self):
        rendered = self.render('main')
        self.assertIn('Host mac-air\n    User fixture-user\n    IdentityFile ~/.ssh/id_ed25519_openclaw_mac_air', rendered)
        self.assertIn('StrictHostKeyChecking yes', rendered)

    def test_agents_without_mac_access_get_no_mac_block(self):
        for agent in ('other', 'third', 'quoted'):
            with self.subTest(agent=agent):
                rendered = self.render(agent, repo_url='git@github.com:owner/repo.git')
                self.assertNotIn('openclaw_mac_air', rendered)
                self.assertIn('Host github.com github-workspace-' + agent, rendered)

    def test_launcher_runtime_gives_the_mac_host_only_to_agents_with_access(self):
        with tempfile.TemporaryDirectory(prefix='ssh-runtime-') as directory:
            root = Path(directory)
            play = root / 'play.json'
            workspaces = [{'agent_id': agent['id'], 'repo_url': '', 'key_file': 'unused'} for agent in AGENTS]
            play.write_text(json.dumps([{
                'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                'vars': {'openclaw_claude_cli_mac_host': 'mac-air', 'openclaw_agents': AGENTS,
                         '_openclaw_workspaces': workspaces, '_claude_cli_ssh': {}},
                'tasks': [self.select, self.derive,
                          {'ansible.builtin.copy': {'content': '{{ _claude_cli_ssh | to_json }}', 'dest': str(root / 'ssh.json')}}]}]))
            env = {**os.environ, 'ANSIBLE_NOCOLOR': '1', 'ANSIBLE_LOCAL_TEMP': str(root / 'local')}
            result = subprocess.run([self.ansible, '-i', 'localhost,', str(play)], env=env,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout[-2000:])
            ssh = json.loads((root / 'ssh.json').read_text())
        self.assertEqual({agent: entry['mac_host'] for agent, entry in ssh.items()},
                         {'main': 'mac-air', 'other': '', 'third': '', 'quoted': ''})

    def test_no_mac_host_means_no_mac_block_for_anyone(self):
        self.assertNotIn('openclaw_mac_air', self.render('main', mac_host=''))


if __name__ == '__main__':
    unittest.main()
