"""Run the real shared-login tasks through Ansible against a temporary home.

systemd and the claude CLI are the faked boundaries.
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
TASKS = ROOT / 'ansible/roles/openclaw/tasks/claude-cli-auth.yml'
FIRST = 'Create the shared Claude login folder'
TOKEN = 'synthetic-setup-token'
EXPORT = 'export CLAUDE_SECURESTORAGE_CONFIG_DIR=/home/ubuntu/.claude/shared/auth'


class SharedLoginTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ansible = shutil.which('ansible-playbook')
        if not cls.ansible:
            raise RuntimeError('Install Ansible before running the shared-login tests')
        python = shlex.split(Path(cls.ansible).read_text().splitlines()[0].removeprefix('#!'))
        tasks = json.loads(subprocess.check_output([*python, '-c',
            'import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))', str(TASKS)], text=True))
        names = [task['name'] for task in tasks]
        cls.tasks = tasks[names.index(FIRST):]

    def run_tasks(self, home, logged_in=True):
        bin_dir = home / '.npm-global/bin'
        bin_dir.mkdir(parents=True, exist_ok=True)
        claude = bin_dir / 'claude'
        claude.write_text('#!/bin/sh\n'
                          f'test "$CLAUDE_SECURESTORAGE_CONFIG_DIR" = "{home}/.claude/shared/auth" || exit 3\n'
                          f'echo \'{{"loggedIn": {"true" if logged_in else "false"}}}\'\n')
        claude.chmod(0o700)
        tasks = json.loads(json.dumps(self.tasks).replace('/home/ubuntu', str(home)))
        for task in tasks:
            if 'ansible.builtin.systemd' in task:  # boundary: no real user systemd
                task.pop('ansible.builtin.systemd'); task.pop('environment', None)
                task['ansible.builtin.debug'] = {'msg': 'daemon-reload'}
        play = home / 'play.json'
        play.write_text(json.dumps([{
            'hosts': 'localhost', 'connection': 'local', 'gather_facts': True,
            'vars': {'claude_setup_token': TOKEN}, 'tasks': tasks,
            'handlers': [{'name': 'restart openclaw-gateway', 'ansible.builtin.debug': {'msg': 'RESTART-HANDLER'}}]}]))
        env = {**os.environ, 'ANSIBLE_NOCOLOR': '1', 'ANSIBLE_LOCAL_TEMP': str(home / 'local'),
               'ANSIBLE_REMOTE_TEMP': str(home / 'remote')}
        result = subprocess.run([self.ansible, '-i', 'localhost,', str(play)], env=env,
                                capture_output=True, text=True, timeout=120)
        self.assertNotIn(TOKEN, result.stdout + result.stderr)
        return result

    def home(self):
        directory = tempfile.TemporaryDirectory(prefix='claude-login-')
        self.addCleanup(directory.cleanup)
        home = Path(directory.name).resolve()
        (home / '.claude').mkdir()
        return home

    def assert_converged(self, home):
        again = self.run_tasks(home)
        self.assertEqual(again.returncode, 0, again.stdout[-2000:])
        self.assertIn('changed=0', again.stdout)
        self.assertNotIn('RESTART-HANDLER', again.stdout)

    def test_fresh_host_gets_the_setup_token_login(self):
        home = self.home()
        result = self.run_tasks(home)
        self.assertEqual(result.returncode, 0, result.stdout[-2000:])
        login = home / '.claude/shared/auth/.credentials.json'
        oauth = json.loads(login.read_text())['claudeAiOauth']
        self.assertEqual((oauth['accessToken'], oauth['refreshToken']), (TOKEN, ''))
        self.assertEqual(login.stat().st_mode & 0o777, 0o600)
        self.assertEqual(login.parent.stat().st_mode & 0o777, 0o700)
        dropin = home / '.config/systemd/user/openclaw-gateway.service.d/claude-auth.conf'
        self.assertEqual(dropin.read_text(),
                         f'[Service]\nEnvironment=CLAUDE_SECURESTORAGE_CONFIG_DIR={home}/.claude/shared/auth\n')
        for profile in ('.bashrc', '.profile'):
            self.assertIn(EXPORT.replace('/home/ubuntu', str(home)), (home / profile).read_text())
        self.assertIn('RESTART-HANDLER', result.stdout)
        self.assert_converged(home)

    def test_existing_login_is_never_replaced(self):
        home = self.home()
        storage = home / '.claude/shared/auth'
        storage.mkdir(parents=True, mode=0o700)
        existing = {'claudeAiOauth': {'accessToken': 'synthetic-live', 'refreshToken': 'synthetic-refresh'}}
        login = storage / '.credentials.json'
        login.write_text(json.dumps(existing))
        login.chmod(0o600)
        before = login.read_bytes()
        result = self.run_tasks(home)
        self.assertEqual(result.returncode, 0, result.stdout[-2000:])
        self.assertEqual(login.read_bytes(), before)
        self.assert_converged(home)
        self.assertEqual(login.read_bytes(), before)

    def test_unreadable_login_fails_the_run(self):
        result = self.run_tasks(self.home(), logged_in=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Verify Claude reads the shared login', result.stdout)


if __name__ == '__main__':
    unittest.main()
