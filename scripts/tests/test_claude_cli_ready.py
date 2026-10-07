"""The container entrypoint: tool-less compaction and /btw start Claude, a turn needs one MCP config."""

import os
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch

READY = Path(__file__).resolve().parents[2] / 'ansible/roles/claude-cli/files/claude-cli-ready.py'


class ReadyTest(unittest.TestCase):
    def run_ready(self, args):
        with patch('sys.argv', ['claude-cli-ready', *args]), \
             patch.object(os, 'execv', side_effect=SystemExit('exec')) as execv:
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path(str(READY), run_name='__main__')
        return str(raised.exception), execv

    def test_compaction_and_btw_start_claude_without_mcp(self):
        for args in (['-p', '--resume', 'fixture', '/compact'], ['-p', '--no-session-persistence', '--tools', '']):
            with self.subTest(args=args):
                message, execv = self.run_ready(args)
                self.assertEqual(message, 'exec')
                execv.assert_called_once_with('/usr/local/bin/claude', ['/usr/local/bin/claude', *args])

    def test_two_mcp_configs_are_refused(self):
        message, execv = self.run_ready(['--mcp-config', '/tmp/a.json', '--mcp-config=/tmp/b.json'])
        self.assertIn('Expected one generated OpenClaw MCP configuration', message)
        execv.assert_not_called()


if __name__ == '__main__':
    unittest.main()
