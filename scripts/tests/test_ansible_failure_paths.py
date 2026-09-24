#!/usr/bin/env python3
"""Execute role shell bodies with failures at the OpenClaw CLI boundary."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]


def shell_body(file, task):
    source = (ROOT / file).read_text().split('- name: ' + task, 1)[1]
    return textwrap.dedent(re.search(r'ansible\.builtin\.shell: \|\n(.*?)\n\s+args:', source, re.S).group(1))


class AnsibleFailureTests(unittest.TestCase):
    def run_shell(self, file, task):
        with tempfile.TemporaryDirectory(prefix='role-failure-') as temporary:
            root = Path(temporary)
            cli = root / 'openclaw'
            cli.write_text('#!/bin/sh\nprintf \'{"token":"fixture-secret"}\\n\' >&2\nexit 23\n')
            cli.chmod(0o700)
            systemctl = root / 'systemctl'
            systemctl.write_text('#!/bin/sh\ntouch "$HOME/reloaded"\n')
            systemctl.chmod(0o700)
            result = subprocess.run(['bash', '-c', shell_body(file, task)],
                env={'HOME': str(root), 'PATH': str(root) + ':' + os.environ['PATH']},
                capture_output=True, text=True, timeout=15)
            result.reloaded = (root / 'reloaded').exists()
            return result

    def test_failed_daemon_install_never_claims_regeneration(self):
        result = self.run_shell('ansible/roles/openclaw/tasks/daemon.yml',
                                'Regenerate daemon service file if version drifted')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(result.reloaded)
        self.assertNotIn('REGENERATED', result.stdout)

    def test_agent_query_errors_withhold_raw_credentials(self):
        for task in ['Get existing agents', 'Refresh agent list for config targeting']:
            with self.subTest(task=task):
                result = self.run_shell('ansible/roles/agents/tasks/main.yml', task)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('fixture-secret', result.stdout + result.stderr)
                self.assertIn('23', result.stderr)


if __name__ == '__main__':
    unittest.main()
