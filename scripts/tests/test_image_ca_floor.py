#!/usr/bin/env python3
"""Real Docker and Debian packages for the image CA floor; run with IMAGE_CA_FLOOR_TESTS=1.

Builds one image with Debian's pre-incident ca-certificates and one with the current
package, then runs the real ansible/roles/docker/tasks/image-ca-floor.yml against them.
Needs Docker and dpkg (Lint's Ubuntu runner has both).
"""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT/'ansible/roles/docker/tasks/image-ca-floor.yml'
ALL = ROOT/'ansible/group_vars/all.yml'
WORKSPACE_FILES = ROOT/'ansible/roles/workspace/files'
# Debian 13's release package, which still trusts the eMudhra roots.
STALE_VERSION = '20250419'
IMAGE = 'openclaw-ca-floor-test'


def build(tag, dockerfile, *args, context=None):
    with tempfile.TemporaryDirectory(prefix='ca-floor-build-') as directory:
        if dockerfile is not None:
            (Path(directory)/'Dockerfile').write_text(dockerfile)
        return subprocess.run(['docker', 'build', *args, '-t', tag, str(context or directory)],
                              capture_output=True, text=True, timeout=600)


@unittest.skipUnless(os.environ.get('IMAGE_CA_FLOOR_TESTS') == '1',
                     'Run IMAGE_CA_FLOOR_TESTS=1 python3 -m unittest scripts.tests.test_image_ca_floor')
class ImageCaFloorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.floor, = re.findall(r'(?m)^debian_ca_certificates_min_version: "([^"]+)"$', ALL.read_text())
        base, = re.findall(r'(?m)^FROM (\S+)$', (WORKSPACE_FILES/'Dockerfile').read_text())
        for tag, package in (('stale', 'ca-certificates=' + STALE_VERSION), ('current', 'ca-certificates')):
            result = build(f'{IMAGE}:{tag}', f'FROM {base}\nRUN apt-get update && apt-get install -y '
                           f'--no-install-recommends {package} && rm -rf /var/lib/apt/lists/*\n')
            if result.returncode:
                raise RuntimeError(result.stdout[-3000:] + result.stderr[-3000:])
        subprocess.run(['docker', 'image', 'rm', f'{IMAGE}:absent'], capture_output=True)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(['docker', 'image', 'rm', f'{IMAGE}:stale', f'{IMAGE}:current'], capture_output=True)

    def classify(self, tag, require=False):
        with tempfile.TemporaryDirectory(prefix='ca-floor-') as directory:
            play = [{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                     'vars': {'debian_ca_certificates_min_version': self.floor},
                     'tasks': [{'ansible.builtin.include_tasks': str(TASKS),
                                'vars': {'image_ca_floor_image': f'{IMAGE}:{tag}', 'image_ca_floor_require': require}},
                               {'ansible.builtin.copy': {'dest': f'{directory}/result.json',
                                 'content': '{{ {"state": image_ca_floor, "version": image_ca_floor_version} | to_json }}'}}]}]
            (Path(directory)/'play.json').write_text(json.dumps(play))
            env = {**os.environ, 'ANSIBLE_NOCOLOR': '1', 'ANSIBLE_LOCAL_TEMP': f'{directory}/tmp'}
            run = subprocess.run(['ansible-playbook', '-i', 'localhost,', f'{directory}/play.json'],
                                 capture_output=True, text=True, env=env, timeout=300)
            result = Path(directory)/'result.json'
            return run, json.loads(result.read_text()) if result.exists() else None

    def test_images_are_classified_against_the_floor(self):
        for tag, state in (('stale', 'stale'), ('current', 'current'), ('absent', 'missing')):
            with self.subTest(tag=tag):
                run, result = self.classify(tag)
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                self.assertEqual(result['state'], state, result)
        _, stale = self.classify('stale')
        self.assertEqual(stale['version'], STALE_VERSION)

    def test_required_floor_fails_loudly_for_stale_and_missing_images(self):
        for tag in ('stale', 'absent'):
            with self.subTest(tag=tag):
                run, result = self.classify(tag, require=True)
                self.assertNotEqual(run.returncode, 0, 'A stale image passed the required CA floor')
                self.assertIn(f'it needs {self.floor} or later', run.stdout)
                self.assertIsNone(result)
        run, result = self.classify('current', require=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_workspace_dockerfile_refuses_a_missing_or_unmet_floor(self):
        for args in ([], ['--build-arg', 'CA_CERTIFICATES_MIN_VERSION=99999999']):
            with self.subTest(args=args):
                result = build(f'{IMAGE}:workspace-refused', None, *args, context=WORKSPACE_FILES)
                self.assertNotEqual(result.returncode, 0, 'Workspace image built without meeting the CA floor')
        subprocess.run(['docker', 'image', 'rm', f'{IMAGE}:workspace-refused'], capture_output=True)


if __name__ == '__main__':
    unittest.main()
