#!/usr/bin/env python3
"""Run the agents role's real removal tasks through Ansible with fake OpenClaw and systemctl.

Deleting an agent moves its workspace to Trash. A running Obsidian daemon would
sync every vanished file into the cloud vault, so the sync units must be stopped
first, and the system agent set before the delete clears it (2026-11-24, `main`).
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from test_playbook_order import load_yaml

ROOT = Path(__file__).resolve().parents[2]
SELECTED = ('Get existing agents', 'Find retired agents',
            'Refuse to delete agents not named in openclaw_retired_agents',
            'Stop the sync services of retired agents', 'Set the system agent', 'Delete retired agents',
            'Show retired agent and system agent changes')
FAKE_OPENCLAW = '''#!/bin/sh
case "$*" in
    "agents list --json") printf '%s\\n' "$FIXTURE_AGENTS_LIST" ;;
    "config get agents.defaults.systemAgent.agentId")
        if [ -s "$FIXTURE_ROOT/system-agent" ]; then cat "$FIXTURE_ROOT/system-agent"
        else echo "Config path is valid but unset: agents.defaults.systemAgent.agentId"; exit 1; fi ;;
    "config set agents.defaults.systemAgent.agentId "*)
        echo "openclaw $*" >> "$FIXTURE_ROOT/calls"; printf '%s\\n' "$5" > "$FIXTURE_ROOT/system-agent" ;;
    "agents delete "*)
        echo "openclaw $*" >> "$FIXTURE_ROOT/calls"
        case "$FIXTURE_DELETE" in
            refuse) echo '{"ok":false,"error":"Agent owns the shared auth store"}'; exit 1 ;;
            purge-failed) echo '{"agentId":"main","removed":[],"failed":[],"purgeFailed":true}' ;;
            *) echo '{"agentId":"main","removed":["/home/ubuntu/.openclaw/workspace"],"failed":[]}' ;;
        esac ;;
    *) echo "unexpected openclaw call: $*" >&2; exit 97 ;;
esac
'''
# Units are loaded, enabled and running until stopped; FIXTURE_STUCK never stops.
FAKE_SYSTEMCTL = '''#!/bin/sh
[ "$1" = --user ] || { echo "expected --user" >&2; exit 97; }
shift
STATE="$FIXTURE_ROOT/units"
mkdir -p "$STATE"
case "$1" in
    show) echo loaded ;;
    is-active) if [ -e "$STATE/$2.stopped" ]; then echo inactive; exit 3; fi; echo active ;;
    is-enabled) if [ -e "$STATE/$2.disabled" ]; then echo disabled; exit 1; fi; echo enabled ;;
    stop) echo "systemctl stop $2" >> "$FIXTURE_ROOT/calls"
          [ "$2" = "$FIXTURE_STUCK" ] || touch "$STATE/$2.stopped" ;;
    disable) echo "systemctl disable $2" >> "$FIXTURE_ROOT/calls"; touch "$STATE/$2.disabled" ;;
    *) echo "unexpected systemctl call: $*" >&2; exit 97 ;;
esac
'''


class AgentRemovalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ansible = shutil.which('ansible-playbook')
        if not cls.ansible:
            raise RuntimeError("Install the project's Ansible development dependency first")
        tasks, = load_yaml(ROOT / 'ansible/roles/agents/tasks/main.yml')
        cls.tasks = [task for task in tasks if task.get('name') in SELECTED]
        # Kept in file order: the order under test is the role's own.
        assert sorted(t['name'] for t in cls.tasks) == sorted(SELECTED), 'The removal tasks were renamed'

    def run_role(self, live=('main', 'tl'), retired=('main',), delete='ok', stuck='', check=False):
        with tempfile.TemporaryDirectory(prefix='agent-removal-') as tmp:
            root = Path(tmp)
            (root / 'bin').mkdir()
            for name, body in (('openclaw', FAKE_OPENCLAW), ('systemctl', FAKE_SYSTEMCTL)):
                (root / 'bin' / name).write_text(body)
                (root / 'bin' / name).chmod(0o700)
            (root / 'system-agent').write_text('main\n')
            play = [{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                     'vars': {'openclaw_agents': [{'id': 'tl', 'is_default': False}],
                              'openclaw_retired_agents': list(retired), 'openclaw_system_agent': 'tl'},
                     'tasks': self.tasks}]
            (root / 'play.json').write_text(json.dumps(play))
            env = {'PATH': f"{root / 'bin'}:{os.environ['PATH']}", 'HOME': tmp, 'FIXTURE_ROOT': tmp,
                   'FIXTURE_AGENTS_LIST': json.dumps([{'id': agent} for agent in live]),
                   'FIXTURE_DELETE': delete, 'FIXTURE_STUCK': stuck,
                   'ANSIBLE_LOCAL_TEMP': str(root / 'local'), 'ANSIBLE_REMOTE_TEMP': str(root / 'remote'),
                   'ANSIBLE_NOCOLOR': '1'}
            result = subprocess.run([self.ansible, '-i', 'localhost,', str(root / 'play.json')]
                                    + (['--check'] if check else []),
                                    env=env, capture_output=True, text=True, timeout=120)
            calls = (root / 'calls').read_text().splitlines() if (root / 'calls').exists() else []
            return result, calls

    def test_units_stop_then_owner_moves_then_agent_is_deleted(self):
        result, calls = self.run_role()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        stops = [i for i, call in enumerate(calls) if call.startswith('systemctl stop ')]
        owner = calls.index('openclaw config set agents.defaults.systemAgent.agentId tl')
        delete = calls.index('openclaw agents delete main --force --json')
        self.assertEqual({calls[i] for i in stops}, {
            'systemctl stop obsidian-headless-main.service', 'systemctl stop workspace-git-sync-main.timer',
            'systemctl stop workspace-git-sync-main.service'})
        self.assertLess(max(stops), owner)
        self.assertLess(owner, delete)
        self.assertIn('systemctl disable obsidian-headless-main.service', calls[:owner])

    def test_a_refused_delete_fails_the_run(self):
        result, calls = self.run_role(delete='refuse')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Delete retired agents', result.stdout)
        self.assertIn('openclaw agents delete main --force --json', calls)

    def test_a_delete_that_keeps_session_data_fails_the_run(self):
        result, _ = self.run_role(delete='purge-failed')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('left files or session data behind', result.stdout + result.stderr)

    def test_a_unit_that_keeps_running_stops_the_delete(self):
        result, calls = self.run_role(stuck='obsidian-headless-main.service')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse([call for call in calls if call.startswith('openclaw ')], calls)

    def test_an_agent_not_named_for_removal_stops_the_run(self):
        # Without the personal openclaw.yml, all.yml's single main would retire everyone else.
        result, calls = self.run_role(live=('main', 'tl', 'manon'), retired=('main',))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('manon', result.stdout)
        self.assertEqual(calls, [])

    def test_check_mode_reports_and_changes_nothing(self):
        result, calls = self.run_role(check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(calls, [])
        for line in ('would stop and disable obsidian-headless-main.service',
                     'system agent main \u2192 tl', 'would delete agent main'):
            self.assertIn(line, result.stdout)

    def test_nothing_to_remove_changes_nothing(self):
        result, calls = self.run_role(live=('tl',))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(calls, ['openclaw config set agents.defaults.systemAgent.agentId tl'])


if __name__ == '__main__':
    unittest.main()
