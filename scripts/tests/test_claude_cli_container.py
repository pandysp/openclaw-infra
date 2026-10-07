"""Cancellation regression tests with real child processes; Docker is the boundary."""
import importlib.util
import json
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



BTW_ARGV = ['-p', '--output-format', 'stream-json', '--include-partial-messages', '--verbose', '--setting-sources', 'user',
            '--safe-mode', '--tools', '', '--disallowedTools', 'mcp__*', '--strict-mcp-config', '--no-session-persistence',
            '--max-turns', '1', '--permission-mode', 'default', '--model', 'claude-sonnet-5-5',
            '--append-system-prompt-file', '{artifacts}/system-prompt.md']
COMPACT_ARGV = ['-p', '--output-format', 'stream-json', '--include-partial-messages', '--verbose', '--setting-sources', 'user',
                '--allowedTools', 'mcp__openclaw__*', '--resume', 'fixture-resumed', '--permission-mode', 'bypassPermissions',
                '--disallowedTools', 'Task', '--exclude-dynamic-system-prompt-sections', '--model', 'claude-sonnet-5-5', '/compact']

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


class LauncherMacScopeTest(unittest.TestCase):
    """Exercise main() through Docker's command boundary, not just mac_access()."""

    def launch(self, agent, mac_host, skill_root=None, extra_args=(), session_args=('--session-id', 'fixture-session'), refusal=None,
               openclaw_args=None, appended=None):
        with tempfile.TemporaryDirectory(prefix='launcher-mac-') as directory:
            home = Path(directory).resolve()
            workspace = home / 'workspace'
            workspace.mkdir()
            auth = home / '.claude/shared/auth'
            auth.mkdir(parents=True)
            (auth / '.credentials.json').write_text('{}')
            (home / '.claude/settings.json').write_text('{}')
            native = home / 'native'
            native.write_text('fixture')
            ssh = home / '.ssh'
            ssh.mkdir()
            for name in ('config', 'id_ed25519_openclaw_mac_air', 'known_hosts_openclaw_mac_air'):
                (ssh / name).write_text('fixture')
            state = home / '.openclaw'
            state.mkdir()
            (state / 'openclaw.json').write_text(json.dumps({'agents': {
                'entries': {agent: {'workspace': str(workspace)}}, 'defaults': {}}}))
            entry = {'config': str(ssh / 'config'), 'workspace_key': None}
            if mac_host is not None:
                entry['mac_host'] = mac_host
            runtime = state / 'runtime.json'
            runtime.write_text(json.dumps({
                'homes': str(home / 'homes'), 'ssh': {agent: entry},
                'guard_table': 'fixture', 'guard_service': 'fixture.service',
                'network': 'fixture', 'image': 'fixture', 'env_names': [],
                'mcp_url': 'http://172.30.0.1:8787/openclaw/mcp',
                'mcp_target': str(state / 'target.json'), 'invocations': str(state / 'invocations.jsonl'),
                'extra_args': list(extra_args),
            }))
            with tempfile.TemporaryDirectory(prefix='openclaw-main-test-', dir='/tmp') as artifacts:
                mcp = Path(artifacts) / 'mcp.json'
                mcp.write_text(json.dumps({'mcpServers': {'openclaw': {
                    'type': 'http', 'url': 'http://127.0.0.1:1234/mcp'}}}))
                args = ['--mcp-config', str(mcp), '--strict-mcp-config', *session_args]
                if openclaw_args is not None:
                    (Path(artifacts) / 'system-prompt.md').write_text('fixture')
                    args = [arg.replace('{artifacts}', artifacts) for arg in openclaw_args]
                if skill_root is not None:
                    skill = home / skill_root / 'skills/discord'
                    skill.mkdir(parents=True)
                    plugin = Path(artifacts) / 'plugin'
                    (plugin / 'skills').mkdir(parents=True)
                    (plugin / 'skills/discord').symlink_to(skill)
                    args += ['--plugin-dir', str(plugin)]
                read_text, resolve = Path.read_text, Path.resolve
                temporary_directory = tempfile.TemporaryDirectory

                def read(path, *args, **kwargs):
                    if str(path) == '/proc/self/stat':
                        return '1 (fixture) ' + ' '.join(['0'] * 20)
                    return read_text(path, *args, **kwargs)

                def canonical(path, *args, **kwargs):
                    # Linux /tmp is canonical; macOS /tmp is a symlink.
                    if str(path).startswith('/tmp/openclaw-main-test-') and not path.is_symlink():
                        return path
                    return resolve(path, *args, **kwargs)

                def docker_output(command):
                    if command[:3] == ['docker', 'network', 'inspect']:
                        return json.dumps([{'Id': 'fixture', 'Driver': 'bridge', 'EnableIPv6': True,
                            'Options': {'com.docker.network.bridge.name': 'fixture'}}])
                    return '{"nftables": []}'

                with patch.object(launcher, 'HOME', home), patch.object(launcher, 'SECURE_STORAGE', auth), \
                     patch.object(launcher, 'NATIVE', native), \
                     patch.dict(os.environ, {}, clear=True), \
                     patch.object(Path, 'cwd', return_value=workspace), \
                     patch.object(Path, 'read_text', read), patch.object(Path, 'resolve', canonical), \
                     patch.object(launcher.tempfile, 'TemporaryDirectory', side_effect=lambda **kwargs:
                         temporary_directory(prefix=kwargs['prefix'], dir=home)), \
                     patch.object(launcher.socket, 'getaddrinfo', return_value=[(None, None, None, None, ('100.64.0.7', 22))]) as dns, \
                     patch.object(launcher, 'output', side_effect=docker_output), \
                     patch.object(launcher.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout='active\n')), \
                     patch.object(launcher, 'validate_guard'), \
                     patch.object(launcher, 'create_guarded') as create, \
                     patch.object(launcher, 'run_container', side_effect=lambda *args, prepare: prepare() or 0) as run:
                    if mac_host is None:
                        with self.assertRaisesRegex(KeyError, 'mac_host'):
                            launcher.main(args, runtime)
                        create.assert_not_called()
                        run.assert_not_called()
                        return
                    if refusal:
                        with self.assertRaisesRegex(SystemExit, refusal):
                            launcher.main(args, runtime)
                        create.assert_not_called()
                        return
                    launcher.main(args, runtime)
                    command = create.call_args.args[0]
                    appended = list(extra_args) if appended is None else appended
                    self.assertEqual(command[-len(args) - len(appended) - 1:], ['fixture', *args, *appended])
                    session = next((args[i + 1] for i, arg in enumerate(args) if arg in ('--session-id', '--resume')), 'none')
                    self.assertIn('openclaw.claude-session=' + session, command)
                    self.assertIn('openclaw.claude-agent=' + agent, command)
                    bindings = [command[i + 1] for i, value in enumerate(command) if value == '--mount']
                    mac_bindings = [value for value in bindings if 'openclaw_mac_air' in value]
                    if skill_root is not None:
                        skill = home / skill_root / 'skills/discord'
                        self.assertIn(f'type=bind,source={skill},target={skill},readonly', bindings)
                    if mac_host:
                        self.assertEqual(mac_bindings, [
                            f'type=bind,source={ssh / name},target={ssh / name},readonly'
                            for name in ('id_ed25519_openclaw_mac_air', 'known_hosts_openclaw_mac_air')])
                        self.assertEqual(command[command.index('--add-host') + 1], 'mac-air:100.64.0.7')
                        dns.assert_called_once()
                    else:
                        self.assertEqual(mac_bindings, [])
                        self.assertNotIn('--add-host', command)
                        dns.assert_not_called()

    def test_main_gets_mac_key_pin_and_host_entry(self):
        self.launch('main', 'mac-air')

    def test_other_agent_gets_no_mac_mounts_lookup_or_host_entry(self):
        self.launch('other', '')

    def test_plugin_package_skill_is_mounted_read_only(self):
        # From 2026.7.1, channel plugins ship skills that OpenClaw links from their installed package.
        self.launch('main', 'mac-air', '.openclaw/npm/projects/openclaw-discord-fixture/node_modules/@openclaw/discord')

    def test_extra_args_follow_openclaws_args(self):
        # Staging turns Claude's own tools off; OpenClaw's tools.allow does not reach them.
        self.launch('other', '', extra_args=('--tools', ''))

    def test_extra_flag_openclaw_passes_with_another_value_is_refused(self):
        self.launch('other', '', extra_args=('--max-turns', '5'), openclaw_args=BTW_ARGV, refusal='with another value')

    def test_resumed_turn_is_labelled_with_its_session(self):
        self.launch('other', '', session_args=('--resume', 'fixture-resumed'))

    def test_btw_side_question_runs_unlabelled_without_mcp(self):
        # Captured from a live /btw on 2026.9.8: no session, no MCP config, Claude's tools off.
        self.launch('other', '', openclaw_args=BTW_ARGV)
        with self.subTest('staging lockdown already passed by OpenClaw'):
            self.launch('other', '', extra_args=('--tools', ''), openclaw_args=BTW_ARGV, appended=[])

    def test_compaction_resumes_its_session_without_mcp(self):
        # Captured from a live `openclaw sessions compact` on 2026.9.8.
        self.launch('other', '', openclaw_args=COMPACT_ARGV)

    def test_login_status_query_runs_the_real_claude_without_a_session(self):
        # OpenClaw checks the login before turns, outside any workspace.
        with patch.object(launcher.os, 'execv', side_effect=SystemExit('exec')) as execv:
            with self.assertRaisesRegex(SystemExit, 'exec'):
                launcher.main(['auth', 'status', '--json'], '/nonexistent/runtime.json')
        execv.assert_called_once_with(str(launcher.NATIVE), [str(launcher.NATIVE), 'auth', 'status', '--json'])

    def test_bundled_extension_skill_is_mounted_read_only(self):
        # From 2026.9.8, bundled extensions (e.g. custodian-skills) link skills from the OpenClaw package.
        self.launch('main', 'mac-air', '.npm-global/lib/node_modules/openclaw/dist/extensions/custodian-skills')

    def test_skill_outside_known_roots_is_refused(self):
        self.launch('main', 'mac-air', 'elsewhere', refusal='outside expected skill roots')

    def refused(self, entries, cwd, args, message):
        with tempfile.TemporaryDirectory(prefix='launcher-scope-') as directory:
            home = Path(directory).resolve()
            (home / '.openclaw').mkdir()
            (home / '.openclaw/openclaw.json').write_text(json.dumps({'agents': {
                'entries': {agent: {'workspace': str(home / folder)} for agent, folder in entries.items()},
                'defaults': {}}}))
            runtime = home / 'runtime.json'
            runtime.write_text('{}')
            with patch.object(launcher, 'HOME', home), patch.dict(os.environ, {}, clear=True), \
                 patch.object(Path, 'cwd', return_value=home / cwd), patch.object(launcher, 'output') as docker:
                with self.assertRaisesRegex(SystemExit, message):
                    launcher.main(args, runtime)
            docker.assert_not_called()

    def test_workspace_of_no_agent_is_refused_before_docker(self):
        # From 2026.9.8 OpenClaw names no agent; the workspace OpenClaw starts Claude in decides.
        self.refused({'main': 'main'}, 'elsewhere', ['--session-id', 'fixture'], 'exactly one configured agent workspace')

    def test_workspace_shared_by_two_agents_is_refused_before_docker(self):
        self.refused({'main': 'shared', 'other': 'shared'}, 'shared', ['--session-id', 'fixture'],
                     'exactly one configured agent workspace')

    def test_launch_without_claude_session_is_refused_before_docker(self):
        self.refused({'main': 'main'}, 'main', ['--mcp-config', '/tmp/openclaw-x/mcp.json'], 'names no Claude session')

    def test_old_manifest_fails_before_docker_creation(self):
        self.launch('other', None)


if __name__ == '__main__':
    unittest.main()
