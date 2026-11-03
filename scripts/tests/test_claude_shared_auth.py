"""Run the real shared-login tasks through Ansible against a temporary home,
and the login expiry check against fixture logins.

systemd, the claude CLI and the openclaw CLI are the faked boundaries.
"""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / 'ansible/roles/openclaw/tasks/claude-cli-auth.yml'
FIRST = 'Create the shared Claude login folder'
TOKEN = 'synthetic-setup-token'
EXPIRES = '2027-11-03'
EXPIRES_MS = 1825200000000  # 2027-11-03T00:00:00Z
CHECK = ROOT / 'ansible/roles/openclaw/files/claude-login-expiry.py'
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
            copy = task.get('ansible.builtin.copy') or {}
            if 'src' in copy:  # outside the role: resolve its files/ folder
                copy['src'] = str(ROOT / 'ansible/roles/openclaw/files' / copy['src'])
            if str(task.get('ansible.builtin.command', '')).startswith('systemctl '):
                task.pop('ansible.builtin.command'); task.pop('environment', None)
                task['ansible.builtin.debug'] = {'msg': 'systemctl'}
        play = home / 'play.json'
        play.write_text(json.dumps([{
            'hosts': 'localhost', 'connection': 'local', 'gather_facts': True,
            'vars': {'claude_setup_token': TOKEN, 'claude_setup_token_expires': EXPIRES,
                     'claude_login_warn_days': 21,
                     'openclaw_agents': [{'id': 'main', 'is_default': True,
                                          'deliver_channel': 'telegram', 'deliver_to': '12345'}]},
            'tasks': tasks,
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
        self.assertEqual((oauth['accessToken'], oauth['refreshToken'], oauth['expiresAt']), (TOKEN, '', EXPIRES_MS))
        self.assertEqual(login.stat().st_mode & 0o777, 0o600)
        self.assertEqual(login.parent.stat().st_mode & 0o777, 0o700)
        dropin = home / '.config/systemd/user/openclaw-gateway.service.d/claude-auth.conf'
        self.assertEqual(dropin.read_text(),
                         f'[Service]\nEnvironment=CLAUDE_SECURESTORAGE_CONFIG_DIR={home}/.claude/shared/auth\n')
        for profile in ('.bashrc', '.profile'):
            self.assertIn(EXPORT.replace('/home/ubuntu', str(home)), (home / profile).read_text())
        service = (home / '.config/systemd/user/claude-login-expiry.service').read_text()
        for line in (f'CLAUDE_LOGIN_CREDENTIALS={home}/.claude/shared/auth/.credentials.json',
                     'CLAUDE_LOGIN_WARN_DAYS=21', 'OWNER_CHANNEL=telegram', 'OWNER_TARGET=12345',
                     f'ExecStart={home}/.local/bin/claude-login-expiry'):
            self.assertIn(line, service)
        self.assertEqual((home / '.local/bin/claude-login-expiry').read_bytes(), CHECK.read_bytes())
        self.assertIn('RESTART-HANDLER', result.stdout)
        self.assert_converged(home)

    def test_any_other_login_is_replaced_with_the_setup_token(self):
        # An interactive login renews itself only for 30 days; this one ran out.
        home = self.home()
        storage = home / '.claude/shared/auth'
        storage.mkdir(parents=True, mode=0o700)
        login = storage / '.credentials.json'
        login.write_text(json.dumps({'claudeAiOauth': {'accessToken': '', 'refreshToken': '', 'expiresAt': 0,
                                                       'refreshTokenExpiresAt': 1793677550771}}))
        login.chmod(0o600)
        result = self.run_tasks(home)
        self.assertEqual(result.returncode, 0, result.stdout[-2000:])
        oauth = json.loads(login.read_text())['claudeAiOauth']
        self.assertEqual((oauth['accessToken'], oauth['expiresAt']), (TOKEN, EXPIRES_MS))
        self.assertNotIn('refreshTokenExpiresAt', oauth)
        self.assertIn('RESTART-HANDLER', result.stdout)
        self.assert_converged(home)

    def test_unreadable_login_fails_the_run(self):
        result = self.run_tasks(self.home(), logged_in=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Verify Claude reads the shared login', result.stdout)



class ExpiryCheckTest(unittest.TestCase):
    """The real check script; only the openclaw CLI is faked."""

    def run_check(self, oauth, send_exit=0):
        directory = tempfile.TemporaryDirectory(prefix='claude-expiry-')
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        login = root / '.credentials.json'
        login.write_text(json.dumps({'claudeAiOauth': oauth}))
        sent = root / 'sent.json'
        openclaw = root / 'openclaw'
        openclaw.write_text(f'#!/usr/bin/env python3\nimport json,sys\n'
                            f'open({str(sent)!r},"w").write(json.dumps(sys.argv[1:]))\nsys.exit({send_exit})\n')
        openclaw.chmod(0o700)
        env = {**os.environ, 'CLAUDE_LOGIN_CREDENTIALS': str(login), 'CLAUDE_LOGIN_WARN_DAYS': '21',
               'OPENCLAW_BIN': str(openclaw), 'OWNER_CHANNEL': 'telegram', 'OWNER_TARGET': '12345'}
        result = subprocess.run([sys.executable, str(CHECK)], env=env, capture_output=True, text=True, timeout=30)
        return result, (json.loads(sent.read_text()) if sent.exists() else None)

    @staticmethod
    def in_days(days):
        return int((time.time() + days * 86400) * 1000)

    def assert_told_owner(self, sent, words):
        self.assertEqual(sent[:5], ['message', 'send', '--channel', 'telegram', '--target'])
        self.assertEqual(sent[5:7], ['12345', '--message'])
        self.assertIn(words, sent[7])

    def test_a_distant_end_sends_nothing(self):
        result, sent = self.run_check({'accessToken': TOKEN, 'refreshToken': '', 'expiresAt': self.in_days(300)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(sent)
        self.assertIn('OK:', result.stdout)

    def test_a_near_end_tells_the_owner(self):
        result, sent = self.run_check({'accessToken': TOKEN, 'refreshToken': '', 'expiresAt': self.in_days(10.5)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_told_owner(sent, 'in 10 days')

    def test_a_renewing_login_ends_at_its_sign_in_limit(self):
        # Its access token looks fresh, but renewal stops at refreshTokenExpiresAt.
        result, sent = self.run_check({'accessToken': TOKEN, 'refreshToken': 'synthetic-refresh',
                                       'expiresAt': self.in_days(0.3), 'refreshTokenExpiresAt': self.in_days(5.5)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_told_owner(sent, 'in 5 days')

    def test_a_signed_out_login_tells_the_owner(self):
        # What Claude Code left behind on 2026-11-03 after the sign-in limit passed.
        result, sent = self.run_check({'accessToken': '', 'refreshToken': '', 'expiresAt': 0,
                                       'refreshTokenExpiresAt': 1793677550771})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_told_owner(sent, 'is signed out')

    def test_an_undelivered_warning_fails_the_check(self):
        result, _ = self.run_check({'accessToken': TOKEN, 'refreshToken': '', 'expiresAt': self.in_days(-1)}, send_exit=1)
        self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
