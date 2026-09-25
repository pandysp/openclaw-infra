#!/usr/bin/env python3
"""Run the real shell/Python cleaner with only the HTTP boundary scripted."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'cleanup-staging-tailnet.sh'
NAME = 'openclaw-staging-123456-2'
OWN = {'id': '42', 'nodeId': 'nOwned', 'hostname': NAME}
OTHER = {'id': '43', 'nodeId': 'nOther', 'hostname': 'openclaw-staging-987654-1'}
HTTP_FIXTURE = '''import io,json,os,pathlib,urllib.error,urllib.request
root=pathlib.Path(os.environ['FIXTURE_ROOT'])
opts=json.loads(os.environ['FIXTURE_OPTIONS'])
class API:
 def open(self,request,timeout):
  method=request.get_method();url=request.full_url
  with (root/'calls').open('a') as f:f.write(json.dumps({'method':method,'url':url})+'\\n')
  assert timeout==30
  if url.endswith('/oauth/token'):
   assert method=='POST' and b'client_secret=fixture-secret' in request.data
   return io.BytesIO(b'{"access_token":"fixture-bearer"}')
  assert request.get_header('Authorization')=='Bearer fixture-bearer'
  if method=='DELETE':
   assert url.endswith('/device/42'), 'Deletion escaped the proven device'
   if opts.get('delete_error'):raise urllib.error.HTTPError(url,500,'Failure',{},None)
   (root/'deleted').touch()
   return io.BytesIO(b'')
  assert method=='GET' and url.endswith('/tailnet/-/devices')
  if opts.get('read_error') or (opts.get('readback_error') and (root/'deleted').exists()):
   raise urllib.error.HTTPError(url,503,'Failure',{},None)
  if 'raw' in opts:return io.BytesIO(opts['raw'].encode())
  devices=opts.get('devices',[])
  if (root/'deleted').exists() and not opts.get('retained'):
   devices=opts.get('after',[d for d in devices if d['id']!='42'])
  return io.BytesIO(json.dumps({'devices':devices}).encode())
def build(*handlers):
 assert len(handlers)==1
 assert handlers[0].redirect_request(None,None,302,'',{},'http://outside.invalid') is None
 (root/'redirects-blocked').touch()
 return API()
urllib.request.build_opener=build
'''


class TailnetCleanupTests(unittest.TestCase):
    def run_cleaner(self, owned=False, node='nOwned', **options):
        with tempfile.TemporaryDirectory(prefix='tailnet-cleanup-') as tmp:
            root = Path(tmp)
            (root / 'sitecustomize.py').write_text(HTTP_FIXTURE)
            env = {'PATH': os.environ['PATH'], 'HOME': tmp, 'PYTHONPATH': tmp,
                   'FIXTURE_ROOT': tmp, 'FIXTURE_OPTIONS': json.dumps(options),
                   'TS_OAUTH_CLIENT_ID': 'fixture-client', 'TS_OAUTH_SECRET': 'fixture-secret',
                   'PHOENIX_RESOURCE_NAME': NAME, 'PHOENIX_TAILSCALE_NODE_ID': node,
                   'PHOENIX_TAILSCALE_TAG': 'tag:openclaw-staging'}
            result = subprocess.run(['bash', str(SCRIPT), '--owned-node' if owned else '--dry-run'],
                                    env=env, text=True, capture_output=True, timeout=10)
            self.assertNotIn('fixture-', result.stdout + result.stderr)
            self.assertTrue((root / 'redirects-blocked').exists(), result.stdout + result.stderr)
            calls = [json.loads(line) for line in (root / 'calls').read_text().splitlines()] if (root / 'calls').exists() else []
            return result, calls

    def test_inventory_never_deletes_and_reports_leftovers(self):
        for devices in ([], [OWN, OTHER]):
            result, calls = self.run_cleaner(devices=devices)
            self.assertEqual(result.returncode, 1 if devices else 0)
            self.assertFalse(any(c['method'] == 'DELETE' for c in calls))
            self.assertEqual(json.loads(result.stdout)['deleted'], 0)

    def test_only_the_proven_node_is_deleted_and_read_back(self):
        result, calls = self.run_cleaner(owned=True, devices=[OWN, OTHER])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([c['method'] for c in calls], ['POST', 'GET', 'DELETE', 'GET'])
        self.assertTrue(json.loads(result.stdout)['owned_node_absent'])

    def test_unrelated_concurrent_changes_do_not_block_cleanup(self):
        result, calls = self.run_cleaner(owned=True, devices=[OWN, OTHER], after=[
            {'id': '99', 'nodeId': 'nNew', 'hostname': 'unrelated-new-device'}])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([c['method'] for c in calls], ['POST', 'GET', 'DELETE', 'GET'])

    def test_an_already_absent_owned_node_needs_no_delete(self):
        # A run whose deploy failed never proved a node (empty ID); it still
        # has to confirm that no device carries its run name.
        for node in ('nOwned', ''):
            with self.subTest(node=node):
                result, calls = self.run_cleaner(owned=True, node=node, devices=[OTHER])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(any(c['method'] == 'DELETE' for c in calls))
                self.assertTrue(json.loads(result.stdout)['owned_node_absent'])

    def test_a_name_or_offline_timestamp_is_not_ownership(self):
        for node, devices in (('', [OWN]), ('nDifferent', [OWN]),
                              ('nOwned', [OWN | {'hostname': OTHER['hostname']}])):
            with self.subTest(node=node, devices=devices):
                result, calls = self.run_cleaner(owned=True, node=node, devices=devices)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(c['method'] == 'DELETE' for c in calls))

    def test_the_exclusive_run_tag_proves_ownership_when_no_node_id_was_proven(self):
        # Deploy failed after the server joined: no node ID, but the device carries
        # this run's unique name and the tag only the CI OAuth client can mint.
        tagged = OWN | {'tags': ['tag:openclaw-staging']}
        result, calls = self.run_cleaner(owned=True, node='', devices=[tagged, OTHER])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([c['method'] for c in calls], ['POST', 'GET', 'DELETE', 'GET'])
        self.assertTrue(json.loads(result.stdout)['owned_node_absent'])
        for devices in ([OWN | {'tags': ['tag:server']}], [OWN | {'tags': ['tag:openclaw-staging', 'tag:ci']}],
                        [OWN | {'tags': 'tag:openclaw-staging'}], [tagged, tagged | {'id': '44', 'nodeId': 'nTwin'}]):
            with self.subTest(devices=devices):
                result, calls = self.run_cleaner(owned=True, node='', devices=devices)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(c['method'] == 'DELETE' for c in calls))

    def test_missing_node_ids_cannot_prove_owned_node_absence(self):
        renamed = {'id': OWN['id'], 'hostname': 'renamed-device'}
        result, calls = self.run_cleaner(owned=True, devices=[renamed, OTHER])
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('owned_node_absent', result.stdout)
        self.assertFalse(any(c['method'] == 'DELETE' for c in calls))
        result, calls = self.run_cleaner(owned=True, devices=[OWN, OTHER], after=[renamed])
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('owned_node_absent', result.stdout)

    def test_deletion_and_readback_failures_are_not_success(self):
        for options in ({'delete_error': True}, {'readback_error': True}, {'retained': True}):
            with self.subTest(options=options):
                result, calls = self.run_cleaner(owned=True, devices=[OWN, OTHER], **options)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls[-1]['method'], 'GET')

    def test_missing_or_malformed_inventory_is_not_empty_inventory(self):
        for options in ({'read_error': True}, {'raw': ''}, {'raw': '{}'}, {'raw': '[]'},
                        {'raw': '{"devices":null}'}, {'devices': [{'id': '', 'hostname': NAME}]},
                        {'devices': [{'id': 42, 'hostname': NAME}]}):
            with self.subTest(options=options):
                result, calls = self.run_cleaner(**options)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(c['method'] == 'DELETE' for c in calls))


if __name__ == '__main__':
    unittest.main()
