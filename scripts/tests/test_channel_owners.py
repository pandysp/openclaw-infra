"""Which channel accounts the default agent owns, run through the real tasks."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from test_playbook_order import derived_vars, load_yaml

ROOT = Path(__file__).resolve().parents[2]
BINDINGS = ROOT / 'ansible/roles/telegram/tasks/bindings.yml'
OWNERS = "Give the default agent each configured channel account"


class ChannelOwnerTest(unittest.TestCase):
    def owned(self, agents=None, **variables):
        tasks, = load_yaml(BINDINGS)
        selected = [t for t in tasks if t.get('name') in ('Initialize bindings list', OWNERS)]
        self.assertEqual(len(selected), 2, 'bindings tasks moved')
        agents = agents or [{'id': 'main', 'is_default': True}, {'id': 'other', 'is_default': False}]
        play = [{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                 'vars': {'openclaw_agents': agents, 'openclaw_whatsapp_used': False, **derived_vars(), **variables},
                 'tasks': selected + [{'ansible.builtin.copy': {'content': '{{ _telegram_bindings | to_json }}',
                                                                 'dest': '{{ owners_file }}'}}]}]
        with tempfile.TemporaryDirectory() as tmp:
            play[0]['vars']['owners_file'] = str(Path(tmp) / 'owners.json')
            (Path(tmp) / 'play.json').write_text(json.dumps(play))
            result = subprocess.run([shutil.which('ansible-playbook'), '-i', 'localhost,', str(Path(tmp) / 'play.json')],
                                    capture_output=True, text=True, timeout=120,
                                    env={**os.environ, 'ANSIBLE_NOCOLOR': '1', 'ANSIBLE_LOCAL_TEMP': tmp})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            bindings = json.loads((Path(tmp) / 'owners.json').read_text())
        self.assertLessEqual({b['agentId'] for b in bindings}, {'main'})
        return [b['match']['channel'] for b in bindings]

    def test_a_configured_discord_is_owned_by_the_default_agent(self):
        # Since 2026.8.1 a channel account without an owner binding stays blocked.
        self.assertEqual(self.owned(discord_bot_token='fixture'), ['telegram', 'discord'])

    def test_unconfigured_channels_get_no_owner(self):
        self.assertEqual(self.owned(), ['telegram'])
        self.assertEqual(self.owned(openclaw_whatsapp_used=True, discord_bot_token=''), ['telegram', 'whatsapp'])
        self.assertEqual(self.owned(discord_bot_token='fixture', openclaw_discord_enabled=False), ['telegram'])

    def test_without_a_default_agent_no_account_gets_an_owner(self):
        # 2026-11-24: main was removed and tl must not inherit Andy's default accounts;
        # unmatched chats fall to openclaw_system_agent instead.
        agents = [{'id': 'tl', 'is_default': False}, {'id': 'other', 'is_default': False}]
        self.assertEqual(self.owned(agents, openclaw_whatsapp_used=True, discord_bot_token='fixture'), [])

    def test_adding_a_channel_with_its_own_tag_reaches_the_owners(self):
        main, = load_yaml(ROOT / 'ansible/roles/telegram/tasks/main.yml')
        include = next(t for t in main if 'bindings.yml' in json.dumps(t))
        for tag in ('whatsapp', 'discord'):
            self.assertIn(tag, include.get('tags', []))
            self.assertIn(tag, include['ansible.builtin.include_tasks']['apply']['tags'])

if __name__ == '__main__':
    unittest.main()
