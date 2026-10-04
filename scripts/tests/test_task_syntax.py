"""Run the syntax gate on real tasks, without executing their commands."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CHECK = ROOT / 'scripts/check-task-syntax.sh'
CONFIG = ROOT / 'ansible/roles/config/tasks/main.yml'


class TaskSyntaxTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ansible = shutil.which('ansible-playbook')
        if not ansible:
            raise RuntimeError('Install the project Ansible dependency')
        python = shlex.split(Path(ansible).read_text().splitlines()[0].removeprefix('#!'))
        tasks = json.loads(subprocess.check_output([*python, '-c',
            'import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))', str(CONFIG)], text=True))
        cls.configure = next(task for block in tasks for task in block.get('block', [])
                             if task.get('name') == 'Configure gateway settings')

    def run_check(self, task):
        with tempfile.TemporaryDirectory(prefix='task-syntax-') as directory:
            root = Path(directory)
            scripts = root / 'scripts'
            tasks = root / 'ansible/roles/fixture/tasks'
            scripts.mkdir()
            tasks.mkdir(parents=True)
            shutil.copy2(CHECK, scripts / CHECK.name)
            (tasks / 'main.yml').write_text(json.dumps([task]))
            env = {**os.environ, 'ANSIBLE_NOCOLOR': '1', 'ANSIBLE_LOCAL_TEMP': str(root / 'local')}
            return subprocess.run(['bash', str(scripts / CHECK.name)], env=env,
                                  capture_output=True, text=True, timeout=60)

    def test_real_templated_shell_task_parses_without_running(self):
        result = self.run_check(self.configure)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unbalanced_comment_in_real_shell_task_fails(self):
        task = dict(self.configure)
        original = task['ansible.builtin.shell']
        self.assertEqual(original.count('Exec tool policy for OpenClaw itself'), 1)
        task['ansible.builtin.shell'] = original.replace(
            'Exec tool policy for OpenClaw itself', "OpenClaw's own exec tool")
        result = self.run_check({'block': [task], 'always': [{'ansible.builtin.debug': {'msg': 'cleanup'}}]})
        self.assertNotEqual(result.returncode, 0, 'Gate accepted the exact comment that broke provisioning')
        self.assertIn('failed at splitting arguments', result.stdout + result.stderr)
        self.assertIn('main.yml', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
