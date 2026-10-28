"""Run Ansible's real restoration boundary with failed external preparation."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from test_playbook_order import load_yaml

ROOT = Path(__file__).resolve().parents[2]


class WorkspaceTimerRestoreTest(unittest.TestCase):
    def test_failed_preparation_restores_existing_timers(self):
        source, = load_yaml(ROOT / 'ansible/roles/workspace/tasks/main.yml')
        for failure in ('Read official GitHub SSH host keys', 'Build workspace sync image'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory(prefix='workspace-restore-') as tmp:
                root = Path(tmp)

                def fixture(task):
                    # Preserve the production block/always structure and unit loops;
                    # replace network, Docker and systemd only at their boundaries.
                    if 'block' in task:
                        return {key: [fixture(child) for child in value]
                                if key in ('block', 'always', 'rescue') else value
                                for key, value in task.items()}
                    name = task.get('name', '')
                    if name == failure:
                        return {'name': name, 'ansible.builtin.fail': {'msg': 'injected preparation failure'}}
                    if name in ('Stop existing workspace units during installation',
                                'Restore the workspace timers that existed before this run'):
                        unit = task['ansible.builtin.systemd']
                        return {'name': name, 'ansible.builtin.copy': {
                            'dest': '{{ fixture_dir }}/' + unit['name'],
                            'content': unit['state']},
                            **{key: task[key] for key in ('loop', 'loop_control', 'when') if key in task}}
                    return {'name': name, 'ansible.builtin.debug': {'msg': 'external boundary omitted'}}

                units = [{'stat': {'exists': True, 'path': f'/fixture/workspace-git-sync-{agent}.timer'},
                          'item': [{'agent_id': agent}, 'timer']} for agent in ('main', 'second')]
                play = [{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                         'vars': {'fixture_dir': tmp, 'workspace_existing_units': {'results': units}},
                         'tasks': [fixture(task) for task in source]}]
                (root / 'play.json').write_text(json.dumps(play))
                env = {'PATH': os.environ['PATH'], 'HOME': tmp, 'ANSIBLE_NOCOLOR': '1',
                       'ANSIBLE_LOCAL_TEMP': str(root / 'local'), 'ANSIBLE_REMOTE_TEMP': str(root / 'remote')}
                result = subprocess.run([shutil.which('ansible-playbook'), '-i', 'localhost,', str(root / 'play.json')],
                                        capture_output=True, text=True, env=env, timeout=120)
                self.assertNotEqual(result.returncode, 0, 'Preparation failure was swallowed')
                self.assertIn('injected preparation failure', result.stdout, result.stdout + result.stderr)
                for agent in ('main', 'second'):
                    self.assertEqual((root / f'workspace-git-sync-{agent}.timer').read_text(), 'started',
                                     result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
