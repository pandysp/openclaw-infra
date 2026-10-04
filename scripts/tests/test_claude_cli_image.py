"""Run the real image inspect/build tasks through Ansible with the Docker CLI faked."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / 'ansible/roles/claude-cli/tasks/main.yml'
NAMES = ('Inspect the existing container CLI image', 'Build container CLI image')


class ImageInspectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ansible = shutil.which('ansible-playbook')
        if not cls.ansible:
            raise RuntimeError('Install the project Ansible dependency')
        python = shlex.split(Path(cls.ansible).read_text().splitlines()[0].removeprefix('#!'))
        tasks = json.loads(subprocess.check_output([*python, '-c',
            'import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))', str(TASKS)], text=True))
        cls.tasks = [task for task in tasks if task.get('name') in NAMES]
        assert [task['name'] for task in cls.tasks] == list(NAMES)

    def run_inspect(self, rc, stderr):
        """Return whether the play succeeded and whether the build ran."""
        with tempfile.TemporaryDirectory(prefix='cli-image-') as directory:
            root = Path(directory)
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            docker = bin_dir / 'docker'
            docker.write_text(f'''#!/bin/sh
case "$1" in
  image) printf '%s\\n' {shlex.quote(stderr)} >&2; [ {rc} -eq 0 ] && echo sha256:fixture; exit {rc} ;;
  build) touch {shlex.quote(str(root / 'built'))}; exit 0 ;;
esac
exit 2
''')
            docker.chmod(0o700)
            play = root / 'play.json'
            play.write_text(json.dumps([{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                                         'tasks': self.tasks}]))
            env = {**os.environ, 'PATH': str(bin_dir) + os.pathsep + os.environ['PATH'], 'ANSIBLE_NOCOLOR': '1',
                   'ANSIBLE_LOCAL_TEMP': str(root / 'local'), 'ANSIBLE_REMOTE_TEMP': str(root / 'remote')}
            result = subprocess.run([self.ansible, '-i', 'localhost,', str(play)],
                                    env=env, capture_output=True, text=True, timeout=60)
            return result.returncode == 0, (root / 'built').exists()

    def test_missing_image_proceeds_to_first_build(self):
        # Docker 29 wording, verified on the VPS; older clients omit "response from daemon".
        for message in ('Error response from daemon: No such image: openclaw-claude-cli:latest',
                        'Error: No such image: openclaw-claude-cli:latest'):
            with self.subTest(message=message):
                self.assertEqual(self.run_inspect(1, message), (True, True))

    def test_existing_image_rebuilds(self):
        self.assertEqual(self.run_inspect(0, ''), (True, True))

    def test_inspection_errors_stop_before_build(self):
        for message in ('Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?',
                        'permission denied while trying to connect to the Docker daemon socket',
                        'Error response from daemon: No such image: some-other-image:latest'):
            with self.subTest(message=message):
                self.assertEqual(self.run_inspect(1, message), (False, False))


if __name__ == '__main__':
    unittest.main()
