"""Exercise the actual Node HTTP proxy without production credentials."""
import contextlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / 'ansible/roles/plugins/templates/mcp-auth-proxy.js.j2'


@contextlib.contextmanager
def running_proxy(tokens, target=None):
    """Start the actual proxy template; yield its base URL and a direct opener."""
    node = shutil.which('node')
    if node is None:
        raise AssertionError('Install Node to test the actual proxy')
    root = tokens.parent
    source = root / 'proxy.js'
    source.write_text(TEMPLATE.read_text().replace('{{ openclaw_mcp_adapter.codex_proxy_port }}', '8787'))
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    env = {**os.environ, 'CODEX_PROXY_LISTEN': '127.0.0.1',
           'CODEX_PROXY_PORT': str(port), 'GITHUB_TOKENS_DIR': str(tokens)}
    env.pop('OPENCLAW_CLI_MCP_TARGET', None)
    if target:
        env['OPENCLAW_CLI_MCP_TARGET'] = str(target)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = f'http://127.0.0.1:{port}'
    with (root / 'node.log').open('w') as log:
        process = subprocess.Popen([node, str(source)], env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 5
            while True:
                try:
                    opener.open(f'{base}/health', timeout=1).close()
                    break
                except urllib.error.HTTPError as error:
                    error.close()
                    break
                except urllib.error.URLError:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise AssertionError('Proxy did not become reachable')
                    time.sleep(0.05)
            yield base, opener
        finally:
            process.terminate()
            process.wait(timeout=7)


def status(opener, url):
    try:
        with opener.open(url, timeout=2) as response:
            return response.code, json.load(response)
    except urllib.error.HTTPError as error:
        with error:
            return error.code, json.load(error)


class ProxyHealthTest(unittest.TestCase):
    def check_health(self, relay, pat):
        with tempfile.TemporaryDirectory(prefix='mcp-proxy-') as directory:
            root = Path(directory)
            tokens = root / 'tokens'
            tokens.mkdir()
            if pat:
                (tokens / 'fixture-agent').write_text('synthetic-noncredential')
            # A target is generated for each gateway invocation. Process
            # health must not depend on a live invocation existing yet.
            with running_proxy(tokens, root / 'target.json' if relay else None) as (base, opener):
                code, body = status(opener, f'{base}/health')
                self.assertEqual(code, 200 if relay or pat else 503)
                self.assertEqual(body['github'], {'agents': ['fixture-agent'] if pat else [],
                                                  'hasTokens': pat, 'readable': True})
                self.assertEqual(body['mcp']['configured'], relay)
                self.assertEqual(body['status'], 'healthy' if relay or pat else 'unhealthy')
                if relay:
                    self.assertEqual(status(opener, f'{base}/openclaw/mcp')[0], 503)

    def test_relay_only_is_healthy_without_optional_pats(self):
        self.check_health(relay=True, pat=False)

    def test_pat_only_health_remains_valid(self):
        self.check_health(relay=False, pat=True)

    def test_both_services_are_healthy(self):
        self.check_health(relay=True, pat=True)

    def test_no_configured_service_remains_unhealthy(self):
        self.check_health(relay=False, pat=False)


class GitHubTokenFailureTest(unittest.TestCase):
    """Only a missing token is an unknown agent; a broken token store is a server error."""

    def test_token_read_failures_are_server_errors(self):
        with tempfile.TemporaryDirectory(prefix='mcp-proxy-') as directory:
            tokens = Path(directory) / 'tokens'
            tokens.mkdir()
            (tokens / 'directory-agent').mkdir()  # EISDIR on read
            unreadable = tokens / 'unreadable-agent'
            unreadable.write_text('synthetic-noncredential')
            unreadable.chmod(0)
            with running_proxy(tokens) as (base, opener):
                path = '/owner/repo.git/info/refs?service=git-upload-pack'
                self.assertEqual(status(opener, f'{base}/github-missing-agent{path}'),
                                 (404, {'error': 'no_token', 'detail': "No GitHub token for agent 'missing-agent'"}))
                for agent in ['directory-agent'] + ([] if os.geteuid() == 0 else ['unreadable-agent']):
                    with self.subTest(agent=agent):
                        self.assertEqual(status(opener, f'{base}/github-{agent}{path}'),
                                         (500, {'error': 'token_unavailable', 'detail': 'GitHub token could not be read'}))
                for agent in ('.', '..', '.hidden'):
                    with self.subTest(agent=agent):
                        self.assertEqual(status(opener, f'{base}/github-{agent}{path}')[0], 400)
            log = (Path(directory) / 'node.log').read_text()
            self.assertNotIn('synthetic-noncredential', log)
            self.assertIn('"code":"EISDIR"', log)

    def test_unreadable_token_store_is_unhealthy_even_with_relay(self):
        with tempfile.TemporaryDirectory(prefix='mcp-proxy-') as directory:
            root = Path(directory)
            tokens = root / 'tokens'
            tokens.write_text('not a directory')  # ENOTDIR on readdir, also as root
            with running_proxy(tokens, root / 'target.json') as (base, opener):
                code, body = status(opener, f'{base}/health')
            self.assertEqual(code, 503)
            self.assertEqual(body['status'], 'unhealthy')
            self.assertEqual(body['github'], {'agents': [], 'hasTokens': False, 'readable': False})
            self.assertTrue(body['mcp']['configured'])
            log = (root / 'node.log').read_text()
            self.assertIn('"code":"ENOTDIR"', log)


if __name__ == '__main__':
    unittest.main()
