#!/usr/bin/env python3
"""Execute Phoenix shell steps with external CLIs replaced by local fixtures.

The ownership conditions are checked as YAML, not emulated as a GitHub runner.
No test contacts GitHub, Tailscale, Hetzner or a Pulumi backend.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest


WORKFLOW = Path(__file__).resolve().parents[2] / '.github/workflows/staging.yml'
NAME = 'openclaw-staging-123456-2'
HOST = NAME + '.example.ts.net'
CLEAN_RECAP = 'PLAY RECAP ********\nserver : ok=42 changed=18 unreachable=0 failed=0 skipped=7 rescued=0 ignored=0\n=== Provisioning complete ===\n'
CLEANUP = ('Remove deploy keys', 'Destroy staging infrastructure', 'Remove owned tailnet device',
           'Verify Hetzner cleanup', 'Remove verified staging stack')
KEY_URN = 'urn:pulumi:staging::openclaw-infra::tls:index/privateKey:PrivateKey::workspace-deploy-key'
BACKEND_ENV = ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'PULUMI_BACKEND_URL', 'PULUMI_CONFIG_PASSPHRASE')
# Staging never receives Obsidian Sync credentials: the role selects the cloud
# vault by agent ID, so a staging run would attach the public staging backup to
# the real vault. Keep this list equal to the workflow's required-input loop.
REQUIRED_INPUTS = ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'PULUMI_BACKEND_URL',
                   'PULUMI_CONFIG_PASSPHRASE', 'HCLOUD_TOKEN', 'GH_TOKEN', 'TS_OAUTH_CLIENT_ID', 'TS_OAUTH_SECRET',
                   'CLAUDE_SETUP_TOKEN', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_USER_ID',
                   'XAI_API_KEY', 'GITHUB_TOKEN_PAT', 'STAGING_PRIVATE_REPOSITORY')
SECRET_CONFIG_KEYS = ('hcloud:token', 'tailscaleAuthKey', 'claudeSetupToken', 'telegramBotToken',
                      'xaiApiKey', 'githubToken', 'githubTokenTest')

# This fixture records argv/stdin separately so a secret in argv cannot go unnoticed.
CLI = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
cmd, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
if cmd == 'ansible-galaxy':
    # Dependency installation must work with only HOME and PATH, not the
    # provisioning credentials or fixture control environment.
    root = pathlib.Path(__file__).resolve().parents[1]
    opts, name = {}, None
else:
    root = pathlib.Path(os.environ['FIXTURE_ROOT'])
    opts = json.loads(os.environ['FIXTURE_OPTIONS'])
    name = os.environ['PHOENIX_RESOURCE_NAME']
stdin = sys.stdin.read() if (cmd == 'pulumi' and '--secret' in args) or (cmd == 'gh' and args[:3] == ['repo', 'deploy-key', 'add']) or (cmd == 'curl' and '@-' in args) else ''
with (root / 'calls.jsonl').open('a') as f:
    f.write(json.dumps({'cmd': cmd, 'args': args, 'stdin': stdin}) + '\n')
if any('fixture-' in arg for arg in args):
    sys.exit('Credential found in command arguments')
if cmd == 'pulumi':
    if args[:2] == ['stack', 'init']:
        sys.exit(opts.get('init_exit', 0))
    if args[:2] == ['stack', 'select']:
        if args[2:] != ['staging', '--non-interactive']: sys.exit('Wrong selected stack')
        sys.exit(opts.get('select_exit', 0))
    if args[:2] == ['config', 'get']:
        values = {'agentIds': 'test', 'claudeSetupToken': 'fixture-claude'}
        if args[2] in values: print(values[args[2]])
        else: sys.exit(1)
        sys.exit(opts.get('config_read_exit', 0) if args[2] == 'claudeSetupToken' else 0)
    if args[:2] == ['config', '--json']:
        config = {'openclaw-infra:agentIds': {'value': 'test'},
                  'openclaw-infra:claudeSetupToken': {'value': 'fixture-claude', 'secret': True}}
        config.update(opts.get('config_values', {}))
        print(opts.get('raw_config_output', json.dumps(config)))
        sys.exit(opts.get('config_read_exit', 0))
    if args[:2] == ['config', 'set']:
        if '--secret' in args and not stdin.startswith('fixture-'):
            sys.exit('Secret was not supplied on stdin')
        sys.exit(opts.get('config_exit', 0))
    if args[0] == 'up':
        if '--target' in args:
            print(opts.get('key_up_log', ''))
            sys.exit(opts.get('key_up_exit', 0))
        # up_sequence answers successive full updates in order (capacity retries).
        attempt = sum(1 for line in (root / 'calls.jsonl').read_text().splitlines()
                      if json.loads(line)['cmd'] == 'pulumi' and json.loads(line)['args'][:1] == ['up']
                      and '--target' not in json.loads(line)['args']) - 1
        sequence = opts.get('up_sequence')
        if sequence is not None:
            if attempt >= len(sequence): sys.exit('Unexpected extra pulumi up')
            print(sequence[attempt]['log'])
            sys.exit(sequence[attempt]['exit'])
        print(opts.get('up_log', ''))
        sys.exit(opts.get('up_exit', 0))
    if args[:2] == ['stack', 'export']:
        base = 'urn:pulumi:staging::openclaw-infra::tls:index/privateKey:PrivateKey::workspace-deploy-key'
        resources = [{'urn': base + suffix, 'type': 'tls:index/privateKey:PrivateKey', 'id': 'key' + suffix}
                     for suffix in ['', '-test']]
        print(opts.get('raw_export_output', json.dumps({'deployment': {'resources': resources}})))
        sys.exit(opts.get('export_exit', 0))
    if args[0] == 'destroy':
        sys.exit(opts.get('destroy_exit', 0))
    if args[:2] == ['stack', 'rm']:
        sys.exit(opts.get('rm_exit', 0))
    if args[:2] == ['stack', 'output']:
        key = args[2]
        if key == '--json':
            outputs = {'openclawGatewayToken': 'fixture-gateway',
                       'tailscaleHostname': opts.get('pulumi_hostname', name),
                       'agentWorkspaceKeys': {'test': {'privateKey': '-----BEGIN OPENSSH PRIVATE KEY-----\nfixture-private-key\n-----END OPENSSH PRIVATE KEY-----'}}}
            outputs.update(opts.get('output_values', {}))
            print(opts.get('raw_stack_output', json.dumps(outputs)))
            sys.exit(opts.get('outputs_read_exit', 0))
        if key == 'openclawGatewayToken':
            print('fixture-gateway')
            sys.exit(opts.get('gateway_output_exit', 0))
        elif key == 'workspaceDeployPublicKey': print('ssh-ed25519 AAAAMAIN')
        elif key == 'agentWorkspaceKeys': print(json.dumps({'test': {'publicKey': 'ssh-ed25519 AAAATEST', 'privateKey': '-----BEGIN OPENSSH PRIVATE KEY-----\nfixture-private-key\n-----END OPENSSH PRIVATE KEY-----'}}))
        elif key == 'tailscaleHostname':
            print(opts.get('pulumi_hostname', name))
            sys.exit(opts.get('hostname_output_exit', 0))
        elif key == 'serverIpv4': print('203.0.113.42')
        else: sys.exit('Unexpected stack output')
        sys.exit(0)
elif cmd == 'sleep':
    assert args == ['10'], 'Unexpected readiness retry delay'
    sys.exit(0)
elif cmd == 'tailscale':
    if args == ['status', '--json']:
        count_path = root / 'tailscale-reads'
        count = int(count_path.read_text()) + 1 if count_path.exists() else 1
        count_path.write_text(str(count))
        peer = {'HostName': name, 'DNSName': name + '.example.ts.net.', 'Online': True, 'TailscaleIPs': ['100.64.0.1'],
                'sshHostKeys': ['ssh-ed25519 AAAATEST'], 'ID': 'nOwned'}
        if count <= opts.get('keys_ready_after', 0): peer['sshHostKeys'] = []
        print(opts.get('raw_tailscale_output', json.dumps(opts.get('tailscale', {'MagicDNSSuffix': 'example.ts.net', 'Peer': {'a': peer}}))))
        sys.exit(opts.get('tailscale_exit', 0))
    if args[:2] == ['ssh', 'ubuntu@' + name + '.example.ts.net']:
        if args[2:] == ['true']: pass
        elif args[2:] == ['hostname']: print(opts.get('hostname', name))
        elif args[2:] == ['ip -j -4 address show scope global']:
            print(opts.get('raw_ip_output', json.dumps([{'addr_info': [{'local': opts.get('ipv4', '203.0.113.42')}]}])))
            sys.exit(opts.get('ip_output_exit', 0))
        elif args[2:] == ['openclaw cron list --all --json']:
            count_path = root / 'cron-reads'
            count = int(count_path.read_text()) + 1 if count_path.exists() else 1
            count_path.write_text(str(count))
            default = json.dumps({'jobs': [{'agentId': 'main', 'name': 'main cron', 'id': 'main-id', 'enabled': False}, {'agentId': 'test', 'name': 'test cron', 'id': 'test-id', 'enabled': True}]})
            before = opts.get('raw_cron_output', default)
            print(opts.get('raw_cron_after_output', before) if count > 1 else before)
            sys.exit(opts.get('cron_after_exit', 0) if count > 1 else opts.get('cron_exit', 0))
        elif args[2:] == ['openclaw status --json']:
            print(opts.get('raw_status_output', json.dumps({'heartbeat': {'agents': [{'agentId': 'main', 'everyMs': None}, {'agentId': 'test', 'everyMs': 1800000}]}})))
            sys.exit(opts.get('status_exit', 0))
        else: sys.exit('Unexpected remote command')
        sys.exit(0)
elif cmd == 'ssh':
    if args[-2:] != ['ubuntu@100.64.0.1', 'true']: sys.exit('Unexpected SSH target')
    sys.exit(0)
elif cmd == 'ansible-galaxy':
    if args != ['collection', 'install', '-r', 'requirements.yml', '--upgrade']: sys.exit('Unexpected Ansible Galaxy invocation')
    sys.exit(0)
elif cmd == 'ansible-playbook':
    for key, value in opts.get('expected_env', {}).items():
        if os.environ.get(key) != value: sys.exit('Incorrect provisioning environment: ' + key)
    secret_files = [pathlib.Path(arg[1:]) for arg in args if arg.startswith('@')]
    if len(secret_files) != 1 or secret_files[0].stat().st_mode & 0o777 != 0o600:
        sys.exit('Provisioning secrets file is not private')
    if args[0] != 'playbook.yml' or args[-2:] != ['--tags', 'agents,telegram']: sys.exit('Unexpected provisioning scope')
    assert os.environ['OPENCLAW_SSH_HOST'] == name + '.example.ts.net', 'Ansible did not receive the pinned host'
    known_hosts = pathlib.Path(os.environ['OPENCLAW_SSH_KNOWN_HOSTS'])
    assert known_hosts.stat().st_mode & 0o777 == 0o600
    assert known_hosts.read_text() == name + '.example.ts.net ssh-ed25519 AAAATEST\n'
    import subprocess
    inventory = subprocess.run([sys.executable, os.environ['INVENTORY_SCRIPT'], '--list'],
                               capture_output=True, text=True, check=True)
    variables = json.loads(inventory.stdout)['_meta']['hostvars'][os.environ['OPENCLAW_SSH_HOST']]
    assert variables['ansible_host_key_checking'] is True
    assert 'StrictHostKeyChecking=yes' in variables['ansible_ssh_common_args']
    assert str(known_hosts) in variables['ansible_ssh_common_args']
    assert 'ProxyCommand=' in variables['ansible_ssh_common_args']
    assert variables['ansible_ssh_args'] == '-o ControlMaster=no -o ControlPath=none'
    print(opts.get('idempotence_log', ''))
    sys.exit(opts.get('ansible_exit', 0))
elif cmd == 'gh':
    if args[:3] == ['repo', 'deploy-key', 'add']:
        sys.exit(opts.get('add_key_exit', 0))
    if args[0] == 'api':
        endpoint = next(arg for arg in args if arg.startswith('repos/'))
        if endpoint == 'repos/pandysp/private-phoenix-probe':
            if opts.get('private_repo_exit'): sys.exit(opts['private_repo_exit'])
            print(json.dumps({'full_name': 'pandysp/private-phoenix-probe', 'private': opts.get('private_repo', True)}))
            sys.exit(0)
        if endpoint == 'repos/pandysp/private-phoenix-probe/contents/':
            if opts.get('private_read_exit'): sys.exit(opts['private_read_exit'])
            print(json.dumps(opts.get('private_contents', [{'type': 'file', 'name': 'README.md'}])))
            sys.exit(0)
        if 'DELETE' in args:
            key_id = int(endpoint.rsplit('/', 1)[1])
            if key_id == opts.get('delete_fail'): sys.exit(1)
            with (root / 'deleted').open('a') as f: f.write(endpoint + '\n')
            sys.exit(0)
        repo = '/'.join(endpoint.split('/')[1:3])
        count_path = root / (repo.replace('/', '-') + '-reads')
        count = int(count_path.read_text()) + 1 if count_path.exists() else 1
        count_path.write_text(str(count))
        if opts.get('key_read_fail') or (opts.get('key_readback_fail') and count > 1): sys.exit(1)
        if 'raw_key_output' in opts:
            print(opts['raw_key_output'])
            sys.exit(0)
        pages = opts.get('key_pages_by_repo', {}).get(repo, opts.get('key_pages', [[], []]))
        deleted = set((root / 'deleted').read_text().splitlines()) if (root / 'deleted').exists() else set()
        if not opts.get('retain_keys'):
            pages = [[key for key in page if endpoint + '/' + str(key['id']) not in deleted] for page in pages]
        print(json.dumps(pages if '--paginate' in args and '--slurp' in args else pages[0]))
        sys.exit(0)
elif cmd == 'tar':
    assert args == ['xzf', 'hcloud.tar.gz']
    assert (root / 'hcloud.tar.gz').is_file()
    (root / 'hcloud').write_text('fake CLI executable')
    sys.exit(0)
elif cmd == 'sudo':
    assert args == ['mv', 'hcloud', '/usr/local/bin/']
    (root / 'hcloud').rename(root / 'hcloud-installed')
    sys.exit(0)
elif cmd == 'curl':
    url = next(arg for arg in args if arg.startswith('https://'))
    if url == 'https://api.github.com/repos/hetznercloud/cli/releases/latest':
        print(opts.get('release_body', '{"tag_name":"v1.2.3"}'))
        sys.exit(opts.get('release_exit', 0))
    if url.startswith('https://github.com/hetznercloud/cli/releases/download/'):
        (root / 'hcloud.tar.gz').write_text('fake release archive')
        sys.exit(opts.get('download_exit', 0))
    if url == 'https://api.tailscale.com/api/v2/oauth/token':
        # The OAuth client secret travels on stdin, never in argv.
        assert '--fail' in args and '--max-time' in args, 'OAuth call must fail loudly and time out'
        assert stdin == 'client_id=fixture-ts-client&client_secret=fixture-ts-secret&grant_type=client_credentials', stdin
        print(opts.get('oauth_body', '{"access_token":"fixture-oauth-token"}'))
        sys.exit(opts.get('oauth_exit', 0))
    if url == 'https://api.tailscale.com/api/v2/tailnet/-/keys':
        assert '--max-time' in args, 'Key minting must time out'
        output = pathlib.Path(args[args.index('--output') + 1])
        assert output.stat().st_mode & 0o077 == 0, 'Mint response file must be private'
        header_file = args[args.index('--header') + 1]
        assert header_file.startswith('@'), 'Bearer token must come from a file'
        header_path = pathlib.Path(header_file[1:])
        assert header_path.stat().st_mode & 0o077 == 0, 'Bearer token file must be private'
        assert header_path.read_text().strip() == 'Authorization: Bearer fixture-oauth-token'
        body = json.loads(stdin)
        assert body == {'description': name, 'expirySeconds': 3600, 'capabilities': {'devices': {'create': {
            'reusable': False, 'ephemeral': True, 'preauthorized': True, 'tags': ['tag:openclaw-staging']}}}}, body
        output.write_text(opts.get('mint_body', '{"key":"fixture-tskey-auth-minted"}'))
        sys.exit(opts.get('mint_exit', 0))
    assert args[0] == '-q' and args[-1] == 'https://api.github.com/repos/pandysp/private-phoenix-probe'
    assert '--max-time' in args and '--connect-timeout' in args
    print(opts.get('anonymous_status', '404'), end='')
    sys.exit(opts.get('anonymous_exit', 0))
elif cmd == 'hcloud' and args[1:] == ['list', '-o', 'json']:
    if opts.get('hcloud_exit'): sys.exit(opts['hcloud_exit'])
    if 'raw_hcloud_output' in opts:
        print(opts['raw_hcloud_output'])
        sys.exit(0)
    print(json.dumps(opts.get('resources', {}).get(args[0], [])))
    sys.exit(0)
sys.exit('Unexpected external command: ' + cmd)
'''


class PhoenixSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        parsed = subprocess.run(
            ['ruby', '-ryaml', '-rjson', '-e', 'puts JSON.generate(YAML.load_file(ARGV[0]))', str(WORKFLOW)],
            check=True, capture_output=True, text=True,
        )
        cls.workflow = json.loads(parsed.stdout)
        cls.steps = {step['name']: step for step in cls.workflow['jobs']['phoenix']['steps']}
        inventory = subprocess.run(
            ['ruby', '-ryaml', '-rjson', '-e', 'puts JSON.generate(YAML.load_file(ARGV[0]))',
             str(WORKFLOW.with_name('staging-cleanup.yml'))], check=True, capture_output=True, text=True)
        cls.inventory_workflow = json.loads(inventory.stdout)
        cls.inventory_step = next(step for step in cls.inventory_workflow['jobs']['inventory']['steps']
                                  if step.get('name') == 'Inventory staging cloud resources and deploy keys')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='phoenix-safety-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'pulumi').mkdir()
        (self.root / 'bin').mkdir()
        executable = self.root / 'bin/fixture-cli'
        executable.write_text('#!' + sys.executable + '\n' + CLI.partition('\n')[2])
        executable.chmod(0o700)
        python = self.root / 'bin/python3'
        python.write_text('#!' + sys.executable + '\n' + '''import os,pathlib,stat,sys
if sys.argv[1] == '-c' and sys.argv[-1].endswith('/secrets.json'):
    target = pathlib.Path(sys.argv[-1])
    if not target.exists(): sys.exit('Secret target did not exist before population')
    info = target.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        sys.exit('Secret target was not regular, owned and private before population')
    (pathlib.Path(os.environ['FIXTURE_ROOT'])/'prewrite-checked').touch()
os.execv(sys.executable, [sys.executable] + sys.argv[1:])
''')
        python.chmod(0o700)
        for name in ('pulumi', 'gh', 'tailscale', 'hcloud', 'curl', 'tar', 'sudo', 'ssh', 'sleep', 'ansible-galaxy', 'ansible-playbook'):
            (self.root / 'bin' / name).symlink_to(executable)
        self.env = {
            'HOME': str(self.root), 'TMPDIR': str(self.root),
            'PATH': str(self.root / 'bin') + ':' + os.environ['PATH'],
            'RUNNER_TEMP': str(self.root), 'GITHUB_OUTPUT': str(self.root / 'output'),
            'GITHUB_ENV': str(self.root / 'env'), 'PHOENIX_RESOURCE_NAME': NAME,
            'STAGING_MODEL': self.workflow['env']['STAGING_MODEL'],
            'FIXTURE_ROOT': str(self.root), 'TELEGRAM_USER_ID': '123456', 'STAGING_HOST': HOST,
            'INVENTORY_SCRIPT': str(WORKFLOW.parents[2] / 'ansible/inventory/pulumi_inventory.py'),
        }
        self.env['TS_OAUTH_CLIENT_ID'] = 'fixture-ts-client'
        self.env['TS_OAUTH_SECRET'] = 'fixture-ts-secret'
        for name in REQUIRED_INPUTS:
            if name not in ('TELEGRAM_USER_ID', 'STAGING_PRIVATE_REPOSITORY', 'TS_OAUTH_CLIENT_ID', 'TS_OAUTH_SECRET'):
                self.env[name] = 'fixture-' + name.lower()
        self.env['STAGING_PRIVATE_REPOSITORY'] = 'pandysp/private-phoenix-probe'

    def run_step(self, name, **options):
        env = self.env | {'FIXTURE_OPTIONS': json.dumps(options)}
        # GitHub's default Bash shell uses -e; explicit shell: bash also uses
        # pipefail. Do not silently strengthen a step that omitted its own flags.
        args = ['bash', '--noprofile', '--norc', '-e']
        if self.steps[name].get('shell') == 'bash':
            args += ['-o', 'pipefail']
        args += ['-c', self.steps[name]['run']]
        with subprocess.Popen(args, cwd=self.root, env=env, text=True,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, start_new_session=True) as process:
            try:
                stdout, stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise
        result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
        self.assertNotIn('fixture-', result.stdout + result.stderr)
        return result

    def calls(self):
        path = self.root / 'calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_shell_steps_are_valid_bash(self):
        for name, step in self.steps.items():
            if 'run' not in step:
                continue
            with self.subTest(step=name):
                result = subprocess.run(['bash', '-n'], input=step['run'],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_galaxy_fixture_needs_no_provisioning_environment(self):
        result = subprocess.run(
            [str(self.root/'bin/ansible-galaxy'), 'collection', 'install', '-r', 'requirements.yml', '--upgrade'],
            env={'HOME': str(self.root), 'PATH': self.env['PATH']},
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[0]['cmd'], 'ansible-galaxy')

    def test_workflows_use_read_only_checkout_credentials_and_step_scoped_secrets(self):
        lint = subprocess.run(
            ['ruby', '-ryaml', '-rjson', '-e', 'puts JSON.generate(YAML.load_file(ARGV[0]))',
             str(WORKFLOW.with_name('lint.yml'))], check=True, capture_output=True, text=True)
        for workflow in (self.workflow, self.inventory_workflow, json.loads(lint.stdout)):
            self.assertEqual(workflow.get('permissions'), {'contents': 'read'})
            self.assertNotIn('${{ secrets.', json.dumps(workflow.get('env', {})))
            for job in workflow['jobs'].values():
                self.assertNotIn('${{ secrets.', json.dumps(job.get('env', {})))
                for step in job['steps']:
                    if step.get('uses', '').startswith('actions/checkout@'):
                        self.assertIs(step.get('with', {}).get('persist-credentials'), False)
                    if step.get('name', '').startswith(('Install ', 'Setup ')):
                        self.assertNotIn('${{ secrets.', json.dumps(step.get('env', {})))
        for name in ('Initialize Pulumi staging stack', 'Create workspace deploy keys',
                     'Add deploy keys to GitHub repos', 'Deploy staging infrastructure',
                     'Resolve staging host', 'Verify scheduled automation and idempotence',
                     'Destroy staging infrastructure', 'Remove verified staging stack'):
            self.assertTrue(set(BACKEND_ENV) <= self.steps[name].get('env', {}).keys(), name)
        for name in ('Run smoke test', 'Trigger workspace git sync', 'Run deployment verification'):
            self.assertNotIn('${{ secrets.', json.dumps(self.steps[name].get('env', {})))
        self.assertEqual(set(self.steps['Remove deploy keys']['env']), {'GH_TOKEN'})
        self.assertEqual(set(self.steps['Verify Hetzner cleanup']['env']), {'HCLOUD_TOKEN'})

    def test_private_deploy_keys_are_registered_before_provisioning_can_run(self):
        ordered = list(self.steps)
        self.assertLess(ordered.index('Add deploy keys to GitHub repos'), ordered.index('Deploy staging infrastructure'))
        self.assertLess(ordered.index('Initialize Pulumi staging stack'), ordered.index('Create workspace deploy keys'))
        self.assertLess(ordered.index('Create workspace deploy keys'), ordered.index('Add deploy keys to GitHub repos'))
        self.assertNotIn('--target-dependents', self.steps['Create workspace deploy keys']['run'])

    def test_key_creation_targets_only_existing_main_and_test_resources(self):
        result = self.run_step('Create workspace deploy keys')
        self.assertEqual(result.returncode, 0, result.stderr)
        updates = [call['args'] for call in self.calls() if call['cmd'] == 'pulumi' and call['args'][0] == 'up']
        self.assertEqual(len(updates), 1)
        args = updates[0]
        self.assertEqual([args[i + 1] for i, value in enumerate(args) if value == '--target'], [KEY_URN, KEY_URN + '-test'])
        self.assertFalse(any(call['cmd'] in ('hcloud', 'tailscale', 'ansible-playbook') for call in self.calls()))
        self.assertTrue(any(call['args'][:2] == ['stack', 'export'] for call in self.calls()))
        for options in ({'key_up_exit': 71}, {'export_exit': 72},
                        {'raw_export_output': ''}, {'raw_export_output': 'not JSON'},
                        {'raw_export_output': '{"deployment":{"resources":[]}}'},
                        {'raw_export_output': '{"deployment":{"resources":[]}}\n{"deployment":{"resources":[]}}'}):
            with self.subTest(options=options):
                self.assertNotEqual(self.run_step('Create workspace deploy keys', **options).returncode, 0)

    def test_key_creation_refuses_missing_duplicate_or_unexpected_materialized_resources(self):
        main = {'urn': KEY_URN, 'type': 'tls:index/privateKey:PrivateKey', 'id': 'main-key'}
        test = main | {'urn': KEY_URN + '-test', 'id': 'test-key'}
        for resources in ([main], [main, main], [main, test | {'id': ''}],
                          [main, test, {'urn': 'other', 'type': 'hcloud:index/server:Server', 'id': 'server'}]):
            with self.subTest(resources=resources):
                raw = json.dumps({'deployment': {'resources': resources}})
                self.assertNotEqual(self.run_step('Create workspace deploy keys', raw_export_output=raw).returncode, 0)

    def test_cli_bootstrap_requires_successful_metadata_and_archive_downloads(self):
        result = self.run_step('Install hcloud CLI')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'hcloud-installed').is_file())
        for options in ({'release_exit': 79}, {'release_body': '{}'},
                        {'release_body': '{"tag_name":null}'}, {'release_body': '{"tag_name":12}'},
                        {'release_body': '{"tag_name":""}'}, {'release_body': ''},
                        {'release_body': '{"tag_name":"v1.2.3"}\n{"tag_name":"v1.2.3"}'},
                        {'release_body': 'invalid JSON'}, {'download_exit': 80}):
            with self.subTest(options=options):
                (self.root / 'hcloud-installed').unlink(missing_ok=True)
                before = len(self.calls())
                result = self.run_step('Install hcloud CLI', **options)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.root / 'hcloud-installed').exists())
                self.assertFalse(any(call['cmd'] in ('tar', 'sudo') for call in self.calls()[before:]))

    def test_native_step_deadlines_leave_job_time_for_cleanup(self):
        job = self.workflow['jobs']['phoenix']
        for step in job['steps']:
            with self.subTest(step=step['name']):
                self.assertIsInstance(step.get('timeout-minutes'), int)
                self.assertGreater(step['timeout-minutes'], 0)
        work_budget = sum(step['timeout-minutes'] for step in job['steps'] if step['name'] not in CLEANUP)
        cleanup_budget = sum(self.steps[name]['timeout-minutes'] for name in CLEANUP)
        # Static GHA deadline configuration, not a simulated runner/cancellation test.
        self.assertGreaterEqual(job['timeout-minutes'], work_budget + cleanup_budget + 5)

    def test_generated_staging_config_uses_the_verified_model_without_fallback(self):
        directory = self.root / 'ansible/group_vars'
        directory.mkdir(parents=True)
        result = self.run_step('Generate staging openclaw.yml')
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = subprocess.run(
            ['ruby', '-ryaml', '-rjson', '-e', 'puts JSON.generate(YAML.load_file(ARGV[0]))',
             str(directory / 'openclaw.yml')], check=True, capture_output=True, text=True,
        )
        config = json.loads(parsed.stdout)
        self.assertEqual(config['openclaw_model_primary'], 'anthropic/claude-sonnet-4-6')
        self.assertEqual(config['openclaw_model_fallbacks'], [])

    def test_existing_stack_rejection_never_claims_ownership_or_removes_state(self):
        result = self.run_step('Initialize Pulumi staging stack', init_exit=71)
        self.assertEqual(result.returncode, 71)
        self.assertFalse((self.root / 'output').exists())
        self.assertEqual([call['args'][:2] for call in self.calls() if call['cmd'] == 'pulumi'], [['stack', 'init']])

    def test_required_inputs_match_the_step_environment(self):
        step = self.steps['Initialize Pulumi staging stack']
        self.assertCountEqual(list(step['env']) + ['STAGING_PRIVATE_REPOSITORY'], REQUIRED_INPUTS)
        for name in ('OBSIDIAN_AUTH_TOKEN', 'OBSIDIAN_VAULT_PASSWORD', 'obsidianAuthToken', 'obsidianVaultPassword'):
            self.assertNotIn(name, json.dumps(self.workflow))

    def test_missing_or_empty_required_inputs_fail_before_any_external_call(self):
        for name in REQUIRED_INPUTS:
            original = self.env[name]
            for value in (None, ''):
                with self.subTest(name=name, value=value):
                    if value is None:
                        self.env.pop(name)
                    else:
                        self.env[name] = value
                    try:
                        result = self.run_step('Initialize Pulumi staging stack')
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn(name, result.stdout + result.stderr)
                        self.assertFalse((self.root / 'output').exists())
                        self.assertEqual(self.calls(), [])
                    finally:
                        self.env[name] = original

    def test_successful_init_claims_ownership_and_secrets_use_stdin(self):
        result = self.run_step('Initialize Pulumi staging stack')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'output').read_text(), 'owned=true\n')
        calls = self.calls()
        secret_calls = [call for call in calls if '--secret' in call['args']]
        self.assertCountEqual([call['args'][2] for call in secret_calls], SECRET_CONFIG_KEYS)
        self.assertTrue(all(call['stdin'].startswith('fixture-') for call in secret_calls))
        self.assertTrue(any(call['args'][:4] == ['config', 'set', 'serverName', NAME] for call in calls))
        # The server's tag is owned by tag:ci and has no source rights on the tailnet.
        self.assertTrue(any(call['args'][:4] == ['config', 'set', 'tailscaleTags', 'tag:openclaw-staging'] for call in calls))
        self.assertNotIn('fixture-', json.dumps([call['args'] for call in calls]))
        # The server joins with a key minted for this run only: ephemeral,
        # single use, pre-authorized, tag:server, short-lived. No stored key can expire.
        minted = [call for call in secret_calls if call['args'][2] == 'tailscaleAuthKey']
        self.assertEqual([call['stdin'] for call in minted], ['fixture-tskey-auth-minted'])
        self.assertNotIn('secrets.TS_AUTHKEY', json.dumps(self.workflow))

    def test_auth_key_minting_failures_stop_before_stack_init(self):
        for options in ({'oauth_exit': 22}, {'oauth_body': '{}'}, {'oauth_body': '{"access_token":""}'},
                        {'oauth_body': 'not json'}, {'mint_exit': 7}, {'mint_body': '{}'},
                        {'mint_body': '{"key":""}'}, {'mint_body': '{"key":7}'}, {'mint_body': 'not json'},
                        {'mint_body': '{"message":"requested tags [tag:server] are invalid or not permitted"}'}):
            with self.subTest(options=options):
                result = self.run_step('Initialize Pulumi staging stack', **options)
                self.assertNotEqual(result.returncode, 0)
                if 'message' in options.get('mint_body', ''):
                    self.assertIn('not permitted', result.stdout)
                self.assertFalse((self.root / 'output').exists())
                self.assertFalse(any(call['cmd'] == 'pulumi' and call['args'][:2] == ['stack', 'init'] for call in self.calls()))
                self.assertNotIn('fixture-', result.stdout + result.stderr)

    def test_private_fixture_must_be_supplied_and_validated_before_stack_init(self):
        self.assertEqual(self.workflow['env'].get('STAGING_PRIVATE_REPOSITORY'), '${{ vars.STAGING_PRIVATE_REPOSITORY }}')
        self.assertEqual(self.steps['Initialize Pulumi staging stack']['env']['GH_TOKEN'],
                         self.steps['Initialize Pulumi staging stack']['env']['GITHUB_TOKEN_PAT'])
        for options in ({'private_repo': False}, {'private_repo_exit': 1},
                        {'private_read_exit': 1}, {'private_contents': []},
                        {'private_contents': {}}, {'anonymous_status': '200'},
                        {'anonymous_status': '404', 'anonymous_exit': 1}):
            with self.subTest(options=options):
                result = self.run_step('Initialize Pulumi staging stack', **options)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.root / 'output').exists())
                self.assertFalse(any(call['cmd'] == 'pulumi' for call in self.calls()))

    def test_config_failure_still_leaves_ownership_for_cleanup(self):
        result = self.run_step('Initialize Pulumi staging stack', config_exit=72)
        self.assertEqual(result.returncode, 72)
        self.assertEqual((self.root / 'output').read_text(), 'owned=true\n')

    def test_all_cleanup_steps_have_the_native_ownership_condition(self):
        self.assertEqual(self.steps['Initialize Pulumi staging stack']['id'], 'init')
        for name in CLEANUP[:3]:
            self.assertEqual(self.steps[name]['if'], "always() && steps.init.outputs.owned == 'true'")
        cleanup_steps = [step['name'] for step in self.steps.values() if 'always()' in step.get('if', '')]
        self.assertCountEqual(cleanup_steps, CLEANUP)

    def test_unique_run_and_attempt_name_is_the_single_ownership_name(self):
        self.assertEqual(self.workflow['env']['PHOENIX_RESOURCE_NAME'],
                         'openclaw-staging-${{ github.run_id }}-${{ github.run_attempt }}')

    def test_pulumi_and_completed_clean_recap_are_both_required(self):
        result = self.run_step('Deploy staging infrastructure', up_log=CLEAN_RECAP)
        self.assertEqual(result.returncode, 0, result.stderr)
        for log, status in ((CLEAN_RECAP, 1), (CLEAN_RECAP, 124), ('resource_unavailable', 1),
                            ('', 0), ('ok=42 unreachable=0 failed=0', 0), ('PLAY RECAP **', 0),
                            (CLEAN_RECAP.replace('=== Provisioning complete ===\n', ''), 0),
                            (CLEAN_RECAP.replace('failed=0', 'failed=1'), 0),
                            (CLEAN_RECAP.replace('unreachable=0', 'unreachable=10'), 0),
                            (CLEAN_RECAP.replace('ok=42', 'ok=0'), 0),
                            (CLEAN_RECAP + 'PLAY RECAP **\n', 0)):
            with self.subTest(status=status, log=log):
                self.assertNotEqual(self.run_step('Deploy staging infrastructure', up_log=log, up_exit=status).returncode, 0)

    def test_capacity_errors_retry_at_the_next_location_only(self):
        capacity = {'log': 'error during placement (resource_unavailable, abc)', 'exit': 255}
        result = self.run_step('Deploy staging infrastructure', up_sequence=[capacity, {'log': CLEAN_RECAP, 'exit': 0}])
        self.assertEqual(result.returncode, 0, result.stderr)
        locations = [call['args'][3] for call in self.calls()
                     if call['cmd'] == 'pulumi' and call['args'][:3] == ['config', 'set', 'serverLocation']]
        self.assertEqual(locations, ['nbg1', 'fsn1'])
        (self.root / 'calls.jsonl').unlink()
        exhausted = self.run_step('Deploy staging infrastructure', up_sequence=[capacity, capacity, capacity])
        self.assertNotEqual(exhausted.returncode, 0)
        self.assertIn('every configured location', exhausted.stdout + exhausted.stderr)
        other = {'log': 'error: update failed (fixture unrelated)', 'exit': 255}
        (self.root / 'calls.jsonl').unlink()
        result = self.run_step('Deploy staging infrastructure', up_sequence=[other, {'log': CLEAN_RECAP, 'exit': 0}])
        self.assertNotEqual(result.returncode, 0)
        ups = [call for call in self.calls() if call['cmd'] == 'pulumi' and call['args'][:1] == ['up']]
        self.assertEqual(len(ups), 1, 'A non-capacity failure must not retry')

    def test_ansi_recaps_are_parsed_and_all_hosts_must_succeed(self):
        colored = CLEAN_RECAP.replace('server :', '\033[0;32mserver :').replace('skipped=7', '\033[0mskipped=7')
        self.assertEqual(self.run_step('Deploy staging infrastructure', up_log=colored).returncode, 0)
        mixed = CLEAN_RECAP + 'other : ok=8 changed=0 unreachable=0 failed=2 skipped=0\n'
        self.assertNotEqual(self.run_step('Deploy staging infrastructure', up_log=mixed).returncode, 0)

    def test_host_is_exact_online_unique_and_matches_created_server_ip(self):
        result = self.run_step('Resolve staging host')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'env').read_text(), 'STAGING_HOST=' + HOST +
                         '\nSTAGING_PUBLIC_IP=203.0.113.42\nPHOENIX_TAILSCALE_NODE_ID=nOwned\n')
        peer = {'HostName': NAME, 'DNSName': HOST + '.', 'Online': True}
        for peers in ({}, {'a': peer, 'b': peer},
                      {'a': peer | {'Online': False}},
                      {'a': peer | {'HostName': NAME + '-1'}},
                      {'a': peer | {'DNSName': 'other.example.ts.net.'}}):
            with self.subTest(peers=peers):
                self.assertNotEqual(self.run_step('Resolve staging host', tailscale={'MagicDNSSuffix': 'example.ts.net', 'Peer': peers}).returncode, 0)
        for options in ({'tailscale_exit': 1}, {'hostname': 'other-server'}, {'ipv4': '203.0.113.9'}):
            with self.subTest(options=options):
                self.assertNotEqual(self.run_step('Resolve staging host', **options).returncode, 0)

    def test_host_address_requires_one_json_array_and_successful_ssh(self):
        valid = json.dumps([{'addr_info': [{'local': '203.0.113.42'}]}])
        for options in ({'raw_ip_output': ''}, {'raw_ip_output': 'null'},
                        {'raw_ip_output': '{}'}, {'raw_ip_output': 'not JSON'},
                        {'raw_ip_output': '[]\n' + valid},
                        {'raw_ip_output': valid, 'ip_output_exit': 1}):
            with self.subTest(options=options):
                (self.root / 'env').unlink(missing_ok=True)
                result = self.run_step('Resolve staging host', **options)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.root / 'env').exists())

    def test_deploy_keys_use_the_exact_run_title_and_no_private_output(self):
        result = self.run_step('Add deploy keys to GitHub repos')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [call for call in self.calls() if call['cmd'] == 'gh']
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertEqual(call['args'][call['args'].index('-t') + 1], NAME)
            self.assertTrue(call['stdin'].startswith('ssh-ed25519 '))

    def test_deploy_key_registration_failure_stops_before_second_repository(self):
        result = self.run_step('Add deploy keys to GitHub repos', add_key_exit=73)
        self.assertEqual(result.returncode, 73)
        self.assertEqual(len([call for call in self.calls() if call['cmd'] == 'gh']), 1)

    def test_key_cleanup_paginates_and_deletes_only_exact_run_keys(self):
        pages = {
            'pandysp/openclaw-staging-workspace': [[{'id': 1, 'title': 'staging-old'}], [{'id': 2, 'title': NAME}, {'id': 3, 'title': NAME + '-other'}]],
            'pandysp/openclaw-staging-workspace-test': [[{'id': 4, 'title': NAME + '-other'}], [{'id': 5, 'title': NAME}]],
        }
        result = self.run_step('Remove deploy keys', key_pages_by_repo=pages)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'deleted').read_text().splitlines(), [
            'repos/pandysp/openclaw-staging-workspace/keys/2',
            'repos/pandysp/openclaw-staging-workspace-test/keys/5',
        ])
        reads = [call for call in self.calls() if call['cmd'] == 'gh' and 'DELETE' not in call['args']]
        self.assertEqual(len(reads), 4)
        self.assertTrue(all('--paginate' in call['args'] and '--slurp' in call['args'] for call in reads))

    def test_key_inventory_deletion_and_readback_failures_are_fatal(self):
        for options in ({'key_read_fail': True}, {'delete_fail': 2}, {'retain_keys': True}):
            with self.subTest(options=options):
                result = self.run_step('Remove deploy keys', key_pages=[[{'id': 2, 'title': NAME}]], **options)
                self.assertNotEqual(result.returncode, 0)
                if (self.root / 'deleted').exists():
                    (self.root / 'deleted').unlink()

    def test_key_readback_error_is_fatal_even_after_successful_deletion(self):
        result = self.run_step('Remove deploy keys', key_pages=[[{'id': 2, 'title': NAME}]], key_readback_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.root / 'deleted').exists())
        self.assertIn('Could not verify deploy-key cleanup', result.stdout)

    def test_empty_or_invalid_inventory_responses_do_not_prove_cleanup(self):
        for raw in ('', 'null', '{}', '[{}]', 'not JSON'):
            with self.subTest(raw=raw):
                self.assertNotEqual(self.run_step('Remove deploy keys', raw_key_output=raw).returncode, 0)
                self.assertNotEqual(self.run_step('Verify Hetzner cleanup', raw_hcloud_output=raw).returncode, 0)

    def test_multiple_inventory_documents_cannot_look_like_zero_leftovers(self):
        self.assertNotEqual(self.run_step('Remove deploy keys', raw_key_output='[[]]\n[[]]').returncode, 0)
        self.assertNotEqual(self.run_step('Verify Hetzner cleanup', raw_hcloud_output='[]\n[]').returncode, 0)
        # gh --paginate --slurp must report at least one actual page, even when empty.
        self.assertNotEqual(self.run_step('Remove deploy keys', raw_key_output='[]').returncode, 0)
        self.assertEqual(self.run_step('Remove deploy keys', raw_key_output='[[]]').returncode, 0)
        self.assertEqual(self.run_step('Verify Hetzner cleanup', raw_hcloud_output='[]').returncode, 0)

    def test_destroy_failure_keeps_checkpoint_and_no_force_removal_is_used(self):
        result = self.run_step('Destroy staging infrastructure', destroy_exit=73)
        self.assertEqual(result.returncode, 73)
        self.assertEqual([call['args'][0] for call in self.calls()], ['destroy'])
        self.assertNotIn('--force', self.steps['Destroy staging infrastructure']['run'])

    def test_successful_destroy_keeps_checkpoint_until_cleanup_is_verified(self):
        self.assertEqual(self.run_step('Destroy staging infrastructure').returncode, 0)
        self.assertEqual([call['args'][:2] for call in self.calls()], [['destroy', '--yes']])

    def test_checkpoint_removal_requires_all_cleanup_outcomes_and_runs_last(self):
        step = self.steps['Remove verified staging stack']
        self.assertEqual(step['if'], "always() && steps.init.outputs.owned == 'true' && "
                         "steps.cleanup_keys.outcome == 'success' && "
                         "steps.destroy.outcome == 'success' && "
                         "steps.cleanup_tailnet.outcome == 'success' && "
                         "steps.cleanup_cloud.outcome == 'success'")
        ordered_names = list(self.steps)
        for name, step_id in zip(CLEANUP[:-1], ('cleanup_keys', 'destroy', 'cleanup_tailnet', 'cleanup_cloud'), strict=True):
            self.assertEqual(self.steps[name]['id'], step_id)
            self.assertLess(ordered_names.index(name), ordered_names.index('Remove verified staging stack'))

    def test_verified_stack_removal_failure_is_fatal(self):
        self.assertEqual(self.run_step('Remove verified staging stack').returncode, 0)
        self.assertEqual([call['args'][:2] for call in self.calls()], [['stack', 'rm']])
        self.assertEqual(self.run_step('Remove verified staging stack', rm_exit=74).returncode, 74)
        self.assertNotIn('--force', self.steps['Remove verified staging stack']['run'])

    def test_cloud_inventory_is_read_only_and_owned_leftovers_fail(self):
        result = self.run_step('Verify Hetzner cleanup', resources={'server': [{'id': 7, 'name': NAME + '-old'}]})
        self.assertEqual(result.returncode, 0, result.stderr)
        for kind, suffix in (('server', ''), ('ssh-key', '-key'), ('firewall', '-firewall')):
            with self.subTest(kind=kind):
                result = self.run_step('Verify Hetzner cleanup', resources={kind: [{'id': 8, 'name': NAME + suffix}]})
                self.assertNotEqual(result.returncode, 0)
        self.assertNotEqual(self.run_step('Verify Hetzner cleanup', hcloud_exit=1).returncode, 0)
        self.assertTrue(all(call['args'][1:] == ['list', '-o', 'json'] for call in self.calls()))

    def test_idempotence_requires_a_complete_clean_unchanged_recap(self):
        # Execute the real repository provisioner; mock only its external CLIs.
        scripts = self.root / 'scripts'
        scripts.mkdir()
        provision = scripts / 'provision.sh'
        provision.write_bytes((WORKFLOW.parents[2] / 'scripts/provision.sh').read_bytes())
        provision.chmod(0o700)
        (self.root / 'ansible').mkdir()
        self.env.update({'PROVISION_GATEWAY_TOKEN': 'fixture-gateway',
                         'PROVISION_CLAUDE_SETUP_TOKEN': 'fixture-claude',
                         'PROVISION_TAILSCALE_HOSTNAME': NAME,
                         'PROVISION_AGENT_IDS': 'test'})
        clean = CLEAN_RECAP.replace('changed=18', 'changed=0').replace('=== Provisioning complete ===\n', '')
        result = self.run_step('Verify scheduled automation and idempotence', idempotence_log=clean)
        self.assertEqual(result.returncode, 0, result.stderr)
        for log, status in (('', 0), ('changed=0 failed=0', 0), ('PLAY RECAP **', 0),
                            (clean.replace('changed=0', 'changed=1'), 0),
                            (clean.replace('failed=0', 'failed=10'), 0),
                            (clean.replace('unreachable=0', 'unreachable=1'), 0),
                            (clean + 'other : ok=1 changed=2 unreachable=0 failed=0\n', 0),
                            (clean + 'PLAY RECAP **\n', 0), (clean, 1)):
            with self.subTest(log=log, status=status):
                result = self.run_step('Verify scheduled automation and idempotence',
                                       idempotence_log=log, ansible_exit=status)
                self.assertNotEqual(result.returncode, 0)
        ansible_calls = [call for call in self.calls() if call['cmd'] == 'ansible-playbook']
        self.assertEqual(len(ansible_calls), 10)

    def test_idempotence_selects_staging_and_rejects_failed_required_reads(self):
        scripts = self.root / 'scripts'
        scripts.mkdir()
        provision = scripts / 'provision.sh'
        provision.write_bytes((WORKFLOW.parents[2] / 'scripts/provision.sh').read_bytes())
        provision.chmod(0o700)
        (self.root / 'ansible').mkdir()
        for options in ({'select_exit': 1}, {'outputs_read_exit': 1},
                        {'output_values': {'openclawGatewayToken': ''}},
                        {'output_values': {'tailscaleHostname': ''}},
                        {'pulumi_hostname': 'openclaw-vps'}):
            with self.subTest(options=options):
                result = self.run_step('Verify scheduled automation and idempotence', **options)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(call['cmd'] == 'ansible-playbook' for call in self.calls()))
        calls = [call for call in self.calls() if call['cmd'] == 'pulumi']
        self.assertEqual(calls[0]['args'], ['stack', 'select', 'staging', '--non-interactive'])

    def test_idempotence_rejects_failed_claude_read_even_with_nonempty_stdout(self):
        scripts = self.root / 'scripts'
        scripts.mkdir()
        provision = scripts / 'provision.sh'
        provision.write_bytes((WORKFLOW.parents[2] / 'scripts/provision.sh').read_bytes())
        provision.chmod(0o700)
        (self.root / 'ansible').mkdir()
        clean = CLEAN_RECAP.replace('changed=18', 'changed=0').replace('=== Provisioning complete ===\n', '')
        result = self.run_step('Verify scheduled automation and idempotence',
                               idempotence_log=clean, config_read_exit=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(call['cmd'] == 'ansible-playbook' for call in self.calls()))

    def test_idempotence_loads_the_selected_stack_and_passes_its_exact_host_to_ansible(self):
        scripts = self.root / 'scripts'
        scripts.mkdir()
        provision = scripts / 'provision.sh'
        provision.write_bytes((WORKFLOW.parents[2] / 'scripts/provision.sh').read_bytes())
        provision.chmod(0o700)
        (self.root / 'ansible').mkdir()
        clean = CLEAN_RECAP.replace('changed=18', 'changed=0').replace('=== Provisioning complete ===\n', '')
        result = self.run_step('Verify scheduled automation and idempotence', idempotence_log=clean)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual(sum(call['cmd'] == 'ansible-playbook' for call in calls), 1)
        self.assertFalse(any(call['cmd'] == 'ssh' for call in calls))
        self.assertIn(['stack', 'select', 'staging', '--non-interactive'], [call['args'] for call in calls])
        self.assertEqual([call['args'] for call in calls if call['cmd'] == 'pulumi'], [
            ['stack', 'select', 'staging', '--non-interactive'],
            ['config', '--json', '--show-secrets', '--non-interactive'],
            ['stack', 'output', '--json', '--show-secrets', '--non-interactive'],
        ])

    def install_provisioner(self):
        scripts = self.root / 'scripts'
        scripts.mkdir()
        provision = scripts / 'provision.sh'
        provision.write_bytes((WORKFLOW.parents[2] / 'scripts/provision.sh').read_bytes())
        provision.chmod(0o700)
        (self.root / 'ansible').mkdir()

    def test_idempotence_validates_cron_identity_and_policy_before_ansible(self):
        self.install_provisioner()
        jobs = [{'agentId': 'main', 'name': 'main cron', 'id': 'main-id', 'enabled': False},
                {'agentId': 'test', 'name': 'test cron', 'id': 'test-id', 'enabled': True}]
        clean = CLEAN_RECAP.replace('changed=18', 'changed=0').replace('=== Provisioning complete ===\n', '')
        variants = [
            [{key: value for key, value in job.items() if key != 'id'} for job in jobs],
            [jobs[0], jobs[1] | {'id': ''}],
            [jobs[0], jobs[1] | {'id': 123}],
            [jobs[0], jobs[1] | {'id': 'main-id'}],
            [jobs[0], jobs[1] | {'name': ''}],
            [jobs[0], jobs[1] | {'enabled': 'false'}],
        ]
        options = [{'raw_cron_output': json.dumps({'jobs': variant})} for variant in variants]
        options += [{'raw_status_output': json.dumps({'heartbeat': {'agents': [
            {'agentId': 'main', 'everyMs': None}, {'agentId': 'test', 'everyMs': 'enabled'}]}})},
            {'cron_exit': 1}, {'status_exit': 1}]
        for values in options:
            with self.subTest(values=values):
                (self.root / 'cron-reads').unlink(missing_ok=True)
                before = len([call for call in self.calls() if call['cmd'] == 'ansible-playbook'])
                result = self.run_step('Verify scheduled automation and idempotence',
                                       idempotence_log=clean, **values)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(len([call for call in self.calls() if call['cmd'] == 'ansible-playbook']), before)

    def test_idempotence_checks_post_run_policy_and_withholds_private_job_names(self):
        self.install_provisioner()
        jobs = [{'agentId': 'main', 'name': 'main cron', 'id': 'main-id', 'enabled': False},
                {'agentId': 'test', 'name': 'test cron', 'id': 'test-id', 'enabled': True}]
        clean = CLEAN_RECAP.replace('changed=18', 'changed=0').replace('=== Provisioning complete ===\n', '')
        options = [
            {'raw_cron_after_output': json.dumps({'jobs': [jobs[0], jobs[1] | {'enabled': False}]})},
            {'raw_cron_after_output': json.dumps({'jobs': [jobs[0], jobs[1] | {'id': 'new-id', 'name': 'fixture-private-title'}]})},
            {'cron_after_exit': 1},
        ]
        for values in options:
            with self.subTest(values=values):
                (self.root / 'cron-reads').unlink(missing_ok=True)
                before = len([call for call in self.calls() if call['cmd'] == 'ansible-playbook'])
                result = self.run_step('Verify scheduled automation and idempotence',
                                       idempotence_log=clean, **values)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(len([call for call in self.calls() if call['cmd'] == 'ansible-playbook']), before + 1)

    def test_invalid_snapshots_or_fields_stop_before_ansible_and_remove_temporary_secrets(self):
        self.install_provisioner()
        options = [{'config_read_exit': 1}, {'outputs_read_exit': 1}]
        for name in ('raw_config_output', 'raw_stack_output'):
            options.extend({name: raw} for raw in ('', 'null', '[]', 'not JSON fixture-private', '{}\n{}'))
        options.extend([
            {'output_values': {'openclawGatewayToken': None}},
            {'output_values': {'tailscaleHostname': []}},
            {'output_values': {'agentWorkspaceKeys': None}},
            {'output_values': {'agentWorkspaceKeys': {'test': None}}},
            {'output_values': {'agentWorkspaceKeys': {'test': {'privateKey': None}},
                               'workspaceTestDeployPrivateKey': 'fixture-legacy-must-not-hide-invalid-field'}},
            {'config_values': {'openclaw-infra:claudeSetupToken': {'value': None}}},
            {'config_values': {'openclaw-infra:githubTokenTest': {'value': {'unexpected': 'fixture-private'}}}},
            {'config_values': {'openclaw-infra:agentIds': {'value': 'bad-id'}}},
        ])
        for values in options:
            with self.subTest(options=values):
                result = self.run_step('Verify scheduled automation and idempotence', **values)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(call['cmd'] == 'ansible-playbook' for call in self.calls()))
                self.assertEqual(list(self.root.glob('tmp.*')), [])

    def test_config_snapshot_preserves_values_and_clears_absent_optional_values(self):
        self.install_provisioner()
        self.env['PROVISION_GITHUB_TOKEN'] = 'fixture-inherited-must-clear'
        values = {
            'claudeSetupToken': ('PROVISION_CLAUDE_SETUP_TOKEN', 'fixture-claude'),
            'claudeOAuthCredentials': ('PROVISION_CLAUDE_OAUTH_CREDENTIALS', '{"accessToken":"fixture-oauth"}'),
            'telegramBotToken': ('PROVISION_TELEGRAM_BOT_TOKEN', 'fixture-telegram'),
            'telegramUserId': ('PROVISION_TELEGRAM_USER_ID', '12345'),
            'telegramGroupId': ('PROVISION_TELEGRAM_GROUP_ID', '-12345'),
            'xaiApiKey': ('PROVISION_XAI_API_KEY', 'fixture-xai'),
            'groqApiKey': ('PROVISION_GROQ_API_KEY', 'fixture-groq'),
            'geminiApiKey': ('PROVISION_GEMINI_API_KEY', 'fixture-gemini'),
            'obsidianAuthToken': ('PROVISION_OBSIDIAN_AUTH_TOKEN', 'fixture-obsidian'),
            'obsidianVaultPassword': ('PROVISION_OBSIDIAN_VAULT_PASSWORD', 'fixture-quote"\\slash\nline'),
            'discordBotToken': ('PROVISION_DISCORD_BOT_TOKEN', 'fixture-discord'),
            'discordGuildId': ('PROVISION_DISCORD_GUILD_ID', '67890'),
            'discordUserId': ('PROVISION_DISCORD_USER_ID', '98765'),
            'githubTokenTest': ('PROVISION_GITHUB_TOKEN_TEST', 'fixture-test-github'),
            'telegramTestUserId': ('PROVISION_TELEGRAM_TEST_USER_ID', '11111'),
            'telegramTestGroupId': ('PROVISION_TELEGRAM_TEST_GROUP_ID', '-11111'),
            'whatsappTestPhone': ('PROVISION_WHATSAPP_TEST_PHONE', '+123456789'),
        }
        config = {'openclaw-infra:' + key: {'value': value} for key, (_, value) in values.items()}
        expected = {name: value for name, value in values.values()}
        expected.update({'PROVISION_GITHUB_TOKEN': '', 'PROVISION_WORKSPACE_REPO_URL': '',
                         'PROVISION_WORKSPACE_TEST_REPO_URL': ''})
        clean = CLEAN_RECAP.replace('changed=18', 'changed=0').replace('=== Provisioning complete ===\n', '')
        result = self.run_step('Verify scheduled automation and idempotence', config_values=config,
                               expected_env=expected, idempotence_log=clean)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sum(call['cmd'] == 'ansible-playbook' for call in self.calls()), 1)
        self.assertTrue((self.root / 'prewrite-checked').exists())
        self.assertEqual(list(self.root.glob('tmp.*')), [])

    def test_provisioner_rejects_multiple_tailscale_documents_before_ansible(self):
        self.install_provisioner()
        clean = CLEAN_RECAP.replace('changed=18', 'changed=0').replace('=== Provisioning complete ===\n', '')
        valid = {'MagicDNSSuffix': 'example.ts.net', 'Peer': {
            'a': {'HostName': NAME, 'DNSName': HOST + '.', 'Online': True}}}
        trailing = {'MagicDNSSuffix': 'example.ts.net', 'Peer': {}}
        result = self.run_step('Verify scheduled automation and idempotence', idempotence_log=clean,
                               raw_tailscale_output=json.dumps(valid) + '\n' + json.dumps(trailing))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(call['cmd'] == 'ansible-playbook' for call in self.calls()))

    def test_workspace_keys_prefer_structured_fields_and_only_fall_back_when_absent(self):
        self.install_provisioner()
        structured = '-----BEGIN OPENSSH PRIVATE KEY-----\nfixture-structured\n-----END OPENSSH PRIVATE KEY-----'
        legacy = structured.replace('structured', 'legacy')
        config = {'openclaw-infra:workspaceRepoUrl': {'value': 'git@github.com:pandysp/main.git'},
                  'openclaw-infra:workspaceTestRepoUrl': {'value': 'git@github.com:pandysp/test.git'}}
        clean = CLEAN_RECAP.replace('changed=18', 'changed=0').replace('=== Provisioning complete ===\n', '')
        for keys, expected_key in (({'main': {'privateKey': structured}, 'test': {'privateKey': structured}}, structured),
                                   ({}, legacy), ({'main': {}, 'test': {}}, legacy)):
            with self.subTest(keys=keys):
                outputs = {'agentWorkspaceKeys': keys, 'workspaceDeployPrivateKey': legacy,
                           'workspaceTestDeployPrivateKey': legacy}
                expected = {'PROVISION_WORKSPACE_DEPLOY_KEY': expected_key,
                            'PROVISION_WORKSPACE_TEST_DEPLOY_KEY': expected_key,
                            'PROVISION_WORKSPACE_REPO_URL': 'git@github.com:pandysp/main.git',
                            'PROVISION_WORKSPACE_TEST_REPO_URL': 'git@github.com:pandysp/test.git'}
                result = self.run_step('Verify scheduled automation and idempotence', config_values=config,
                                       output_values=outputs, expected_env=expected, idempotence_log=clean)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(list(self.root.glob('tmp.*')), [])
        before = len([call for call in self.calls() if call['cmd'] == 'ansible-playbook'])
        result = self.run_step('Verify scheduled automation and idempotence', config_values=config,
                               output_values={'agentWorkspaceKeys': {'main': {'privateKey': ''}},
                                              'workspaceDeployPrivateKey': legacy})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len([call for call in self.calls() if call['cmd'] == 'ansible-playbook']), before)

    def test_staging_provisioning_never_uses_a_default_or_mismatched_host(self):
        scripts = self.root / 'scripts'
        scripts.mkdir()
        provision = scripts / 'provision.sh'
        provision.write_bytes((WORKFLOW.parents[2] / 'scripts/provision.sh').read_bytes())
        provision.chmod(0o700)
        (self.root / 'ansible').mkdir()
        self.env.update({'PROVISION_GATEWAY_TOKEN': 'fixture-gateway',
                         'PROVISION_CLAUDE_SETUP_TOKEN': 'fixture-claude',
                         'PROVISION_AGENT_IDS': 'test'})
        for supplied in ('', 'openclaw-vps', NAME + '-other'):
            with self.subTest(supplied=supplied):
                self.env['PROVISION_TAILSCALE_HOSTNAME'] = supplied
                result = self.run_step('Verify scheduled automation and idempotence')
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(call['cmd'] in ('ansible-playbook', 'ssh') for call in self.calls()))
        self.env['PROVISION_TAILSCALE_HOSTNAME'] = NAME
        self.env['STAGING_HOST'] = 'openclaw-vps.example.ts.net'
        self.assertNotEqual(self.run_step('Verify scheduled automation and idempotence').returncode, 0)
        self.assertFalse(any(call['cmd'] in ('ansible-playbook', 'ssh') for call in self.calls()))
        self.env['STAGING_HOST'] = HOST
        peer = {'HostName': NAME, 'DNSName': HOST + '.', 'Online': True}
        for peers in ({'a': peer, 'b': peer}, {'a': peer | {'DNSName': NAME + '-other.example.ts.net.'}}):
            with self.subTest(peers=peers):
                result = self.run_step('Verify scheduled automation and idempotence',
                                       tailscale={'MagicDNSSuffix': 'example.ts.net', 'Peer': peers})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(call['cmd'] in ('ansible-playbook', 'ssh') for call in self.calls()))

    def test_scheduled_inventory_never_deletes_any_resource(self):
        name = self.inventory_step['name']
        self.steps = self.steps | {name: self.inventory_step}
        self.assertEqual(self.inventory_workflow['concurrency'], self.workflow['concurrency'])
        for options, expected_status in (({}, 0), ({'resources': {'server': [
                {'id': 1, 'name': NAME}, {'id': 2, 'name': 'production-server'}]},
                'key_pages': [[{'id': 3, 'title': NAME}, {'id': 4, 'title': 'other-work'}]]}, 1)):
            result = self.run_step(name, **options)
            self.assertEqual(result.returncode, expected_status, result.stderr)
            self.assertFalse(any('DELETE' in c['args'] or 'delete' in c['args'] for c in self.calls()))

    def test_scheduled_inventory_does_not_treat_errors_as_empty(self):
        name = self.inventory_step['name']
        self.steps = self.steps | {name: self.inventory_step}
        for options in ({'hcloud_exit': 1}, {'key_read_fail': True},
                        {'raw_hcloud_output': ''}, {'raw_hcloud_output': '[]\\n[]'},
                        {'raw_key_output': ''}, {'raw_key_output': '[]'}):
            with self.subTest(options=options):
                self.assertNotEqual(self.run_step(name, **options).returncode, 0)

    def test_ansible_requires_authenticated_host_keys(self):
        self.install_provisioner()
        peer = {'HostName': NAME, 'DNSName': HOST + '.', 'Online': True}
        for keys in (None, [], [''], ['ssh-ed25519 bad\nother-host bad'], 'not-an-array'):
            with self.subTest(keys=keys):
                result = self.run_step('Verify scheduled automation and idempotence',
                    tailscale={'MagicDNSSuffix': 'example.ts.net', 'Peer': {'a': peer | {'sshHostKeys': keys}}})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(call['cmd'] == 'ansible-playbook' for call in self.calls()))

    def test_provisioner_waits_for_authenticated_keys_without_an_extra_ssh_probe(self):
        self.install_provisioner()
        clean = CLEAN_RECAP.replace('changed=18', 'changed=0').replace('=== Provisioning complete ===\n', '')
        result = self.run_step('Verify scheduled automation and idempotence',
                               keys_ready_after=2, idempotence_log=clean)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sum(call['cmd'] == 'sleep' for call in self.calls()), 2)
        self.assertEqual(sum(call['cmd'] == 'ansible-playbook' for call in self.calls()), 1)
        self.assertFalse(any(call['cmd'] == 'ssh' for call in self.calls()))

    def test_provisioner_key_readiness_expires_without_starting_ansible(self):
        self.install_provisioner()
        result = self.run_step('Verify scheduled automation and idempotence', keys_ready_after=100)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sum(call['cmd'] == 'sleep' for call in self.calls()), 29)
        self.assertFalse(any(call['cmd'] in ('ansible-playbook', 'ssh') for call in self.calls()))
        self.assertEqual(list(self.root.glob('tmp.*')), [])

    def test_consumers_use_bound_host_and_smoke_has_no_gateway_token(self):
        smoke = self.steps['Run smoke test']['run']
        self.assertNotIn('GATEWAY_TOKEN', smoke)
        self.assertNotIn('pulumi', smoke)
        self.assertNotIn('tailscale status', smoke)
        for name in ('Trigger workspace git sync', 'Verify scheduled automation and idempotence'):
            code = self.steps[name]['run']
            self.assertIn('tailscale ssh', code)
            self.assertIn('STAGING_HOST', code)
            self.assertNotIn('tailscale status', code)
            self.assertNotIn('accept-new', code)
        self.assertIn('PHOENIX_RESOURCE_NAME', self.steps['Run deployment verification']['run'])


if __name__ == '__main__':
    unittest.main()
