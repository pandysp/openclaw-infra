#!/usr/bin/env python3
"""Run scripts/lib/tls-trust-check.py's images section against a scripted docker CLI.

The fake docker answers probe runs with chosen outcomes and reports containers the way
the real CLI does, including a bare image ID in `docker ps` once a tag has moved.
Real probes against the incident's test sites run in Phoenix (verify-tls-trust.sh).
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from test_restart_replaced_programs import SYSTEMCTL

ROOT = Path(__file__).resolve().parents[2]
CHECK = ROOT/'scripts/lib/tls-trust-check.py'

DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
state = json.loads(open(os.environ['FAKE_DOCKER']).read())
args = sys.argv[1:]
if args[:2] == ['image', 'inspect']:
    image = args[-1]
    if image not in state['images']:
        sys.exit(f'Error response from daemon: No such image: {image}')
    print(state['images'][image] + (' 2026-10-28T00:00:00Z' if '{{.Id}} {{.Created}}' in args else ''))
elif args[0] == 'run':
    image = args[args.index('--entrypoint') + 2]
    hosts = args[args.index(image) + 2:]
    accepted = set(state.get('accepted', {}).get(image, []))
    unreachable = set(state.get('unreachable', []))
    for host in hosts:
        if host in unreachable:
            outcome = 'error'
        elif '-emudhra.' in host:
            outcome = 'accept' if host in accepted else 'reject'
        else:
            outcome = 'accept'
        print(json.dumps({'host': host, 'outcome': outcome, 'detail': 'fake'}))
elif args[:3] == ['ps', '-q', '--no-trunc']:
    print('\n'.join(c['id'] for c in state['containers']))
elif args[0] == 'inspect':
    gone = []
    for c in state['containers']:
        if c['id'] in args:
            if c.get('gone'):
                gone.append(c['id'])
            else:
                print(f"/{c['name']} {c['config_image']} {c['image_id']}")
    for container in gone:
        print(f'Error: No such object: {container}', file=sys.stderr)
    sys.exit(1 if gone else 0)
else:
    sys.exit(f'unexpected docker call: {args}')
'''
IMAGES = {'openclaw-sandbox:trixie': 'sha256:base', 'openclaw-sandbox-custom:latest': 'sha256:custom',
          'openclaw-claude-cli:latest': 'sha256:claude', 'openclaw-workspace-sync:latest': 'sha256:sync'}


class TlsTrustCheckTest(unittest.TestCase):
    def check(self, *sections, features=False, **state):
        """`features` leaves the markers of a server with Claude CLI containers and backups."""
        with tempfile.TemporaryDirectory(prefix='tls-trust-') as directory:
            bin_dir = Path(directory)/'bin'
            bin_dir.mkdir()
            (bin_dir/'docker').write_text(DOCKER)
            (bin_dir/'docker').chmod(0o755)
            (Path(directory)/'state.json').write_text(json.dumps({'images': IMAGES, 'containers': [], **state}))
            if features:
                units = Path(directory)/'.config/systemd/user'
                units.mkdir(parents=True)
                (units/'workspace-git-sync-main.timer').write_text('')
                (Path(directory)/'claude').write_text('')
            env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}', 'FAKE_DOCKER': f'{directory}/state.json',
                   'HOME': directory, 'CLAUDE_LAUNCHER': f'{directory}/claude'}
            return subprocess.run([sys.executable, str(CHECK), '26.12.1', *(sections or ['images'])],
                                  capture_output=True, text=True, env=env, timeout=60)

    def test_current_images_and_containers_pass(self):
        result = self.check(features=True, containers=[{'id': 'c1', 'name': 'openclaw-sbx-main', 'config_image': 'openclaw-sandbox-custom:latest',
                                         'image_id': 'sha256:custom'}])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PASS image openclaw-sandbox-custom:latest uv: unsafe rejected 12/12, healthy 4/4', result.stdout)
        self.assertIn('PASS running containers: 1 from OpenClaw images', result.stdout)
        self.assertIn('All TLS trust checks passed', result.stdout)

    def test_an_accepted_unsafe_chain_fails(self):
        result = self.check(features=True, accepted={'openclaw-workspace-sync:latest': ['dv-g3b-emudhra.we-have-a-problem.com']})
        self.assertEqual(result.returncode, 1)
        self.assertIn('FAIL image openclaw-workspace-sync:latest python: unsafe rejected 11/12, healthy 4/4; '
                      'ACCEPTED compromised chains: dv-g3b-emudhra.we-have-a-problem.com', result.stdout)

    def test_an_unreachable_test_site_is_inconclusive_not_rejected(self):
        result = self.check(unreachable=['ev-g3c-aaa-emudhra.we-have-a-problem.com', 'pypi.org'])
        self.assertEqual(result.returncode, 1)
        self.assertIn('unsafe rejected 11/12, healthy 3/4; inconclusive (not a certificate rejection): '
                      'ev-g3c-aaa-emudhra.we-have-a-problem.com', result.stdout)
        self.assertNotIn('All TLS trust checks passed', result.stdout)

    def test_a_container_on_an_older_build_fails_although_ps_shows_no_name(self):
        result = self.check(containers=[
            {'id': 'c1', 'name': 'openclaw-claude-1a2b', 'config_image': 'openclaw-claude-cli:latest', 'image_id': 'sha256:old'},
            {'id': 'c2', 'name': 'unrelated', 'config_image': 'postgres:17', 'image_id': 'sha256:pg'}])
        self.assertEqual(result.returncode, 1)
        self.assertIn('FAIL containers on an older image build (restart the gateway to recreate them): '
                      'openclaw-claude-1a2b (openclaw-claude-cli:latest)', result.stdout)

    def test_images_of_features_that_are_off_are_skipped_but_the_sandbox_is_required(self):
        images = {k: v for k, v in IMAGES.items() if k == 'openclaw-sandbox-custom:latest'}
        result = self.check(images=images)
        self.assertEqual(result.returncode, 1)
        self.assertIn('SKIP image openclaw-claude-cli:latest: its feature is off on this server', result.stdout)
        self.assertIn('SKIP image openclaw-workspace-sync:latest: its feature is off on this server', result.stdout)
        self.assertIn('FAIL image openclaw-sandbox:trixie: Error response from daemon: No such image', result.stdout)

    def test_a_left_over_image_of_a_feature_that_is_off_is_not_probed(self):
        result = self.check(accepted={'openclaw-claude-cli:latest': ['dv-g3b-emudhra.we-have-a-problem.com']})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('SKIP image openclaw-claude-cli:latest: its feature is off on this server', result.stdout)

    def test_an_image_of_a_disabled_feature_stays_under_test_while_containers_run_from_it(self):
        result = self.check(accepted={'openclaw-claude-cli:latest': ['dv-g3b-emudhra.we-have-a-problem.com']},
                            containers=[{'id': 'c1', 'name': 'openclaw-claude-1a2b', 'config_image': 'openclaw-claude-cli:latest',
                                         'image_id': 'sha256:claude'}])
        self.assertEqual(result.returncode, 1)
        self.assertIn('FAIL image openclaw-claude-cli:latest node: unsafe rejected 11/12', result.stdout)

    def test_a_container_that_exits_during_the_check_is_skipped(self):
        result = self.check(features=True, containers=[
            {'id': 'c1', 'name': 'openclaw-sbx-main', 'config_image': 'openclaw-sandbox-custom:latest', 'image_id': 'sha256:custom'},
            {'id': 'c2', 'name': 'openclaw-claude-gone', 'config_image': 'openclaw-claude-cli:latest', 'image_id': 'sha256:old',
             'gone': True}])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PASS running containers: 1 from OpenClaw images', result.stdout)

    def test_a_missing_image_of_a_feature_that_is_on_fails(self):
        images = {k: v for k, v in IMAGES.items() if k.startswith('openclaw-sandbox')}
        result = self.check(images=images, features=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('FAIL image openclaw-claude-cli:latest: Error response from daemon: No such image', result.stdout)
        self.assertIn('FAIL image openclaw-workspace-sync:latest: Error response from daemon: No such image', result.stdout)

    def test_an_unknown_section_fails_instead_of_checking_nothing(self):
        result = self.check('imagse')
        self.assertEqual(result.returncode, 1)
        self.assertIn('unknown section(s) imagse', result.stderr)
        self.assertNotIn('passed', result.stdout)


# A live process whose /proc entries its own user cannot read (not dumpable).
NONDUMPABLE = 'import ctypes, time; ctypes.CDLL(None).prctl(4, 0, 0, 0, 0); time.sleep(300)'


def hidden_ready(pid):
    """Wait until the process has made itself unreadable."""
    while True:
        try:
            os.readlink(f'/proc/{pid}/exe')
        except PermissionError:
            return


@unittest.skipUnless(sys.platform.startswith('linux'), 'Needs Linux /proc/<pid>/exe')
class NodeProcessDiscoveryTest(unittest.TestCase):
    """Real processes named node in a fixture cgroup tree, found the way check_host finds them."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='tls-node-'))
        self.processes = []
        self.addCleanup(self.cleanup)
        (self.tmp/'bin').mkdir()
        (self.tmp/'bin/systemctl').write_text(SYSTEMCTL)
        (self.tmp/'bin/systemctl').chmod(0o755)
        self.env = {'PATH': f'{self.tmp}/bin:{os.environ["PATH"]}', 'FIXTURE': str(self.tmp),
                    'CGROUP_ROOT': str(self.tmp/'cgroup'), 'HOME': str(self.tmp)}

    def cleanup(self):
        for process in self.processes:
            process.kill()
            process.wait()
        shutil.rmtree(self.tmp)

    def node(self, name, **env):
        program = self.tmp/name
        if not program.exists():
            shutil.copy2(shutil.which('sleep'), program)
        process = subprocess.Popen([str(program), '300'], env={'PATH': '/usr/bin:/bin', **env})
        self.processes.append(process)
        while os.readlink(f'/proc/{process.pid}/exe') != str(program):
            pass
        return process.pid

    def discover(self, units):
        for unit in units.values():
            for child, pids in unit.pop('procs').items():
                (self.tmp/'cgroup'/unit['cgroup'].lstrip('/')/child).mkdir(parents=True, exist_ok=True)
                (self.tmp/'cgroup'/unit['cgroup'].lstrip('/')/child/'cgroup.procs').write_text(''.join(f'{p}\n' for p in pids))
        (self.tmp/'units.json').write_text(json.dumps(units))
        with mock.patch.dict(os.environ, self.env):
            spec = importlib.util.spec_from_file_location('tls_trust_check', CHECK)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return [(unit, pid, Path(exe).name, trust) for unit, pid, exe, trust in module.node_processes()]

    def unit(self, procs, **extra):
        name = f'unit-{len(list(self.tmp.glob("cgroup/user.slice/*")))}-{len(procs)}'
        return {'fragment': f'{self.tmp}/.config/systemd/user/{name}.service', 'type': 'simple',
                'cgroup': f'/user.slice/{name}.service', 'procs': procs, **extra}

    def test_every_distinct_node_process_in_a_service_is_probed(self):
        parent, twin = self.node('node'), self.node('node')
        child = self.node('node', NODE_TLS_REJECT_UNAUTHORIZED='0')
        other = self.node('node-other')
        self.node('not-a-runtime')
        found = self.discover({'gateway.service': self.unit({'.': [parent, twin, self.processes[-1].pid], 'worker': [child, other]})})
        self.assertEqual(found, [('gateway.service', parent, 'node', {}),
                                 ('gateway.service', child, 'node', {'NODE_TLS_REJECT_UNAUTHORIZED': '0'}),
                                 ('gateway.service', other, 'node-other', {})])

    def test_a_live_process_that_cannot_be_inspected_stops_the_check(self):
        hidden = subprocess.Popen([sys.executable, '-c', NONDUMPABLE])
        self.processes.append(hidden)
        hidden_ready(hidden.pid)
        units = {'gateway.service': self.unit({'.': [self.node('node'), hidden.pid]})}
        with self.assertRaisesRegex(SystemExit, f'cannot inspect pid {hidden.pid} in gateway.service'):
            self.discover(units)

    def test_a_service_that_cannot_be_inspected_stops_the_check(self):
        units = {'gateway.service': self.unit({'.': [self.node('node')]}, broken=['FragmentPath'])}
        with self.assertRaisesRegex(SystemExit, 'systemctl --user show -P FragmentPath gateway.service failed'):
            self.discover(units)


if __name__ == '__main__':
    unittest.main()
