#!/usr/bin/env python3
"""Run the playbook's real workspace derivation, the role's real input check and
the real unit template through Ansible, fed the repository URL forms that exist.

setup-workspace.sh writes git@github.com:owner/repo.git; older deployments store
the agent's own SSH alias (git@github-workspace-<agent>:owner/repo.git), which
the pre-isolation sync accepted. Both must keep working; nothing else may.
"""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
DERIVE = 'Auto-generate workspaces from agents'
VALIDATE = 'Validate workspace service inputs'


class WorkspaceInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ansible = shutil.which('ansible-playbook')
        if not cls.ansible:
            raise RuntimeError("Install the project's Ansible development dependency first")
        python = shlex.split(Path(cls.ansible).read_text().splitlines()[0].removeprefix('#!'))
        code = 'import json,sys,yaml; print(json.dumps([yaml.safe_load(open(p)) for p in sys.argv[1:]]))'
        playbook, role = json.loads(subprocess.run(
            [*python, '-c', code, str(ROOT / 'ansible/playbook.yml'), str(ROOT / 'ansible/roles/workspace/tasks/main.yml')],
            capture_output=True, text=True, check=True).stdout)
        pre_tasks = [task for play in playbook for task in play.get('pre_tasks', [])]
        derive = [task for task in pre_tasks if task.get('name') == DERIVE]
        validate = [task for task in role if task.get('name') == VALIDATE]
        active = [task for task in role if 'active_workspaces' in json.dumps(task.get('ansible.builtin.set_fact', {}))]
        assert len(derive) == 1 and len(validate) == 1 and len(active) == 1, 'Workspace tasks moved'
        cls.tasks = [dict(derive[0], tags=[]), active[0], validate[0],
                     {'name': 'Render real unit', 'ansible.builtin.template': {
                         'src': str(ROOT / 'ansible/roles/workspace/templates/workspace-git-sync.service.j2'),
                         'dest': '{{ render_dir }}/{{ item.agent_id }}.service'},
                      'loop': '{{ active_workspaces }}'}]

    def run_play(self, urls):
        with tempfile.TemporaryDirectory(prefix='workspace-inputs-') as tmp:
            root = Path(tmp)
            variables = {'openclaw_agents': [{'id': agent} for agent in urls], '_openclaw_workspaces': [],
                         'render_dir': tmp,
                         **{('workspace_repo_url' if agent == 'main' else f'workspace_{agent}_repo_url'): url
                            for agent, url in urls.items()}}
            play = [{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                     'vars': variables, 'tasks': self.tasks}]
            (root / 'play.json').write_text(json.dumps(play))
            env = {'PATH': os.environ['PATH'], 'HOME': tmp, 'ANSIBLE_NOCOLOR': '1',
                   'ANSIBLE_LOCAL_TEMP': str(root / 'local'), 'ANSIBLE_REMOTE_TEMP': str(root / 'remote')}
            result = subprocess.run([self.ansible, '-i', 'localhost,', str(root / 'play.json')],
                                    env=env, capture_output=True, text=True, timeout=120)
            units = {path.stem: path.read_text() for path in root.glob('*.service')}
            return result, units

    def repository_in(self, unit):
        return re.search(r'--env WORKSPACE_REPOSITORY=(\S+)', unit).group(1)

    def test_canonical_and_own_alias_urls_render_the_agents_alias(self):
        result, units = self.run_play({
            'main': 'git@github-workspace-main:pandysp/openclaw-workspace.git',
            'henning': 'git@github-workspace-henning:pandysp/openclaw-workspace-henning.git',
            'cama': 'git@github.com:pandysp/openclaw-workspace-cama.git'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual({agent: self.repository_in(unit) for agent, unit in units.items()}, {
            'main': 'git@github-workspace-main:pandysp/openclaw-workspace.git',
            'henning': 'git@github-workspace-henning:pandysp/openclaw-workspace-henning.git',
            'cama': 'git@github-workspace-cama:pandysp/openclaw-workspace-cama.git'})

    def test_another_agents_alias_or_host_is_rejected(self):
        for url in ('git@github-workspace-main:pandysp/openclaw-workspace-henning.git',
                    'git@gitlab.com:pandysp/openclaw-workspace-henning.git',
                    'https://github.com/pandysp/openclaw-workspace-henning.git'):
            with self.subTest(url=url):
                result, units = self.run_play({'henning': url})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Workspace service inputs must be valid GitHub SSH URLs', result.stdout)
                self.assertEqual(units, {})


if __name__ == '__main__':
    unittest.main()
