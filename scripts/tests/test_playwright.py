"""Playwright browsers: addresses, settings checks, the home proxy policy and the deploy check."""
import hashlib
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK = ROOT / 'ansible/playbook.yml'
ALL_VARS = ROOT / 'ansible/group_vars/all.yml'
PROXY_SCRIPT = ROOT / 'scripts/deploy-mac-browser-proxy.sh'
CHECK = ROOT / 'ansible/roles/playwright/files/playwright-check.py'
DERIVATION = [
    'Use manually defined MCP servers (override)',
    'Initialize auto-generated MCP server list',
    'Auto-generate MCP servers from server types × agents',
    'Validate supported MCP server definitions before provisioning',
    'Validate Playwright browser settings',
    'Initialize Playwright browser list',
    'Derive Playwright browser networks and addresses',
    'Require one address range per Playwright browser',
    'Require the browser servers to match the per-agent deny rules',
]
RECONCILE = ROOT / 'ansible/roles/playwright/files/playwright-reconcile.sh'
PROXY = {'host': '100.100.1.1', 'port': 3128, 'client': '100.100.2.2'}


def expected_browser(agent_id, slot=None):
    # Independent re-implementation of the derivation: /29 picked by sha256(id) in 172.31.0.0/16.
    slot = int(hashlib.sha256(agent_id.encode()).hexdigest()[:8], 16) % 8192 if slot is None else slot
    offset = slot * 8
    net, low = f'172.31.{offset // 256}', offset % 256
    return {'agent_id': agent_id, 'server_name': 'playwright' if agent_id == 'main' else f'playwright-{agent_id}',
            'network': f'openclaw-playwright-net-{agent_id}', 'subnet': f'{net}.{low}/29',
            'browser_ip': f'{net}.{low + 2}', 'relay_ip': f'{net}.{low + 3}', 'url': f'http://{net}.{low + 2}:8931/mcp'}


class DerivationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ansible = shutil.which('ansible-playbook')
        if not cls.ansible:
            raise RuntimeError('Install the project Ansible dependency')
        python = shlex.split(Path(cls.ansible).read_text().splitlines()[0].removeprefix('#!'))
        plays, cls.defaults = json.loads(subprocess.check_output([*python, '-c',
            'import json,sys,yaml; print(json.dumps([yaml.safe_load(open(p)) for p in sys.argv[1:]]))',
            str(PLAYBOOK), str(ALL_VARS)], text=True))
        pre_tasks = next(play for play in plays if play.get('roles'))['pre_tasks']
        cls.tasks = [task for name in DERIVATION for task in pre_tasks if task.get('name') == name]
        assert len(cls.tasks) == len(DERIVATION), 'a derivation task was renamed'

    def derive(self, **overrides):
        with tempfile.TemporaryDirectory(prefix='playwright-derive-') as directory:
            root = Path(directory)
            out = root / 'facts.json'
            variables = {**self.defaults, 'openclaw_agents': [{'id': 'main'}, {'id': 'cama'}],
                         'openclaw_playwright_enabled': True, 'openclaw_playwright_proxy': PROXY, **overrides}
            dump = {'ansible.builtin.copy': {'dest': str(out), 'content':
                    "{{ {'browsers': _openclaw_playwright, 'servers': _openclaw_mcp_servers} | to_json }}"}}
            play = root / 'play.json'
            play.write_text(json.dumps([{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                                         'vars': variables, 'tasks': [*self.tasks, dump]}]))
            env = {**os.environ, 'ANSIBLE_NOCOLOR': '1', 'ANSIBLE_LOCAL_TEMP': str(root / 'local')}
            result = subprocess.run([self.ansible, '-i', 'localhost,', str(play)], env=env,
                                    capture_output=True, text=True, timeout=120)
            return result, (json.loads(out.read_text()) if out.exists() else None)

    def test_each_agent_gets_its_own_network_and_server(self):
        result, facts = self.derive()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(facts['browsers'], [expected_browser('main'), expected_browser('cama')])
        # Same naming as the other per-agent servers, so the deny rules scope them per agent.
        playwright = [s for s in facts['servers'] if s['type'] == 'playwright']
        self.assertEqual([(s['name'], s['agent_id']) for s in playwright],
                         [('playwright', 'main'), ('playwright-cama', 'cama')])
        self.assertTrue(all('_' not in s['name'] for s in facts['servers']))

    def test_reordering_or_adding_agents_never_moves_an_address(self):
        # Positional addresses would hand main's browser address to another agent
        # while the gateway still routes main there.
        _, before = self.derive()
        result, after = self.derive(openclaw_agents=[{'id': 'nici'}, {'id': 'cama'}, {'id': 'main'}])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        by_id = lambda facts: {b['agent_id']: b for b in facts['browsers']}
        self.assertEqual(by_id(after)['main'], by_id(before)['main'])
        self.assertEqual(by_id(after)['cama'], by_id(before)['cama'])

    def test_a_hash_clash_stops_provisioning_until_a_slot_is_set(self):
        clash = expected_browser('main')['subnet']
        slot = (int(clash.split('.')[2]) * 256 + int(clash.split('.')[3].split('/')[0])) // 8
        result, facts = self.derive(openclaw_agents=[{'id': 'main'}, {'id': 'cama', 'playwright_slot': slot}])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Set playwright_slot', result.stdout)
        result, facts = self.derive(openclaw_agents=[{'id': 'main'}, {'id': 'cama', 'playwright_slot': 7}])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(facts['browsers'][1], expected_browser('cama', slot=7))

    def test_deny_rules_are_written_with_the_agents_and_checked_before_registration(self):
        # A new agent must never run without its deny rules, not even when a later
        # role fails (round-5 review); registration must not proceed without them.
        python = shlex.split(Path(self.ansible).read_text().splitlines()[0].removeprefix('#!'))
        load = lambda path: json.loads(subprocess.check_output([*python, '-c',
            'import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))', str(path)], text=True))
        roles = ROOT / 'ansible/roles'
        agents = [t.get('name') for t in load(roles / 'agents/tasks/main.yml')]
        plugins = [t.get('name') for t in load(roles / 'plugins/tasks/main.yml')]
        writer, check = 'Set per-agent tool deny rules for MCP tools', "Require every agent's MCP deny rules before registering servers"
        self.assertLess(agents.index('Refresh agent list for config targeting'), agents.index(writer))
        self.assertNotIn(writer, plugins)
        self.assertLess(plugins.index(check), plugins.index('Configure mcp-adapter plugin'))
        self.assertGreater(plugins.index('Drop MCP deny rules that registration made obsolete'),
                           plugins.index('Configure mcp-adapter plugin'))

    def test_disabled_registers_nothing(self):
        result, facts = self.derive(openclaw_playwright_enabled=False, openclaw_playwright_proxy={'host': '', 'port': 3128, 'client': ''})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(facts['browsers'], [])
        self.assertFalse([s for s in facts['servers'] if s['type'] == 'playwright'])

    def test_missing_or_malformed_proxy_settings_stop_provisioning(self):
        for proxy in ({**PROXY, 'host': ''}, {**PROXY, 'client': 'mac-studio'}, {**PROXY, 'host': '300.1.1.1'}):
            with self.subTest(proxy=proxy):
                result, facts = self.derive(openclaw_playwright_proxy=proxy)
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(facts)
                self.assertIn('openclaw_playwright_proxy.host', result.stdout)

    def test_manual_server_names_must_match_the_registered_browsers(self):
        # Registration uses the derived names, deny rules the manual ones: a mismatch
        # would let one agent use another agent's browser.
        servers = [{'name': 'browser-main', 'type': 'playwright', 'agent_id': 'main'},
                   {'name': 'browser-cama', 'type': 'playwright', 'agent_id': 'cama'}]
        result, facts = self.derive(openclaw_mcp_servers=servers)
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(facts)
        self.assertIn('exactly one', result.stdout)

    def test_subnet_base_must_be_a_whole_16(self):
        result, facts = self.derive(openclaw_playwright_subnet_base='172.31.240.0')
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(facts)


class HomeProxyPolicyTest(unittest.TestCase):
    def render(self):
        env = {**os.environ, 'BROWSER_PROXY_HOST': PROXY['host'], 'BROWSER_PROXY_PORT': '3128',
               'BROWSER_PROXY_CLIENT': PROXY['client'], 'CONF_DIR': '/conf', 'LOG_DIR': '/logs'}
        return subprocess.check_output(['bash', str(PROXY_SCRIPT), '--print-config'], env=env, text=True)

    def test_serves_only_the_server_and_refuses_private_destinations_first(self):
        config = self.render()
        lines = [line for line in config.splitlines() if line.startswith('http_access')]
        self.assertEqual(lines, [
            'http_access deny !openclaw_server', 'http_access deny !safe_ports',
            'http_access deny CONNECT !tls_port', 'http_access deny blocked_dst',
            'http_access allow openclaw_server', 'http_access deny all'])
        self.assertIn('http_port 100.100.1.1:3128', config)
        self.assertIn('acl openclaw_server src 100.100.2.2/32', config)
        blocked = ' '.join(line for line in config.splitlines() if line.startswith('acl blocked_dst'))
        for cidr in ('10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16', '172.16.0.0/12',
                     '192.168.0.0/16', '::1/128', '64:ff9b::/96', 'fc00::/7', 'fe80::/10', '"/conf/home-ipv6.acl"'):
            self.assertIn(cidr, blocked)

    def test_never_lists_ranges_squid_reads_as_all_of_ipv4(self):
        # Seen while building this: squid folds ::ffff:0:0/96 into 0.0.0.0/0, refusing every site.
        config = self.render()
        self.assertNotIn('::ffff:', config)

    def test_rejects_settings_that_are_not_ipv4(self):
        env = {**os.environ, 'BROWSER_PROXY_HOST': 'mac-studio', 'BROWSER_PROXY_PORT': '3128',
               'BROWSER_PROXY_CLIENT': PROXY['client']}
        result = subprocess.run(['bash', str(PROXY_SCRIPT), '--print-config'], env=env,
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be this Mac's tailnet IPv4", result.stderr)


class FakeBrowser(BaseHTTPRequestHandler):
    """Answers the MCP calls playwright-check.py makes; `pages` maps URL to page text.
    A page starting with "!" is a tool error (e.g. a certificate rejection)."""
    pages, calls = {}, []
    current = None

    def log_message(self, *args):
        pass

    def do_GET(self):  # the IP echo, fetched directly by the check
        self.reply(b'203.0.113.9', 'text/plain')

    def do_POST(self):
        message = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        FakeBrowser.calls.append(message.get('params', {}).get('name', message['method']))
        if 'id' not in message:
            self.send_response(202)
            self.end_headers()
            return
        params = message.get('params', {})
        text = ''
        if params.get('name') == 'browser_navigate':
            FakeBrowser.current = params['arguments']['url']
            text = FakeBrowser.pages.get(FakeBrowser.current, '')
        elif params.get('name') == 'browser_snapshot':
            text = FakeBrowser.pages.get(FakeBrowser.current, '').lstrip('!')
        failed = text.startswith('!')
        body = {'jsonrpc': '2.0', 'id': message['id'],
                'result': {'content': [{'type': 'text', 'text': text.lstrip('!')}], 'isError': failed}}
        self.reply(('event: message\ndata: ' + json.dumps(body) + '\n\n').encode(), 'text/event-stream')

    def reply(self, body, kind):
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Mcp-Session-Id', 'fake')
        self.end_headers()
        self.wfile.write(body)


class DeployCheckTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('playwright_check', CHECK)
        cls.check = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.check)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), FakeBrowser)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    CERT_REJECTED = '!Error: net::ERR_CERT_AUTHORITY_INVALID at https://dv-g3b-emudhra.we-have-a-problem.com/'

    def run_check(self, echo_page, private_page, distrusted_page=CERT_REJECTED):
        FakeBrowser.calls = []
        FakeBrowser.pages = {self.base + '/echo': echo_page, self.check.PRIVATE: private_page,
                             self.check.DISTRUSTED: distrusted_page}
        with mock.patch.object(self.check, 'ECHO', self.base + '/echo'):
            self.check.main(self.base + '/mcp')

    SQUID_REFUSAL = ('- HTTP status: 403 Forbidden\n- Page Title: ERROR: The requested URL could not be retrieved\n'
                     '- heading "Access Denied." [level=2]')

    def test_passes_when_pages_leave_from_home_and_private_is_refused(self):
        self.run_check('- generic [ref=e1]: 92.0.2.1', self.SQUID_REFUSAL)
        self.assertEqual(FakeBrowser.calls[-1], 'browser_close')

    def test_fails_when_the_browser_leaves_from_this_server(self):
        with self.assertRaises(SystemExit) as failure:
            self.run_check('- generic [ref=e1]: 203.0.113.9', self.SQUID_REFUSAL)
        self.assertIn('not through the home proxy', str(failure.exception))
        self.assertEqual(FakeBrowser.calls[-1], 'browser_close')

    def test_fails_when_the_proxy_does_not_refuse_private_addresses(self):
        for private in ('- Page Title: Router login',
                        # Page text mentioning 403 is not a refusal.
                        '- HTTP status: 200 OK\n- Page Title: Router login / Firmware 403',
                        # A device's own 403 means the proxy let the request through.
                        '- HTTP status: 403 Forbidden\n- Page Title: Home router access denied'):
            with self.subTest(private=private), self.assertRaises(SystemExit) as failure:
                self.run_check('- generic [ref=e1]: 92.0.2.1', private)
            self.assertIn('did not refuse', str(failure.exception))

    def test_fails_when_chromium_still_trusts_the_compromised_chain(self):
        for distrusted in ('- Page Title: eMudhra test site', '!Error: net::ERR_PROXY_CONNECTION_FAILED'):
            with self.subTest(distrusted=distrusted), self.assertRaises(SystemExit) as failure:
                self.run_check('- generic [ref=e1]: 92.0.2.1', self.SQUID_REFUSAL, distrusted)
            self.assertIn('distrust policy is not loaded', str(failure.exception))


class DenyWriterTest(unittest.TestCase):
    SCRIPT = ROOT / 'ansible/files/openclaw-mcp-deny.sh'

    def write(self, agent, desired, registered, current, owners=None, tighten=False):
        """current=None: a just-created agent without any deny rules yet."""
        with tempfile.TemporaryDirectory(prefix='mcp-deny-') as directory:
            root = Path(directory)
            config = root / 'openclaw.json'
            config.write_text(json.dumps({
                'agents': {'entries': {agent: {} if current is None else {'tools': {'deny': current}}}},
                'plugins': {'entries': {'openclaw-mcp-adapter': {'config': {'servers': [{'name': n} for n in registered]}}}}}))
            (root / 'openclaw').write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" > "$FAKE_ROOT/set"\n')
            (root / 'openclaw').chmod(0o755)
            env = {**os.environ, 'PATH': f'{root}:{os.environ["PATH"]}', 'FAKE_ROOT': str(root)}
            result = subprocess.run(['bash', str(self.SCRIPT), *(['--tighten'] if tighten else []),
                                     str(config), agent, json.dumps(desired),
                                     json.dumps(owners or {})],
                                    env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            written = (root / 'set').read_text().split(' --json ', 1)[1].strip() if (root / 'set').exists() else None
            return result.stdout.strip(), json.loads(written) if written else None

    def test_still_registered_servers_of_others_stay_denied(self):
        # Browsers switched off in config but still registered: keep denying bob's.
        out, written = self.write('main', ['github-bob_*'], ['github', 'github-bob', 'playwright', 'playwright-bob'],
                                  ['github-bob_*', 'playwright-bob_*'])
        self.assertEqual(out, 'OK')
        self.assertIsNone(written)

    def test_new_server_types_are_denied_before_registration_and_own_servers_never(self):
        out, written = self.write('cama', ['github_*', 'playwright_*'], ['github', 'github-cama'], [])
        self.assertEqual(out, 'UPDATED')
        self.assertEqual(written, ['github_*', 'playwright_*'])

    def test_declared_owners_win_over_the_naming_rule(self):
        # A manual definition gives main a server called github-work (round-7 review).
        out, written = self.write('main', ['github-bob_*'], ['github-work', 'github-bob'], [],
                                  owners={'github-work': 'main', 'github-bob': 'bob'})
        self.assertEqual(written, ['github-bob_*'])

    def test_an_ownership_change_relaxes_only_after_registration(self):
        # github-work moves from main to bob; its registration still carries main's token
        # until the plugins role re-registers it (round-8 review).
        args = ('bob', [], ['github-work'], ['github-work_*'], {'github-work': 'bob'})
        out, written = self.write(*args, tighten=True)
        self.assertEqual(out, 'OK')
        out, written = self.write(*args)
        self.assertEqual(written, [])

    def test_a_new_agent_gets_a_reassigned_server_only_after_registration(self):
        # Round-9 review: github-work moves from main to the brand-new bob.
        args = ('bob', ['qmd_*'], ['github-work', 'qmd', 'github-bob'], None, {'github-work': 'bob', 'github-bob': 'bob'})
        out, written = self.write(*args, tighten=True)
        self.assertEqual(written, ['github-work_*', 'qmd_*'])
        out, written = self.write(*args)
        self.assertEqual(written, ['qmd_*'])

    def test_manually_named_own_servers_stay_usable_for_existing_agents(self):
        # No flapping: main keeps github-work on every tightening run.
        out, written = self.write('main', [], ['github-work'], [], {'github-work': 'main'}, tighten=True)
        self.assertEqual(out, 'OK')

    def test_tightening_still_drops_rules_for_servers_no_longer_registered(self):
        out, written = self.write('bob', [], ['github'], ['github_*', 'playwright-cama_*'], tighten=True)
        self.assertEqual(written, ['github_*'])

    def test_agent_ids_with_dashes_own_their_servers(self):
        out, written = self.write('team-a', [], ['github', 'github-team-a', 'qmd-team-b'], [])
        self.assertEqual(written, ['github_*', 'qmd-team-b_*'])


class DistrustPolicyTest(unittest.TestCase):
    def test_policy_lists_exactly_the_distrusted_roots(self):
        files = ROOT / 'ansible/roles/playwright/files'
        pems = sorted((files / 'distrust').glob('*.pem'))
        self.assertEqual(len(pems), 2)
        expected = [__import__('base64').b64encode(subprocess.check_output(
            ['openssl', 'x509', '-in', str(pem), '-outform', 'DER'])).decode() for pem in pems]
        policy = json.loads((files / 'chromium-policy.json').read_text())
        self.assertEqual(policy, {'CADistrustedCertificates': expected})

    def test_policy_lands_where_playwrights_chrome_for_testing_reads_it(self):
        # Measured in the pinned image: the binary only knows /etc/opt/chrome_for_testing/policies.
        dockerfile = (ROOT / 'ansible/roles/playwright/templates/Dockerfile.browser.j2').read_text()
        self.assertIn('/etc/opt/chrome_for_testing/policies/managed/', dockerfile)


FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
state_file = os.path.join(os.environ['FAKE_STATE'], 'docker.json')
state = json.load(open(state_file))
args = sys.argv[1:]
log = open(os.path.join(os.environ['FAKE_STATE'], 'calls'), 'a')
log.write('docker ' + ' '.join(args) + '\n')
if args[:2] == ['network', 'ls']:
    print('\n'.join(state['networks']))
elif args[:2] == ['network', 'inspect']:
    name = args[-1]
    if name not in state['networks']:
        sys.exit(1)
    print(state['networks'][name])
elif args[:2] == ['network', 'rm']:
    del state['networks'][args[2]]
elif args[:2] == ['network', 'disconnect'] or args[:2] == ['rm', '-f']:
    pass
else:
    sys.exit('unexpected docker call: %s' % args)
json.dump(state, open(state_file, 'w'))
'''


def reconcile_entry(agent_id, subnet):
    """<id>=<server>=<subnet>=<browser ip>, as the role passes it; the browser is .2 of the /29."""
    network, low = subnet.split('/')[0].rsplit('.', 1)
    server = 'playwright' if agent_id == 'main' else f'playwright-{agent_id}'
    return f'{agent_id}={server}={subnet}={network}.{int(low) + 2}'


class ReconcileTest(unittest.TestCase):
    def run_reconcile(self, units, networks, *wanted, expect_failure=False, routes=None):
        with tempfile.TemporaryDirectory(prefix='playwright-reconcile-') as directory:
            root = Path(directory)
            unit_dir, bin_dir = root / 'units', root / 'bin'
            unit_dir.mkdir(); bin_dir.mkdir()
            for unit in units:
                (unit_dir / unit).write_text('[Unit]\n')
            (root / 'docker.json').write_text(json.dumps({'networks': networks}))
            config = root / 'openclaw.json'
            if routes is not None:
                config.write_text(json.dumps({'plugins': {'entries': {'openclaw-mcp-adapter': {'config': {'servers': [
                    {'name': 'github', 'transport': 'stdio'}] + [
                    {'name': name, 'transport': 'http', 'url': url} for name, url in routes.items()]}}}}}))
            (bin_dir / 'docker').write_text(FAKE_DOCKER)
            (bin_dir / 'systemctl').write_text('#!/bin/sh\necho "systemctl $*" >> "$FAKE_STATE/calls"\n')
            for tool in ('docker', 'systemctl'):
                (bin_dir / tool).chmod(0o755)
            env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}', 'FAKE_STATE': str(root)}
            wanted = [reconcile_entry(*w.split('=')) for w in wanted]
            result = subprocess.run(['bash', str(RECONCILE), str(unit_dir), 'openclaw-playwright-egress', str(config), *wanted],
                                    env=env, capture_output=True, text=True, timeout=30)
            calls = (root / 'calls').read_text() if (root / 'calls').exists() else ''
            if expect_failure:
                self.assertNotEqual(result.returncode, 0)
                result.calls = calls
                return result
            self.assertEqual(result.returncode, 0, result.stderr)
            return (result.stdout, sorted(p.name for p in unit_dir.iterdir()),
                    json.loads((root / 'docker.json').read_text())['networks'], calls)

    UNITS = ['openclaw-playwright-relay.service', 'openclaw-playwright-browser-main.service',
             'openclaw-playwright-browser-cama.service']
    NETWORKS = {'openclaw-playwright-egress': '172.20.0.0/16', 'openclaw-playwright-net-main': '172.31.240.0/29',
                'openclaw-playwright-net-cama': '172.31.240.8/29', 'other-network': '10.9.0.0/16'}

    def test_unchanged_config_touches_nothing(self):
        out, units, networks, calls = self.run_reconcile(
            self.UNITS, self.NETWORKS, 'main=172.31.240.0/29', 'cama=172.31.240.8/29')
        self.assertEqual(out, '')
        self.assertEqual(units, sorted(self.UNITS))
        self.assertEqual(networks, self.NETWORKS)
        self.assertNotIn('systemctl', calls)

    def test_removed_agent_loses_unit_container_and_network(self):
        out, units, networks, calls = self.run_reconcile(self.UNITS, self.NETWORKS, 'main=172.31.240.0/29')
        self.assertIn('CHANGED', out)
        self.assertNotIn('openclaw-playwright-browser-cama.service', units)
        self.assertNotIn('openclaw-playwright-net-cama', networks)
        self.assertIn('docker rm -f openclaw-playwright-browser-cama', calls)
        self.assertIn('other-network', networks)

    def test_a_subnet_another_agent_still_holds_is_refused(self):
        # The gateway routes main to its old address until the plugins role runs, so
        # cama must not get it: main could download into cama's container.
        result = self.run_reconcile(self.UNITS, self.NETWORKS, 'cama=172.31.240.0/29', 'main=172.31.240.8/29',
                                    expect_failure=True)
        self.assertIn('172.31.240.0/29 still belongs to agent main', result.stderr)
        self.assertNotIn('network rm', result.calls)

    def test_an_address_the_gateway_still_routes_for_another_agent_is_refused(self):
        # A failed run moved cama away but never re-registered it: the gateway still
        # sends cama's browser tools to .10. A new agent must not get that address.
        result = self.run_reconcile(self.UNITS, {'openclaw-playwright-egress': '172.20.0.0/16'},
                                    'main=172.31.240.0/29', 'nici=172.31.240.8/29', expect_failure=True,
                                    routes={'playwright': 'http://172.31.240.2:8931/mcp',
                                            'playwright-cama': 'http://172.31.240.10:8931/mcp'})
        self.assertIn("172.31.240.10 is still the gateway's route for playwright-cama", result.stderr)
        self.assertNotIn('network rm', result.calls)

    def test_routes_of_the_same_agent_pass(self):
        out, units, networks, calls = self.run_reconcile(
            self.UNITS, self.NETWORKS, 'main=172.31.240.0/29', 'cama=172.31.240.8/29',
            routes={'playwright': 'http://172.31.240.2:8931/mcp', 'playwright-cama': 'http://172.31.240.10:8931/mcp'})
        self.assertEqual(out, '')

    def test_a_moved_slot_frees_the_old_subnet_but_keeps_the_unit(self):
        out, units, networks, calls = self.run_reconcile(
            self.UNITS, self.NETWORKS, 'main=172.31.240.0/29', 'cama=172.31.9.0/29')
        self.assertIn('CHANGED', out)
        self.assertEqual(units, sorted(self.UNITS))
        self.assertNotIn('openclaw-playwright-net-cama', networks)
        self.assertIn('openclaw-playwright-net-main', networks)
        self.assertIn('docker rm -f openclaw-playwright-browser-cama', calls)

    def test_disabled_removes_everything_including_the_relay(self):
        out, units, networks, calls = self.run_reconcile(self.UNITS, self.NETWORKS)
        self.assertIn('CHANGED', out)
        self.assertEqual(units, [])
        self.assertEqual(networks, {'other-network': '10.9.0.0/16'})

    def test_an_agent_named_like_infrastructure_cannot_collide(self):
        # Browsers, networks and the relay live in separate name spaces.
        units = ['openclaw-playwright-relay.service', 'openclaw-playwright-browser-relay.service']
        out, kept, networks, calls = self.run_reconcile(
            units, {'openclaw-playwright-egress': '172.20.0.0/16', 'openclaw-playwright-net-relay': '172.31.240.0/29'},
            'relay=172.31.240.0/29')
        self.assertEqual(out, '')
        self.assertEqual(kept, sorted(units))


if __name__ == '__main__':
    unittest.main()
