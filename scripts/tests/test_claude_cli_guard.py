"""Guard lifecycle regression tests; nftables and Docker are the external boundaries."""
import configparser
import errno
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[2] / 'ansible/roles/claude-cli/files/claude-cli-guard.py'
spec = importlib.util.spec_from_file_location('claude_cli_guard', SOURCE)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class GuardTest(unittest.TestCase):
    def setUp(self):
        if sys.platform != 'linux':
            # The runtime is Linux-only. Darwin merges the flock/record domains;
            # exercise those kernel semantics in the actual VPS fixture instead.
            lockf = patch.object(guard.fcntl, 'lockf')
            lockf.start()
            self.addCleanup(lockf.stop)

    def test_service_preserves_bounded_cleanup_children(self):
        unit = configparser.ConfigParser(interpolation=None, strict=False)
        unit.read(SOURCE.with_name('network-guard.service'))
        self.assertEqual(unit['Service']['KillMode'], 'mixed')
        self.assertGreaterEqual(int(unit['Service']['TimeoutStopSec']), 10 + 20 + 15 + 5)
        self.assertGreaterEqual(int(unit['Service']['TimeoutStartSec']), 20 + 15 + 20 + 15 + 5)

    def test_snapshot_ignores_kernel_handles_not_policy(self):
        def data(handle, port):
            return json.dumps({'nftables': [{'metainfo': {'version': 'fixture'}},
                {'rule': {'family': 'inet', 'table': 'fixture', 'handle': handle,
                          'expr': [{'match': {'right': port}}]}}]})
        with patch.object(guard.subprocess, 'check_output', return_value=data(1, 80)):
            first = guard.snapshot('fixture')
        with patch.object(guard.subprocess, 'check_output', return_value=data(2, 80)):
            self.assertEqual(first, guard.snapshot('fixture'))
        with patch.object(guard.subprocess, 'check_output', return_value=data(2, 81)):
            self.assertNotEqual(first, guard.snapshot('fixture'))

    def test_cleanup_selects_only_its_guard(self):
        with patch.object(guard.subprocess, 'check_output', return_value='first\nsecond\n') as inspect, \
             patch.object(guard.subprocess, 'run') as remove:
            guard.stop_containers('fixture')
        inspect.assert_called_once_with(['docker', 'ps', '-aq', '--filter', 'label=openclaw.claude-guard=fixture'],
                                        text=True, timeout=5)
        remove.assert_called_once_with(['docker', 'rm', '-f', 'first', 'second'], check=True,
                                       stdout=subprocess.DEVNULL, timeout=15)

    def test_orphan_cleanup_handles_disappeared_reused_and_zombie_owners(self):
        fields = ['S'] + ['0'] * 18 + ['123']
        for current in (' '.join(fields), ' '.join(['Z', *fields[1:]]),
                        ' '.join(['X', *fields[1:]]),
                        ' '.join([*fields[:-1], '999']), FileNotFoundError(), ProcessLookupError()):
            with self.subTest(state=type(current).__name__):
                reader = {'side_effect': current} if isinstance(current, Exception) else {'return_value': '42 (owner (with spaces)) ' + current}
                with patch.object(guard.subprocess, 'check_output', return_value='container 42:123\n'), \
                     patch.object(guard.Path, 'read_text', **reader), \
                     patch.object(guard.subprocess, 'run') as remove:
                    guard.reap_orphans('fixture')
                    self.assertEqual(remove.call_count, 0 if current == ' '.join(fields) else 1)

    def test_admission_or_lock_failure_cannot_skip_final_cleanup_and_restore(self):
        handlers = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        opener, flock = open, guard.fcntl.flock
        try:
            for failure in ('write', 'lock'):
                with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                    class FaultFile:
                        def __init__(self, stream):
                            self.stream, self.writes = stream, 0
                        def __getattr__(self, name):
                            return getattr(self.stream, name)
                        def __enter__(self):
                            return self
                        def __exit__(self, *args):
                            return self.stream.__exit__(*args)
                        def write(self, value):
                            self.writes += 1
                            if failure == 'write' and self.writes == 3:
                                raise OSError(errno.EIO, 'injected final admission write failure')
                            return self.stream.write(value)
                    lock_calls = []
                    def faulty_lock(state, mode):
                        lock_calls.append(mode)
                        if failure == 'lock' and len(lock_calls) == 3:
                            raise OSError(errno.EIO, 'injected final lock acquisition failure')
                        return flock(state, mode)
                    with patch.object(guard, 'open', side_effect=lambda *a, **kw: FaultFile(opener(*a, **kw)), create=True), \
                         patch.object(guard.fcntl, 'flock', side_effect=faulty_lock), \
                         patch.object(guard, 'stop_containers') as cleanup, \
                         patch.object(guard, 'snapshot', return_value=[]), \
                         patch.object(guard, 'ready', side_effect=lambda: signal.raise_signal(signal.SIGTERM)), \
                         patch.object(guard.subprocess, 'run') as restore:
                        with self.assertRaises(OSError) as error:
                            guard.main('/fixture/policy.nft', 'fixture', Path(directory) / 'state.lock')
                        self.assertEqual(error.exception.errno, errno.EIO)
                        self.assertEqual(lock_calls, [guard.fcntl.LOCK_EX, guard.fcntl.LOCK_UN, guard.fcntl.LOCK_EX])
                        self.assertEqual(cleanup.call_count, 2)
                        self.assertEqual(restore.call_count, 2)
                        self.assertEqual(restore.call_args.args[0], ['nft', '--file', '/fixture/policy.nft'])
        finally:
            for s, handler in handlers.items():
                signal.signal(s, handler)

    def test_state_initialization_failure_still_stops_containers_and_restores(self):
        handlers = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        try:
            for boundary in ('mkdir', 'open'):
                with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                    target = guard.Path if boundary == 'mkdir' else guard.os
                    with patch.object(target, boundary, side_effect=OSError(errno.EIO, 'injected state initialization failure')), \
                         patch.object(guard, 'stop_containers') as cleanup, \
                         patch.object(guard.subprocess, 'run') as restore:
                        with self.assertRaises(OSError):
                            guard.main('/fixture/policy.nft', 'fixture', Path(directory) / 'state.lock')
                        cleanup.assert_called_once_with('fixture')
                        restore.assert_called_once_with(['nft', '--file', '/fixture/policy.nft'], check=True, timeout=15)
        finally:
            for s, handler in handlers.items():
                signal.signal(s, handler)

    def test_host_artifact_cleanup_is_scoped_to_dead_owners(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            def artifact(table, pid):
                path = root / f'openclaw-claude-cli-{table}-{os.getuid()}-{pid}-123-test'
                path.mkdir()
                (path / 'mcp.json').write_text('{}')
                return path
            live, dead, unrelated = artifact('fixture', 42), artifact('fixture', 43), artifact('other', 43)
            legacy = root / 'openclaw-claude-cli-untracked'
            legacy.mkdir()
            with patch.object(guard, 'owner_alive', side_effect=lambda owner: owner == '42:123'):
                guard.reap_artifacts('fixture', root)
            self.assertTrue(live.exists())
            self.assertFalse(dead.exists())
            self.assertTrue(unrelated.exists())
            self.assertTrue(legacy.exists())

    def test_host_artifact_cleanup_rejects_symlinks_and_wrong_uid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / 'unrelated'
            target.mkdir()
            symlink = root / f'openclaw-claude-cli-fixture-{os.getuid()}-43-123-test'
            symlink.symlink_to(target, target_is_directory=True)
            with self.assertRaises(RuntimeError):guard.reap_artifacts('fixture', root)
            self.assertTrue(target.exists())
            symlink.unlink()
            wrong = root / f'openclaw-claude-cli-fixture-{os.getuid()+1}-43-123-test'
            wrong.mkdir()
            with self.assertRaises(RuntimeError):guard.reap_artifacts('fixture', root)
            self.assertTrue(wrong.exists())

    def test_docker_failure_cannot_skip_host_artifact_cleanup(self):
        for cleanup in (guard.reap_orphans, guard.stop_containers):
            with self.subTest(cleanup=cleanup.__name__), \
                 patch.object(guard.subprocess, 'check_output', side_effect=subprocess.TimeoutExpired('docker', 5)), \
                 patch.object(guard, 'reap_artifacts') as artifacts:
                with self.assertRaises(subprocess.TimeoutExpired):cleanup('fixture')
                artifacts.assert_called_once_with('fixture')

    def test_host_artifact_already_removed_is_not_a_guard_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / f'openclaw-claude-cli-fixture-{os.getuid()}-43-123-test'
            path.mkdir()
            with patch.object(guard.Path, 'lstat', side_effect=FileNotFoundError):
                guard.reap_artifacts('fixture', root)

    def test_docker_failure_does_not_prevent_guard_restoration(self):
        handlers = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        try:
            with patch.object(guard, 'stop_containers', side_effect=subprocess.TimeoutExpired('docker', 5)), \
                 patch.object(guard.subprocess, 'run') as apply:
                with tempfile.TemporaryDirectory() as directory:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        guard.main('/fixture/policy.nft', 'fixture', Path(directory) / 'state.lock')
                self.assertEqual(apply.call_count, 2)
                for call in apply.call_args_list:
                    self.assertEqual(call.args[0], ['nft', '--file', '/fixture/policy.nft'])
        finally:
            for s, handler in handlers.items():
                signal.signal(s, handler)


if __name__ == '__main__':
    unittest.main()
