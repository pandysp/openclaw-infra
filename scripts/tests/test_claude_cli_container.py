"""Cancellation regression tests with real child processes; Docker is the boundary."""
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[2] / 'ansible/roles/claude-cli/files/claude-cli-container.py'
spec = importlib.util.spec_from_file_location('claude_cli_container', SOURCE)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class ContainerCancellationTest(unittest.TestCase):
    def test_guard_lifetime_requires_its_exact_whole_file_posix_write_lock(self):
        metadata = SimpleNamespace(st_dev=os.makedev(0, 35), st_ino=567)
        for lock, present in [
            ('1: POSIX ADVISORY WRITE 42 00:23:567 0 EOF', True),
            ('1: POSIX ADVISORY WRITE 42 00:23:568 0 EOF', False),
            ('1: POSIX ADVISORY READ 42 00:23:567 0 EOF', False),
            ('1: FLOCK ADVISORY WRITE 42 00:23:567 0 EOF', False),
            ('1: POSIX ADVISORY WRITE 42 00:23:567 1 EOF', False),
            ('1: -> POSIX ADVISORY WRITE 42 00:23:567 0 EOF', False),
            ('', False),
        ]:
            with self.subTest(lock=lock), patch.object(launcher.Path, 'read_text', return_value=lock):
                self.assertEqual(launcher.guard_lifetime_present(metadata), present)

    def test_stale_ready_byte_without_live_guard_cannot_create(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state'
            state.write_text('1')
            real_open = os.open
            metadata = SimpleNamespace(st_uid=0, st_mode=0o100644)
            with patch.object(launcher.os, 'open', side_effect=lambda path, flags: real_open(state, flags)), \
                 patch.object(launcher.os, 'fstat', return_value=metadata), \
                 patch.object(launcher, 'guard_lifetime_present', return_value=False), \
                 patch.object(launcher.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout='active\n')) as run:
                with self.assertRaisesRegex(SystemExit, 'no live admission owner'):
                    launcher.create_guarded(['docker', 'create', 'fixture'], 'fixture', 'fixture.service')
                run.assert_not_called()

    def test_guard_revocation_during_create_never_starts_the_created_container(self):
        handlers = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        try:
            with tempfile.TemporaryDirectory() as directory:
                state = Path(directory) / 'state'
                state.write_text('1')
                real_open = os.open
                with patch.object(launcher.os, 'open', side_effect=lambda path, flags: real_open(state, flags)), \
                     patch.object(launcher.os, 'fstat', return_value=SimpleNamespace(st_uid=0, st_mode=0o100644)), \
                     patch.object(launcher, 'guard_lifetime_present', side_effect=[True, False]), \
                     patch.object(launcher.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout='active\n')) as run, \
                     patch.object(launcher.subprocess, 'Popen') as start, \
                     patch.object(launcher, 'output', return_value='created-cid'):
                    with self.assertRaisesRegex(SystemExit, 'no live admission owner'):
                        launcher.run_container(['docker', 'start', 'fixture-created'], 'fixture-created',
                            prepare=lambda: launcher.create_guarded(['docker', 'create', 'fixture-created'], 'fixture', 'fixture.service'))
                    start.assert_not_called()
                    self.assertEqual([call.args[0][:2] for call in run.call_args_list],
                                     [['systemctl', 'is-active'], ['docker', 'create'], ['docker', 'rm']])
        finally:
            for s, handler in handlers.items():
                signal.signal(s, handler)

    def test_signal_inside_popen_before_assignment_reaps_client(self):
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum):
                handlers = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
                children = []
                real_popen = subprocess.Popen

                def launch_then_signal(command):
                    child = real_popen(command)
                    children.append(child)
                    os.kill(os.getpid(), signum)
                    return child

                try:
                    with patch.object(launcher.subprocess, 'Popen', side_effect=launch_then_signal), \
                         patch.object(launcher, 'output', return_value='container-id'), \
                         patch.object(launcher.subprocess, 'run') as remove:
                        with self.assertRaises(SystemExit) as exit:
                            launcher.run_container([sys.executable, '-c', 'import time; time.sleep(60)'], 'regression-test')
                        self.assertEqual(exit.exception.code, 128 + signum)
                        self.assertIsNotNone(children[0].poll())
                        remove.assert_called_once_with(['docker', 'rm', '-f', 'regression-test'], check=True,
                                                       stdout=subprocess.DEVNULL, timeout=15)
                finally:
                    for child in children:
                        if child.poll() is None:
                            child.kill()
                        child.wait(timeout=5)
                    for s, handler in handlers.items():
                        signal.signal(s, handler)

    def test_reap_failure_does_not_skip_docker_cleanup(self):
        handlers = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = [SystemExit(143), subprocess.TimeoutExpired('client', 10), subprocess.TimeoutExpired('client', 5)]
        try:
            with patch.object(launcher.subprocess, 'Popen', return_value=child), \
                 patch.object(launcher, 'output', return_value='container-id') as inspect, \
                 patch.object(launcher.subprocess, 'run') as remove:
                with self.assertRaises(subprocess.TimeoutExpired):
                    launcher.run_container(['docker', 'run'], 'regression-test')
                inspect.assert_called_once()
                remove.assert_called_once_with(['docker', 'rm', '-f', 'regression-test'], check=True,
                                               stdout=subprocess.DEVNULL, timeout=15)
        finally:
            for s, handler in handlers.items():
                signal.signal(s, handler)



class MacAccessTest(unittest.TestCase):
    """Mac SSH is optional: without a host, nothing Mac-related reaches the container."""

    def test_no_host_means_no_mounts_lookup_or_host_entry(self):
        mounts = []
        with patch.object(launcher.socket, 'getaddrinfo') as lookup:
            self.assertEqual(launcher.mac_access('', mounts.append), [])
        lookup.assert_not_called()
        self.assertEqual(mounts, [])

    def test_configured_host_mounts_key_and_pin_and_pins_its_address(self):
        mounts = []
        with patch.object(launcher.socket, 'getaddrinfo', return_value=[(None, None, None, None, ('100.64.0.7', 22))]):
            options = launcher.mac_access('mac-air', mounts.append)
        self.assertEqual(options, ['--add-host', 'mac-air:100.64.0.7'])
        self.assertEqual(mounts, [launcher.HOME / '.ssh/id_ed25519_openclaw_mac_air',
                                  launcher.HOME / '.ssh/known_hosts_openclaw_mac_air'])

    def test_invalid_host_fails_loudly(self):
        with self.assertRaises(SystemExit):
            launcher.mac_access('bad host;', lambda path: None)


if __name__ == '__main__':
    unittest.main()
