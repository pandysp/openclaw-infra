#!/usr/bin/env python3
"""Run scripts/lib/tls-trust-check.py's images section against a scripted docker CLI.

The fake docker answers probe runs with chosen outcomes and reports containers the way
the real CLI does, including a bare image ID in `docker ps` once a tag has moved.
Real probes against the incident's test sites run in Phoenix (verify-tls-trust.sh).
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

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
    for c in state['containers']:
        if c['id'] in args:
            print(f"/{c['name']} {c['config_image']} {c['image_id']}")
else:
    sys.exit(f'unexpected docker call: {args}')
'''
IMAGES = {'openclaw-sandbox:trixie': 'sha256:base', 'openclaw-sandbox-custom:latest': 'sha256:custom',
          'openclaw-claude-cli:latest': 'sha256:claude', 'openclaw-workspace-sync:latest': 'sha256:sync'}


class TlsTrustCheckTest(unittest.TestCase):
    def check(self, *sections, **state):
        with tempfile.TemporaryDirectory(prefix='tls-trust-') as directory:
            bin_dir = Path(directory)/'bin'
            bin_dir.mkdir()
            (bin_dir/'docker').write_text(DOCKER)
            (bin_dir/'docker').chmod(0o755)
            (Path(directory)/'state.json').write_text(json.dumps({'images': IMAGES, 'containers': [], **state}))
            env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}', 'FAKE_DOCKER': f'{directory}/state.json'}
            return subprocess.run([sys.executable, str(CHECK), '26.12.1', *(sections or ['images'])],
                                  capture_output=True, text=True, env=env, timeout=60)

    def test_current_images_and_containers_pass(self):
        result = self.check(containers=[{'id': 'c1', 'name': 'openclaw-sbx-main', 'config_image': 'openclaw-sandbox-custom:latest',
                                         'image_id': 'sha256:custom'}])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PASS image openclaw-sandbox-custom:latest uv: unsafe rejected 12/12, healthy 4/4', result.stdout)
        self.assertIn('PASS running containers: 1 from OpenClaw images', result.stdout)
        self.assertIn('All TLS trust checks passed', result.stdout)

    def test_an_accepted_unsafe_chain_fails(self):
        result = self.check(accepted={'openclaw-workspace-sync:latest': ['dv-g3b-emudhra.we-have-a-problem.com']})
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

    def test_optional_images_are_reported_as_skipped_but_the_sandbox_is_required(self):
        images = {k: v for k, v in IMAGES.items() if k == 'openclaw-sandbox-custom:latest'}
        result = self.check(images=images)
        self.assertEqual(result.returncode, 1)
        self.assertIn('SKIP image openclaw-claude-cli:latest: not built on this server', result.stdout)
        self.assertIn('FAIL image openclaw-sandbox:trixie: Error response from daemon: No such image', result.stdout)

    def test_an_unknown_section_fails_instead_of_checking_nothing(self):
        result = self.check('imagse')
        self.assertEqual(result.returncode, 1)
        self.assertIn('unknown section(s) imagse', result.stderr)
        self.assertNotIn('passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
