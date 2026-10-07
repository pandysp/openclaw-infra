"""Ordering rules of the playbook that only a fresh host would reveal."""

import json
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


if __name__ == '__main__':
    unittest.main()
