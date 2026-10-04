"""Run the real GitHub token tasks through Ansible against a fresh home."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / 'ansible/roles/plugins/tasks/main.yml'
TOKENS = '/home/ubuntu/.openclaw/github-tokens'


class TokenFileTest(unittest.TestCase):
    def test_token_files_are_written_on_a_fresh_host(self):
        ansible = shutil.which('ansible-playbook')
        self.assertIsNotNone(ansible, 'Install the project Ansible dependency')
        python = shlex.split(Path(ansible).read_text().splitlines()[0].removeprefix('#!'))
        tasks = json.loads(subprocess.check_output([*python, '-c',
            'import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))', str(TASKS)], text=True))
        # Every task touching the token directory, in file order.
        selected = [task for task in tasks if TOKENS in json.dumps(task)]
        self.assertGreaterEqual(len(selected), 2)
        with tempfile.TemporaryDirectory(prefix='plugin-tokens-') as directory:
            root = Path(directory)
            tokens = root / 'github-tokens'
            play = root / 'play.json'
            play.write_text(json.dumps([{
                'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                'vars': {'_openclaw_mcp_servers': [{'type': 'github', 'agent_id': 'main', 'token_var': 'fixture_token'}],
                         'fixture_token': 'synthetic-noncredential'},
                'tasks': json.loads(json.dumps(selected).replace(TOKENS, str(tokens)))}]))
            env = {**os.environ, 'ANSIBLE_NOCOLOR': '1', 'ANSIBLE_LOCAL_TEMP': str(root / 'local'),
                   'ANSIBLE_REMOTE_TEMP': str(root / 'remote')}
            result = subprocess.run([ansible, '-i', 'localhost,', str(play)], env=env,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout[-2000:])
            self.assertEqual((tokens / 'main').read_text(), 'synthetic-noncredential')
            self.assertEqual(tokens.stat().st_mode & 0o777, 0o700)
            self.assertEqual((tokens / 'main').stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
