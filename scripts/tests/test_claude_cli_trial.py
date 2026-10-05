"""Run the live trial's helpers and its in-container probe with real files; Git is the boundary."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'trials/claude-cli/restricted-gateway-trial.py'
trial = {}
# Load only the helpers, before the trial reads the deployment or sends gateway turns.
exec(compile(SOURCE.read_text().split('\nhome = Path.home()\n', 1)[0], str(SOURCE), 'exec'), trial)
probe = importlib.util.module_from_spec(importlib.util.spec_from_file_location(
    'container_probe', SOURCE.with_name('container-probe.py')))
probe.__spec__.loader.exec_module(probe)


class MacAbsenceTest(unittest.TestCase):
    """The in-container probe reads real files; no Docker boundary is mocked."""

    def check(self, key='', pin='', config='Host github.com\n', hosts='127.0.0.1 localhost\n', mac_host='mac-air'):
        with tempfile.TemporaryDirectory(prefix='trial-mac-') as directory:
            home = Path(directory)
            ssh = home / '.ssh'
            ssh.mkdir()
            (ssh / 'id_ed25519_openclaw_mac_air').write_text(key)
            (ssh / 'known_hosts_openclaw_mac_air').write_text(pin)
            if config is not None:
                (ssh / 'config').write_text(config)
            (home / 'hosts').write_text(hosts)
            return probe.mac_absence(home, home / 'hosts', mac_host)

    def test_empty_mountpoint_stubs_and_unrelated_host_are_allowed(self):
        self.assertEqual(self.check(hosts='127.0.0.1 localhost\n192.0.2.1 github-fixture\n'), {
            'no_mac_key_or_pin': True, 'no_mac_ssh_block': True, 'no_mac_host_entry': True})

    def test_key_and_pin_content_each_fail(self):
        for value in ({'key': 'fixture-key'}, {'pin': 'fixture-pin'}):
            with self.subTest(value=value):
                self.assertFalse(self.check(**value)['no_mac_key_or_pin'])

    def test_mac_ssh_configuration_fails(self):
        self.assertFalse(self.check(config='Host mac-air\n IdentityFile ~/.ssh/id_ed25519_openclaw_mac_air\n')['no_mac_ssh_block'])

    def test_missing_ssh_configuration_is_not_proof_of_denial(self):
        self.assertFalse(self.check(config=None)['no_mac_ssh_block'])

    def test_mac_host_entry_fails(self):
        self.assertFalse(self.check(hosts='127.0.0.1 localhost\n100.64.0.7\tmac-air\n')['no_mac_host_entry'])

    def test_commented_host_entry_is_not_an_entry(self):
        self.assertTrue(self.check(hosts='# 100.64.0.7 mac-air\n')['no_mac_host_entry'])

    def test_unknown_host_does_not_claim_host_entry_was_checked(self):
        self.assertNotIn('no_mac_host_entry', self.check(mac_host=''))


class MacExpectationTest(unittest.TestCase):
    def test_main_only_manifest_matches_explicit_expectation(self):
        trial['validate_mac_access']({'ssh': {'main': {'mac_host': 'mac-air'}, 'other': {'mac_host': ''}}}, ['main'])

    def test_no_mac_manifest_matches_no_mac_expectation(self):
        trial['validate_mac_access']({'ssh': {'main': {'mac_host': ''}}}, [])

    def test_all_disabled_manifest_cannot_certify_main_only_access(self):
        with self.assertRaisesRegex(SystemExit, 'does not match'):
            trial['validate_mac_access']({'ssh': {'main': {'mac_host': ''}}}, ['main'])

    def test_unexpected_grant_fails(self):
        with self.assertRaisesRegex(SystemExit, 'does not match'):
            trial['validate_mac_access']({'ssh': {'main': {'mac_host': 'mac-air'}, 'other': {'mac_host': 'mac-air'}}}, ['main'])

    def test_old_manifest_missing_mac_host_fails_loudly(self):
        with self.assertRaisesRegex(KeyError, 'mac_host'):
            trial['validate_mac_access']({'ssh': {'main': {}}}, ['main'])


class TrialCleanupTest(unittest.TestCase):
    def test_session_delete_failure_does_not_skip_containers_files_other_agents_or_config(self):
        with tempfile.TemporaryDirectory(prefix='trial-cleanup-') as directory:
            root = Path(directory)
            config = root / 'config.json'
            config.write_text('{"changed": true}')
            active, transcripts, paths, calls = [], {}, [], []
            for run in ('first', 'second'):
                proof, diagnostic = root / (run + '.txt'), root / (run + '.py')
                files = [proof, diagnostic, diagnostic.with_suffix('.mp4'), diagnostic.with_suffix('.png')]
                transcript = root / (run + '.jsonl')
                transcript.write_text('Operator C restricted runtime trial ' + run)
                transcripts[run] = transcript
                for path in files:
                    path.write_text('fixture')
                paths.extend([*files, transcript])
                active.append(('agent:main:' + run, run, root, proof, diagnostic, run))

            def gateway(method, params, **kwargs):
                calls.append(('session', params['key']))
                raise RuntimeError('fixture session deletion failure')

            def docker(args):
                calls.append(tuple(args[:3]))
                return 'fixture-container\n' if args[:2] == ['docker', 'ps'] else ''

            with patch.dict(trial, {'gateway': gateway, 'command': docker}):
                with self.assertRaises(ExceptionGroup) as caught:
                    trial['cleanup_trial'](active, transcripts, config, {})
            self.assertEqual(len(caught.exception.exceptions), 3)
            self.assertTrue(any('Unrelated config changed' in str(error) for error in caught.exception.exceptions))
            self.assertEqual(sum(call[0] == 'session' for call in calls), 2)
            self.assertEqual(sum(call[:2] == ('docker', 'rm') for call in calls), 2)
            self.assertFalse(any(path.exists() for path in paths))

    def test_unexpected_transcript_is_preserved_and_error_is_reported(self):
        with tempfile.TemporaryDirectory(prefix='trial-cleanup-') as directory:
            root = Path(directory)
            config = root / 'config.json'
            config.write_text('{}')
            transcript = root / 'unexpected.jsonl'
            transcript.write_text('not this trial')
            active = [('agent:main:fixture', 'fixture', root, root / 'proof.txt', root / 'diagnostic.py', 'fixture')]
            with patch.dict(trial, {'gateway': lambda *args, **kwargs: {}, 'command': lambda args: ''}):
                with self.assertRaises(ExceptionGroup) as caught:
                    trial['cleanup_trial'](active, {'fixture': transcript}, config, {})
            self.assertTrue(transcript.exists())
            self.assertIn('Refusing to delete', str(caught.exception.exceptions[0]))

    def test_failed_first_turn_cleans_only_transcript_matching_its_marker(self):
        with tempfile.TemporaryDirectory(prefix='trial-cleanup-') as directory:
            root = Path(directory)
            config = root / 'config.json'
            config.write_text('{}')
            transcript, unrelated = root / 'trial.jsonl', root / 'other.jsonl'
            transcript.write_text('Operator C restricted runtime trial fixture')
            unrelated.write_text('unrelated session')
            active = [('agent:main:fixture', 'fixture', root, root / 'proof.txt', root / 'diagnostic.py', 'fixture')]
            with patch.dict(trial, {'gateway': lambda *args, **kwargs: {}, 'command': lambda args: ''}):
                trial['cleanup_trial'](active, {}, config, {})
            self.assertFalse(transcript.exists())
            self.assertTrue(unrelated.exists())

    def test_successful_cleanup_returns_without_error(self):
        with tempfile.TemporaryDirectory(prefix='trial-cleanup-') as directory:
            config = Path(directory) / 'config.json'
            config.write_text('{}')
            trial['cleanup_trial']([], {}, config, {})


class GitProbeTest(unittest.TestCase):
    READS = [subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0, stdout='abc\tHEAD\n')]

    def git(self, *results):
        with patch.object(probe.subprocess, 'run', side_effect=[*self.READS, *results]) as run:
            return probe.git_checks('', 'fixture-branch'), run

    def test_push_timeout_still_attempts_branch_deletion(self):
        with patch.object(probe.subprocess, 'run', side_effect=[*self.READS, subprocess.TimeoutExpired('git push', 60),
                                                              subprocess.CompletedProcess([], 0)]) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                probe.git_checks('', 'fixture-branch')
        self.assertEqual(run.call_args.args[0], ['git', 'push', 'origin', '--delete', 'fixture-branch'])

    def test_failed_branch_deletion_is_reported(self):
        checks, _ = self.git(subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 1, stderr='denied'))
        self.assertTrue(checks['git_push'])
        self.assertFalse(checks['git_branch_removed'])

    def test_failed_push_with_no_branch_left_is_clean(self):
        checks, _ = self.git(subprocess.CompletedProcess([], 1),
                             subprocess.CompletedProcess([], 1, stderr='error: unable to delete: remote ref does not exist'))
        self.assertFalse(checks['git_push'])
        self.assertTrue(checks['git_branch_removed'])

    def test_successful_push_and_delete_pass(self):
        checks, _ = self.git(subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0))
        self.assertEqual(checks, {'git_transport_preserved': True, 'git_remote_read': True,
                                  'git_push': True, 'git_branch_removed': True})


if __name__ == '__main__':
    unittest.main()
