#!/usr/bin/env python3
"""Run verify.sh's actual audit gate with SSH as the external boundary."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'verify.sh'


class SecurityAuditGateTests(unittest.TestCase):
    def run_gate(self, payload, ssh_status=0, section='audit', socket_error=None):
        source = SCRIPT.read_text()
        functions = source[source.index('check_pass() {'):source.index('# 1. Check Tailscale')]
        markers = {'audit': ('# 10. OpenClaw security audit', '# 11. Channel status'),
                   'health': ('# 9. OpenClaw health check', '# 10. OpenClaw security audit'),
                   'https': ('# 6. Check gateway health endpoint', '# 7. Check gateway port'),
                   'ports': ('# 8. Security audit', '# 9. OpenClaw health check'),
                   'channels': ('# 11. Channel status', '# 12. Check scheduled automation')}
        start, end = markers[section]
        audit = source[source.index(start):source.index(end)]
        final = source[source.index('if [[ "$FAILURES" -gt 0 ]]'):]
        with tempfile.TemporaryDirectory(prefix='audit-gate-') as tmp:
            root = Path(tmp)
            ssh = root / 'tailscale'
            ssh.write_text('#!/bin/sh\ntest "$1" = ssh || exit 90\ncase "$*" in\n  *"timeout 180 openclaw security audit --deep --json"|*"timeout 60 openclaw health --json"|*"ifconfig.me"|*"timeout 60 openclaw channels status --json"|*"timeout 60 openclaw channels status 2>&1") ;;\n  *) echo "Unexpected audit invocation" >&2; exit 90 ;;\nesac\necho "Fixture transport version warning" >&2\nprintf "%s\\n" "$AUDIT_PAYLOAD"\nexit "$AUDIT_STATUS"\n')
            ssh.chmod(0o700)
            curl = root / 'curl'
            curl.write_text('#!/bin/sh\nprintf "%s" "$AUDIT_PAYLOAD"\nexit "$AUDIT_STATUS"\n')
            curl.chmod(0o700)
            if socket_error is not None:
                (root / 'sitecustomize.py').write_text('import socket\n'
                    'class Connection:\n'
                    ' def __enter__(self): return self\n'
                    ' def __exit__(self,*args): pass\n'
                    ' def settimeout(self,value): pass\n'
                    ' def connect(self,address): raise ' + socket_error + '()\n'
                    'socket.socket=lambda *args: Connection()\n')
            env = {'PATH': str(root) + ':' + os.environ['PATH'], 'HOME': tmp,
                   'PYTHONPATH': tmp,
                   'AUDIT_PAYLOAD': payload if isinstance(payload, str) else json.dumps(payload),
                   'AUDIT_STATUS': str(ssh_status), 'REQUIRED_CHANNELS': 'telegram'}
            return subprocess.run(['bash', '-euo', 'pipefail'],
                                  input='RED= GREEN= YELLOW= NC=\nFAILURES=0\nFULL_HOSTNAME=fixture.invalid\n' + functions + audit + final,
                                  env=env, text=True, capture_output=True, timeout=10)

    def test_connectivity_uses_one_validated_peer_lookup(self):
        source = SCRIPT.read_text().split('# 2. Check SSH access')[0]
        peer = {'HostName': 'fixture', 'Online': True, 'DNSName': 'fixture.example.ts.net.'}
        with tempfile.TemporaryDirectory(prefix='verify-peer-') as tmp:
            root = Path(tmp)
            calls = root / 'calls'
            tailscale = root / 'tailscale'
            tailscale.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n'
                                 'test "$*" = "status --json" || exit 90\n'
                                 'printf "%s\\n" "$PEER_PAYLOAD"\n')
            tailscale.chmod(0o700)
            env = {'PATH': str(root) + ':' + os.environ['PATH'], 'HOME': tmp,
                   'OPENCLAW_HOSTNAME': 'fixture', 'CALLS': str(calls),
                   'PEER_PAYLOAD': json.dumps({'MagicDNSSuffix': 'example.ts.net', 'Peer': {'a': peer}})}
            result = subprocess.run(['bash'], input=source, env=env, text=True,
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(calls.read_text().splitlines(), ['status --json'])
            self.assertIn('Tailscale can reach fixture', result.stdout)

    def test_only_structured_openclaw_status_remains(self):
        source = SCRIPT.read_text()
        self.assertEqual(source.count('openclaw status'), 1)
        self.assertIn('openclaw status --json', source)
        self.assertNotIn('OPENCLAW_STATUS', source)

    def test_public_ip_failure_cannot_skip_the_port_scan_successfully(self):
        for payload, status in (('', 0), ('not-an-ip', 0), ('203.0.113.42', 1)):
            with self.subTest(payload=payload, status=status):
                self.assertNotEqual(self.run_gate(payload, status, 'ports').returncode, 0)

    def test_unexpected_socket_errors_are_not_closed_ports(self):
        for error, success in (('TimeoutError', True), ('ConnectionRefusedError', True),
                               ('PermissionError', False), ('OSError', False)):
            with self.subTest(error=error):
                result = self.run_gate('203.0.113.42', section='ports', socket_error=error)
                self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)

    def test_required_channels_need_completed_positive_evidence(self):
        account = {'enabled': True, 'configured': True, 'running': True,
                   'connected': True, 'lastError': None}
        good = {'channelAccounts': {'telegram': [account]}}
        self.assertEqual(self.run_gate(good, section='channels').returncode, 0)
        failures = [(good, 1), ({'channelAccounts': {'telegram': [
                        {k: v for k, v in account.items() if k != 'connected'}]}}, 0),
                    ({'channelAccounts': {}}, 0),
                    ({'channelAccounts': {'telegram': []}}, 0), ('not JSON', 0)]
        for key in ('enabled', 'configured', 'running', 'connected'):
            failures.append(({'channelAccounts': {'telegram': [account | {key: False}]}}, 0))
        failures.append(({'channelAccounts': {'telegram': [account | {'lastError': 'fixture-private-error'}]}}, 0))
        for payload, status in failures:
            with self.subTest(payload=payload, status=status):
                result = self.run_gate(payload, status, 'channels')
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('fixture-private', result.stdout + result.stderr)

    def test_success_requires_a_completed_zero_critical_audit(self):
        result = self.run_gate({'summary': {'critical': 0, 'warn': 3, 'info': 2}})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Security audit passed', result.stdout)

    def test_every_positive_critical_count_is_fatal(self):
        for count in (1, 10, 20, 2**64):
            with self.subTest(count=count):
                result = self.run_gate({'summary': {'critical': count}})
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('Security audit passed', result.stdout)
                self.assertIn('verification check(s) failed', result.stdout)

    def test_a_failed_command_cannot_pass_using_partial_zero_output(self):
        for status in (1, 124, 255):
            with self.subTest(status=status):
                result = self.run_gate({'summary': {'critical': 0}}, status)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('Security audit passed', result.stdout)

    def test_health_requires_positive_json_and_successful_command(self):
        self.assertEqual(self.run_gate({'ok': True}, section='health').returncode, 0)
        for payload, status in (({'ok': False}, 0), ({}, 0), ('invalid', 0),
                                ({'ok': True}, 1), ({'ok': True}, 124), ({'ok': True}, 255)):
            with self.subTest(payload=payload, status=status):
                result = self.run_gate(payload, status, 'health')
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('OpenClaw health OK', result.stdout)

    def test_https_requires_http_and_command_success(self):
        self.assertEqual(self.run_gate('200', section='https').returncode, 0)
        for payload, status in (('401', 0), ('500', 0), ('200', 28), ('200', 56), ('301', 0), ('', 0)):
            with self.subTest(payload=payload, status=status):
                result = self.run_gate(payload, status, 'https')
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('Gateway responding', result.stdout)

    def test_multiple_json_results_fail_closed(self):
        for section, payload in (
            ('health', '{"ok": false}\n{"ok": true}'),
            ('health', '{"ok": true}\n{"ok": true}'),
            ('audit', '{"summary": {}}\n{"summary": {"critical": 0}}'),
        ):
            with self.subTest(section=section, payload=payload):
                self.assertNotEqual(self.run_gate(payload, section=section).returncode, 0)

    def test_missing_malformed_or_invalid_counts_fail_closed(self):
        for payload in ({}, {'summary': {}}, {'summary': {'critical': '0'}},
                        {'summary': {'critical': False}}, {'summary': {'critical': -1}},
                        {'summary': {'critical': 0.5}}, 'not json', ''):
            with self.subTest(payload=payload):
                result = self.run_gate(payload)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('Security audit passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
