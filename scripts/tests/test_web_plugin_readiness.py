#!/usr/bin/env python3
"""Exercise plugin provisioning against a real HTTP startup probe and a fake CLI."""
import http.server
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / 'ansible/roles/config/tasks/web-plugin.yml'
FAKE_OPENCLAW = '''#!/usr/bin/env python3
import json, os, pathlib, sys
root = pathlib.Path(os.environ['FIXTURE_ROOT'])
args = sys.argv[1:]
with (root / 'calls').open('a') as f:
    f.write(json.dumps(args) + '\\n')
if not (root / 'started').exists():
    sys.exit('Gateway not reachable: startup has not completed')
if args == ['plugins', 'list', '--json']:
    print(json.dumps({'plugins': [{'id': 'exa', 'version': (root / 'version').read_text()}]}))
elif args == ['plugins', 'install', '--pin', '--force', '@openclaw/exa-plugin@2026.9.9']:
    (root / 'version').write_text('2026.9.9')
else:
    sys.exit('Unexpected CLI call: ' + repr(args))
'''


class WebPluginReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ansible = shutil.which('ansible-playbook')
        if not cls.ansible:
            raise RuntimeError("Install the project's Ansible development dependency first")
        python = shlex.split(Path(cls.ansible).read_text().splitlines()[0].removeprefix('#!'))
        cls.tasks = json.loads(subprocess.run(
            [*python, '-c', 'import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))', str(TASKS)],
            capture_output=True, text=True, check=True).stdout)

    def run_fixture(self, root, *, pending=0, malformed=False, passes=1):
        requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                started = len(requests) > pending and not malformed
                if started:
                    (root / 'started').touch()
                body = json.dumps({'ok': started, 'status': 'started' if started else 'starting'}).encode()
                self.send_response(200 if started or malformed else 503)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        with http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                tasks = json.loads(json.dumps(self.tasks).replace(
                    'http://127.0.0.1:18789', f'http://127.0.0.1:{server.server_port}'))
                for task in tasks:
                    task.pop('notify', None)
                    if 'ansible.builtin.uri' in task:
                        # Keep production's bounded policy, but shorten its wall time here.
                        self.assertEqual(task['retries'], 30)
                        self.assertEqual(task['delay'], 5)
                        self.assertEqual(task['ansible.builtin.uri']['timeout'], 5)
                        task['retries'] = 2
                        task['delay'] = 0
                        task['ansible.builtin.uri']['timeout'] = 1
                play = [{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                         'vars': {'web_plugin': 'exa', 'openclaw_version': '2026.9.9'}, 'tasks': tasks}]
                (root / 'play.json').write_text(json.dumps(play))
                (root / 'bin').mkdir()
                cli = root / 'bin/openclaw'
                cli.write_text(FAKE_OPENCLAW)
                cli.chmod(0o700)
                env = {'PATH': f"{root / 'bin'}:{os.environ['PATH']}", 'HOME': str(root),
                       'FIXTURE_ROOT': str(root), 'ANSIBLE_NOCOLOR': '1',
                       'ANSIBLE_LOCAL_TEMP': str(root / 'local'), 'ANSIBLE_REMOTE_TEMP': str(root / 'remote')}
                results = [subprocess.run([self.ansible, '-i', 'localhost,', str(root / 'play.json')],
                                          env=env, capture_output=True, text=True, timeout=30)
                           for _ in range(passes)]
            finally:
                server.shutdown()
                thread.join(timeout=5)
        calls = [json.loads(line) for line in (root / 'calls').read_text().splitlines()] if (root / 'calls').exists() else []
        return results, requests, calls

    def test_inventory_waits_for_startup_not_just_a_listening_port(self):
        with tempfile.TemporaryDirectory(prefix='web-plugin-startup-') as tmp:
            root = Path(tmp)
            (root / 'version').write_text('2026.9.9')
            results, requests, calls = self.run_fixture(root, pending=2)
            self.assertEqual(results[0].returncode, 0, results[0].stdout + results[0].stderr)
            self.assertEqual(requests, ['/startupz'] * 3)
            self.assertEqual(calls, [['plugins', 'list', '--json']])

    def test_pending_startup_exhausts_the_bound_without_querying_plugins(self):
        with tempfile.TemporaryDirectory(prefix='web-plugin-startup-') as tmp:
            root = Path(tmp)
            (root / 'version').write_text('2026.9.9')
            results, requests, calls = self.run_fixture(root, pending=100)
            self.assertNotEqual(results[0].returncode, 0)
            self.assertEqual(requests, ['/startupz'] * 3)
            self.assertEqual(calls, [])

    def test_http_success_without_started_state_does_not_admit_inventory(self):
        with tempfile.TemporaryDirectory(prefix='web-plugin-startup-') as tmp:
            root = Path(tmp)
            (root / 'version').write_text('2026.9.9')
            results, requests, calls = self.run_fixture(root, malformed=True)
            self.assertNotEqual(results[0].returncode, 0)
            self.assertEqual(requests, ['/startupz'] * 3)
            self.assertEqual(calls, [])

    def test_started_gateway_installs_once_and_second_pass_converges(self):
        with tempfile.TemporaryDirectory(prefix='web-plugin-startup-') as tmp:
            root = Path(tmp)
            (root / 'version').write_text('2026.9.8')
            results, requests, calls = self.run_fixture(root, passes=2)
            for result in results:
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(requests, ['/startupz'] * 2)
            self.assertEqual(calls, [['plugins', 'list', '--json'],
                                     ['plugins', 'install', '--pin', '--force', '@openclaw/exa-plugin@2026.9.9'],
                                     ['plugins', 'list', '--json']])
            self.assertIn('changed=0', results[1].stdout)


if __name__ == '__main__':
    unittest.main()
