"""Ordering rules of the playbook that only a fresh host would reveal."""

import json
import os
import re
import tempfile
from pathlib import Path
import shlex
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK = ROOT / 'ansible/playbook.yml'
TELEGRAM_TASKS = ROOT / 'ansible/roles/telegram/tasks/main.yml'


def load_yaml(*paths):
    ansible = shutil.which('ansible-playbook')
    if not ansible:
        raise RuntimeError("Install the project's Ansible development dependency first")
    python = shlex.split(Path(ansible).read_text().splitlines()[0].removeprefix('#!'))
    code = 'import json,sys,yaml; print(json.dumps([yaml.safe_load(open(p)) for p in sys.argv[1:]]))'
    return json.loads(subprocess.run([*python, '-c', code, *map(str, paths)],
                                     capture_output=True, text=True, check=True).stdout)


class PlaybookOrderTest(unittest.TestCase):
    def test_cron_jobs_are_reconciled_after_every_channel_exists(self):
        # Since 2026.9.8 a job's delivery channel must be loaded when the job is added.
        # A fresh host installs WhatsApp and Discord after the telegram role, and the
        # Discord token comes back only with the sensitive-key injection.
        play, telegram = load_yaml(PLAYBOOK, TELEGRAM_TASKS)
        provision = next(p for p in play if p.get('roles'))
        self.assertFalse([t for t in telegram if 'cron.yml' in json.dumps(t)],
                         'the telegram role must not reconcile cron jobs itself')
        names = [t.get('name') for t in provision['post_tasks']]
        cron = next(t for t in provision['post_tasks']
                    if (t.get('ansible.builtin.include_role') or {}).get('tasks_from') == 'cron.yml')
        self.assertGreater(names.index(cron['name']), names.index('Preserve sensitive nested config keys'))
        self.assertGreater(names.index(cron['name']), names.index('Apply pending gateway restarts before verification'))
        # `--tags telegram` updates schedules, and Phoenix scopes its idempotence run with it.
        self.assertIn('telegram', cron['tags'])
        self.assertIn('telegram', cron['ansible.builtin.include_role']['apply']['tags'])


    def test_a_new_openclaw_never_starts_without_the_container_launcher(self):
        # `openclaw daemon install` starts the gateway; without the PATH drop-in it runs the
        # real Claude Code on the host (reproduced on the test server).
        roles = ROOT / 'ansible/roles'
        daemon, main, config = load_yaml(roles / 'openclaw/tasks/daemon.yml', roles / 'openclaw/tasks/main.yml',
                                         roles / 'config/tasks/main.yml')
        includes = lambda tasks: [i for i, t in enumerate(tasks) if t.get('ansible.builtin.include_tasks') == 'claude-cli-path.yml']
        installs = [i for i, t in enumerate(daemon) if 'daemon install' in json.dumps(t)]
        self.assertEqual(len(includes(daemon)), 1, 'daemon.yml must give the existing unit its launcher first')
        first, = includes(daemon)
        self.assertEqual(daemon[first]['when'], 'daemon_service.stat.exists')
        self.assertLess(first, min(installs), 'the service is (re)installed before the launcher is on its PATH')
        auth = next(i for i, t in enumerate(main) if t.get('ansible.builtin.include_tasks') == 'claude-cli-auth.yml')
        self.assertEqual(len(includes(main)), 1, 'the openclaw role must apply the launcher after the unit exists')
        second, = includes(main)
        self.assertGreater(second, auth)
        self.assertEqual(main[second + 1].get('ansible.builtin.meta'), 'flush_handlers')
        self.assertNotIn('claude-cli-path', json.dumps(config), 'a second writer of the PATH drop-in')

    def test_the_gateway_claude_fails_closed_while_the_launcher_is_missing(self):
        # A dangling symlink is skipped by PATH lookup, which then runs the real Claude Code.
        tasks, = load_yaml(ROOT / 'ansible/roles/openclaw/tasks/claude-cli-path.yml')
        entry = next(t for block in tasks for t in block.get('block', [])
                     if t.get('name') == "Make the gateway's claude the container launcher")
        self.assertIn('ansible.builtin.copy', entry, 'the gateway claude must be a file, not a link')
        with tempfile.TemporaryDirectory() as tmp:
            first, real = Path(tmp, 'launcher-bin'), Path(tmp, 'real-bin')
            first.mkdir(); real.mkdir()
            script = re.sub(r'\{\{.*?\}\}', str(Path(tmp, 'missing-launcher')), entry['ansible.builtin.copy']['content'])
            (first / 'claude').write_text(script); (first / 'claude').chmod(0o755)
            (real / 'claude').write_text(f'#!/bin/sh\ntouch {tmp}/real-claude-ran\n'); (real / 'claude').chmod(0o755)
            result = subprocess.run(['claude', '--version'], capture_output=True, text=True,
                                    env={'PATH': f'{first}:{real}:/usr/bin:/bin'})
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertFalse(Path(tmp, 'real-claude-ran').exists(), 'fell through to the real Claude Code')

if __name__ == '__main__':
    unittest.main()
