"""Render the real per-agent SSH config template through Ansible."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

TEMPLATE = Path(__file__).resolve().parents[2] / 'ansible/roles/claude-cli/templates/ssh-config.j2'


class SshConfigTest(unittest.TestCase):
    def render(self, mac_host, mac_user, repo_url=''):
        ansible = shutil.which('ansible-playbook')
        self.assertIsNotNone(ansible, 'Install the project Ansible dependency')
        with tempfile.TemporaryDirectory(prefix='ssh-config-') as directory:
            root = Path(directory)
            play = root / 'play.json'
            play.write_text(json.dumps([{
                'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                'vars': {'openclaw_claude_cli_mac_host': mac_host, 'openclaw_claude_cli_mac_user': mac_user,
                         'item': {'repo_url': repo_url, 'ssh_alias': 'github-workspace-main'}},
                'tasks': [{'ansible.builtin.template': {'src': str(TEMPLATE), 'dest': str(root / 'out.conf')}}]}]))
            env = {**os.environ, 'ANSIBLE_NOCOLOR': '1', 'ANSIBLE_LOCAL_TEMP': str(root / 'local')}
            result = subprocess.run([ansible, '-i', 'localhost,', str(play)], env=env,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout[-2000:])
            return (root / 'out.conf').read_text()

    def test_no_mac_host_renders_no_mac_block(self):
        rendered = self.render('', '', repo_url='git@github.com:owner/repo.git')
        self.assertNotIn('openclaw_mac_air', rendered)
        self.assertIn('Host github.com github-workspace-main', rendered)

    def test_mac_host_renders_the_pinned_block(self):
        rendered = self.render('mac-air', 'fixture-user')
        self.assertIn('Host mac-air\n    User fixture-user\n    IdentityFile ~/.ssh/id_ed25519_openclaw_mac_air', rendered)
        self.assertIn('StrictHostKeyChecking yes', rendered)


if __name__ == '__main__':
    unittest.main()
