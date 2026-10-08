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
        # `openclaw doctor --fix` (install.yml) and `openclaw daemon install` start the gateway;
        # without the PATH drop-in it runs the real Claude Code on the host (both seen on the test server).
        roles = ROOT / 'ansible/roles'
        main, config = load_yaml(roles / 'openclaw/tasks/main.yml', roles / 'config/tasks/main.yml')
        included = lambda t: (lambda v: v.get('file') if isinstance(v, dict) else v)(t.get('ansible.builtin.include_tasks'))
        position = lambda name: [i for i, t in enumerate(main) if included(t) == name]
        launcher = position('claude-cli-path.yml')
        self.assertEqual(len(launcher), 2, 'the launcher must be applied before the install and after the unit exists')
        first, second = launcher
        self.assertEqual(main[first]['when'], 'daemon_service.stat.exists')
        self.assertLess(first, position('install.yml')[0], 'doctor can start the gateway before the launcher is on its PATH')
        # After the unit exists, before claude-cli-auth installs the real Claude Code.
        self.assertGreater(second, position('daemon.yml')[0])
        self.assertLess(second, position('claude-cli-auth.yml')[0], 'real Claude Code installed while the gateway lacks the launcher')
        self.assertEqual(main[second + 1].get('ansible.builtin.meta'), 'flush_handlers')
        # Switching containers on or off with `--tags config,claude-cli` must reach it.
        for task in (main[second], main[second + 1]):
            self.assertTrue({'config', 'claude-cli'} <= set(task['tags']))
        self.assertTrue({'config', 'claude-cli'} <= set(main[second]['ansible.builtin.include_tasks']['apply']['tags']))
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

    def test_state_directory_is_private_before_doctor(self):
        # Doctor skips its service policy refresh while ~/.openclaw is group-writable.
        main, = load_yaml(ROOT / 'ansible/roles/openclaw/tasks/main.yml')
        names = [t.get('name') for t in main]
        self.assertIn('Restrict .openclaw directory permissions', names, 'the openclaw role must restrict ~/.openclaw before installing')
        restrict = names.index('Restrict .openclaw directory permissions')
        self.assertEqual(main[restrict]['ansible.builtin.file']['mode'], '0700')
        self.assertLess(restrict, names.index('Install OpenClaw'))

    def test_a_failed_workspace_sync_restores_the_timers_it_stopped(self):
        # One agent's merge conflict once left all nine production workspaces without backup.
        tasks, = load_yaml(ROOT / 'ansible/roles/workspace/tasks/main.yml')
        block = next((t for t in tasks if any(c.get('ansible.builtin.include_tasks') == 'sync.yml'
                                              for c in t.get('block', []))), None)
        self.assertIsNotNone(block, 'the sync loop must run inside a block')
        restore = [t for t in block.get('always', []) if 'ansible.builtin.systemd' in t]
        self.assertEqual(len(restore), 1, 'the timers must come back in always')
        self.assertIn('workspace_existing_units', restore[0]['loop'])
        self.assertEqual(restore[0]['ansible.builtin.systemd']['state'], 'started')
        self.assertTrue(restore[0]['ansible.builtin.systemd']['enabled'])

    def test_video_frames_skill_is_installed_from_clawhub(self):
        # 2026.9 stopped bundling it; main's video workflow reads it.
        tasks, = load_yaml(ROOT / 'ansible/roles/openclaw/tasks/main.yml')
        install = [t for t in tasks if '@steipete/video-frames' in json.dumps(t)]
        self.assertEqual(len(install), 1)
        command = install[0]['ansible.builtin.command']
        self.assertEqual(command['argv'][:3], ['openclaw', 'skills', 'install'])
        self.assertIn('--global', command['argv'])
        self.assertEqual(command['argv'][command['argv'].index('--version') + 1], '1.0.0')
        self.assertEqual(command['creates'], '/home/ubuntu/.openclaw/skills/video-frames/SKILL.md')
        # OpenClaw hides a skill whose required binaries are missing on the host.
        system, = load_yaml(ROOT / 'ansible/roles/system/tasks/main.yml')
        packages = next(t for t in system if t.get('name') == 'Install base packages')['ansible.builtin.apt']['name']
        self.assertIn('ffmpeg', packages)

    def test_provisioning_never_upgrades_tailscale_itself(self):
        # Upgrading Tailscale restarts tailscaled, which carries the provisioning connection.
        tasks, = load_yaml(ROOT / 'ansible/roles/system/tasks/main.yml')
        upgrade = next((t for t in tasks if any(c.get('ansible.builtin.apt', {}).get('upgrade') == 'dist'
                                                for c in t.get('block', []))), None)
        self.assertIsNotNone(upgrade, 'the dist-upgrade must run inside a block that holds Tailscale')
        hold = upgrade['block'][0]['ansible.builtin.dpkg_selections']
        self.assertEqual((hold['name'], hold['selection']), ('tailscale', 'hold'))
        release = upgrade['always'][0]['ansible.builtin.dpkg_selections']
        self.assertEqual((release['name'], release['selection']), ('tailscale', 'install'))
        names = [t.get('name') for t in tasks]
        # Tailscale's own updater keeps it current; its check needs jq from the base packages.
        self.assertGreater(names.index('Let Tailscale update itself'), names.index('Install base packages'))

    def test_the_gateway_restarts_whenever_tailscaled_restarts(self):
        # The gateway's Serve route ends with tailscaled and is not claimed again (seen in production).
        main, = load_yaml(ROOT / 'ansible/roles/openclaw/tasks/main.yml')
        names = [t.get('name') for t in main]
        self.assertIn('Restart the gateway whenever tailscaled restarts', names)
        unit = next(t for t in main if t.get('name') == 'Restart the gateway whenever tailscaled restarts')
        content = unit['ansible.builtin.copy']['content']
        for line in ('PartOf=tailscaled.service', 'RemainAfterExit=yes', 'WantedBy=tailscaled.service',
                     'try-restart openclaw-gateway.service'):
            self.assertIn(line, content)
        enable = next(t for t in main if t.get('name') == 'Enable the gateway restart after tailscaled restarts')
        self.assertTrue(enable['ansible.builtin.systemd']['enabled'])

if __name__ == '__main__':
    unittest.main()
