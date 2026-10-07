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


def load_install():
    from test_playbook_order import load_yaml
    return load_yaml(ROOT / 'ansible/roles/openclaw/tasks/install.yml')


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
                                'Regenerate daemon service file after an OpenClaw install')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(result.reloaded)
        self.assertNotIn('REGENERATED', result.stdout)

    def test_daemon_is_regenerated_only_after_an_install_or_from_an_old_file(self):
        # 2026.9 service files carry no version: comparing one regenerated the service
        # and restarted the gateway on every provisioning run.
        body = shell_body('ansible/roles/openclaw/tasks/daemon.yml', 'Regenerate daemon service file after an OpenClaw install')
        cases = {('false', 'Description=OpenClaw Gateway\n'): False,
                 ('true', 'Description=OpenClaw Gateway\n'): True,
                 ('false', 'Environment=OPENCLAW_SERVICE_VERSION=2026.6.6\n'): True}
        for (installed, unit), regenerates in cases.items():
            with self.subTest(installed=installed, unit=unit), tempfile.TemporaryDirectory(prefix='daemon-') as temporary:
                root = Path(temporary)
                for tool in ('openclaw', 'systemctl'):
                    (root / tool).write_text('#!/bin/sh\necho "$0 $*" >> "$HOME/calls"\n')
                    (root / tool).chmod(0o700)
                rendered = (re.sub(r"\{\{.*?\}\}", installed, body)
                            .replace('/home/ubuntu/.config/systemd/user/openclaw-gateway.service', str(root / 'unit')))
                (root / 'unit').write_text(unit)
                result = subprocess.run(['bash', '-c', rendered], capture_output=True, text=True, timeout=15,
                                        env={'HOME': str(root), 'PATH': str(root) + ':' + os.environ['PATH']})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual('REGENERATED' in result.stdout, regenerates)
                self.assertEqual((root / 'calls').exists(), regenerates)
        install, = load_install()
        npm = [t for block in install for t in block.get('block', [block])
               if 'npm install -g "openclaw@' in str(t.get('ansible.builtin.command', ''))]
        self.assertEqual([t.get('register') for t in npm], ['openclaw_package_install'])

    def test_ubuntu_becomes_tailscale_operator_once(self):
        # Without operator rights the gateway's `tailscale serve` runs as root via sudo and
        # can outlive a stop; the next start then exits 78 for good.
        daemon = (ROOT / 'ansible/roles/openclaw/tasks/daemon.yml').read_text()
        self.assertIn('- name: Let the gateway run Tailscale Serve without sudo', daemon)
        # Before the first task that can start the gateway.
        self.assertLess(daemon.index('Let the gateway run Tailscale Serve'), daemon.index('daemon install'))
        body = shell_body('ansible/roles/openclaw/tasks/daemon.yml', 'Let the gateway run Tailscale Serve without sudo')
        for current, sets in (('', True), ('root', True), ('ubuntu', False)):
            with self.subTest(operator=current), tempfile.TemporaryDirectory(prefix='operator-') as temporary:
                root = Path(temporary)
                (root / 'tailscale').write_text(
                    '#!/bin/sh\nif [ "$1" = debug ]; then printf \'{"OperatorUser":"%s"}\\n\' "$OPERATOR"; '
                    'else echo "$*" >> "$HOME/sets"; fi\n')
                (root / 'tailscale').chmod(0o700)
                result = subprocess.run(['bash', '-c', body], capture_output=True, text=True, timeout=15,
                                        env={'HOME': str(root), 'OPERATOR': current,
                                             'PATH': str(root) + ':' + os.environ['PATH']})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual('CHANGED' in result.stdout, sets)
                if sets:
                    self.assertEqual((root / 'sets').read_text(), 'set --operator=ubuntu\n')
                else:
                    self.assertFalse((root / 'sets').exists())

    def test_failed_onboarding_shows_no_secret(self):
        # The output reaches the public Phoenix log; onboarding generates its own gateway token.
        body = shell_body('ansible/roles/openclaw/tasks/onboard.yml', 'Run OpenClaw onboarding')
        with tempfile.TemporaryDirectory(prefix='onboard-') as temporary:
            root = Path(temporary)
            (root / '.openclaw').mkdir()
            (root / 'setup-token').write_text('fixture-setup-NOT-SK-SHAPED')
            (root / 'openclaw').write_text(
                '#!/bin/sh\nprintf \'{"gateway":{"auth":{"token":"fixture-generated-gw"}}}\' > "$HOME/.openclaw/openclaw.json"\n'
                'echo "auth with fixture-setup-NOT-SK-SHAPED, gateway token fixture-generated-gw"\nexit 2\n')
            (root / 'openclaw').chmod(0o700)
            result = subprocess.run(['bash', '-c', body.replace('/tmp/ansible-setup-token', str(root / 'setup-token'))],
                                    capture_output=True, text=True, timeout=15,
                                    env={'HOME': str(root), 'PATH': str(root) + ':' + os.environ['PATH']})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('[setup token]', result.stdout)
        self.assertIn('[gateway token]', result.stdout)
        self.assertNotIn('fixture-', result.stdout + result.stderr)

    def test_agent_query_errors_withhold_raw_credentials(self):
        for task in ['Get existing agents', 'Refresh agent list for config targeting']:
            with self.subTest(task=task):
                result = self.run_shell('ansible/roles/agents/tasks/main.yml', task)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('fixture-secret', result.stdout + result.stderr)
                self.assertIn('23', result.stderr)


if __name__ == '__main__':
    unittest.main()
