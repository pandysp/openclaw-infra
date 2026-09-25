#!/usr/bin/env python3
"""Execute smoke-test.sh, replacing only SSH, OpenClaw and HTTP boundaries."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'smoke-test.sh'
HOST = 'openclaw-staging-123-1.fixture.ts.net'


class SmokeTests(unittest.TestCase):
    def run_smoke(self, case='success', host=HOST, repository='fixture/private-repo', model='anthropic/claude-sonnet-4-6'):
        with tempfile.TemporaryDirectory(prefix='phoenix-smoke-') as tmp:
            root = Path(tmp)
            (root / 'fixture-case').write_text(case)
            state = root / '.openclaw'
            (state / 'identity').mkdir(parents=True)
            (state / 'identity/device.json').write_text('{"deviceId":"fixture-device"}')
            native_args = ['-p','--tools','default' if case == 'native_tools_enabled' else '', '--strict-mcp-config']
            (state / 'openclaw.json').write_text(json.dumps({
                'gateway': {'mode': 'remote' if case == 'remote_gateway' else 'local', 'port': 18789,
                            'auth': {'mode': 'none' if case == 'no_auth' else 'token',
                                     'token': '' if case == 'empty_gateway_token' else 'fixture-secret-gateway'}}, 
                'tools': {'deny': ['fixture_existing_deny'],
                          'allow': ['github_get_file_contents','github-test_get_file_contents'],
                          'alsoAllow': ['group:plugins'] if case == 'unsafe_tool_allowlist' else []},
                'agents': {'defaults': {'model': {'primary': 'anthropic/wrong-model' if case == 'wrong_configured_model' else 'anthropic/claude-sonnet-4-6'},
                            'models': {'anthropic/claude-sonnet-4-6': {'agentRuntime': {'id': 'claude-cli'}}},
                            'cliBackends': {'claude-cli': {'args': native_args, 'resumeArgs': native_args+['--resume','{sessionId}']}}}}, 
                'plugins': {'entries': {'openclaw-mcp-adapter': {'config': {'servers': [
                    {'name': 'github', 'env': {'GITHUB_PERSONAL_ACCESS_TOKEN': 'fixture-secret-pat'}}
                ]}}}}
            }))
            original_config = json.loads((state/'openclaw.json').read_text())
            # The real gateway pins the config it started with: tools.* and
            # agents.* file edits are reload class "none" and never swap the
            # runtime snapshot (openclaw 2026.6.6, src/gateway/config-reload-plan.ts).
            (root / 'gateway-snapshot.json').write_text((state/'openclaw.json').read_text())
            preload = root / 'http.mjs'
            preload.write_text('''
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
// Virtual clock: waits complete instantly but still advance Date.now, so the
// script's real deadlines are exercised without real sleeping.
let clockOffset = 0;
const realNow = Date.now.bind(Date);
Date.now = () => realNow() + clockOffset;
const realSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = (callback, ms = 0, ...rest) => { clockOffset += ms; return realSetTimeout(callback, 0, ...rest); };
globalThis.fetch = async (url, options = {}) => {
  const mode = process.env.FIXTURE_CASE;
  if (url === 'https://api.github.com/repos/fixture/private-repo') {
    if (options.headers?.Authorization) {
      assert.equal(options.headers.Authorization, 'Bearer fixture-secret-pat');
      return {status: 200, json: async () => ({private: mode !== 'public_repo', full_name: 'fixture/private-repo'})};
    }
    return {status: mode === 'anonymous_allowed' ? 200 : 404};
  }
  assert.equal(url, 'http://127.0.0.1:18789/tools/invoke');
  if (!options.headers.Authorization) return {status: mode === 'unauthenticated_gateway_allowed' ? 200 : 401};
  assert.equal(options.headers.Authorization, 'Bearer fixture-secret-gateway');
  const body = JSON.parse(options.body);
  assert(['github_get_file_contents', 'github-test_get_file_contents'].includes(body.tool));
  assert.equal(body.args.repo, 'private-repo');
  const refused = os.homedir()+'/gateway-restarting';
  if (fs.existsSync(refused)) { fs.rmSync(refused); throw new TypeError('fetch failed'); }
  const config = JSON.parse(fs.readFileSync(os.homedir()+'/gateway-snapshot.json','utf8'));
  if (config.tools?.deny?.includes('*') && mode !== 'tools_not_disabled') return {status:404};
  return {status: mode === 'mcp_failure' ? 500 : 200, json: async () => ({ok: true, result: {
    isError: mode === 'mcp_error_result', content: [{type: 'text', text: mode === 'invalid_mcp_json' ? 'fixture-secret-invalid' : '[{"type":"file"}]'}]
  }})};
};
''')
            tools = {
                'tailscale': '''import os,pathlib,shlex,subprocess,sys
(pathlib.Path(os.environ['HOME'])/'ssh-called').touch()
assert sys.argv[1:3] == ['ssh','ubuntu@openclaw-staging-123-1.fixture.ts.net']
raise SystemExit(subprocess.run(shlex.split(sys.argv[3]), input=sys.stdin.read(), text=True).returncode)
''',
                'hostname': "print('openclaw-staging-123-1')\n",
                'systemctl': '''import os,pathlib,shutil,sys
root=pathlib.Path(os.environ['HOME'])
assert sys.argv[1:]==['--user','restart','openclaw-gateway'], sys.argv
assert os.environ.get('XDG_RUNTIME_DIR','').startswith('/run/user/')
shutil.copyfile(root/'.openclaw/openclaw.json', root/'gateway-snapshot.json')
(root/'gateway-restarting').touch()
with (root/'restarts').open('a') as f: f.write('restart\\n')
''',
                'claude': '''import os,pathlib,sys
if sys.argv[1:]==['--help']:print('--tools --strict-mcp-config')
else:
 assert sys.argv[-3:]==['--tools','','--strict-mcp-config'], 'Native tools were not disabled'
 (pathlib.Path(os.environ['HOME'])/'native-tool-free').touch()
''',
                'openclaw': '''import json,os,pathlib,subprocess,sys
args=sys.argv[1:];root=pathlib.Path(os.environ['HOME']);mode=(root/'fixture-case').read_text()
assert 'OPENCLAW_GATEWAY_URL' not in os.environ
assert 'OPENCLAW_GATEWAY_TOKEN' not in os.environ
assert os.environ['OPENCLAW_CONFIG_PATH']==str(root/'.openclaw/openclaw.json')
assert os.environ['OPENCLAW_STATE_DIR']==str(root/'.openclaw')
assert 'OPENCLAW_PROFILE' not in os.environ
(root/'cli-called').touch()
assert 'fixture-secret' not in ' '.join(args), 'Secret reached command arguments'
if args[:2]==['devices','list']:
 paired=(root/'approved').exists() or mode=='already_paired'
 pending=[] if paired else [{'deviceId':'fixture-device','requestId':'own-request'}]
 pending += [{'deviceId':'another-device','requestId':'other-request'}]
 if mode=='ambiguous':pending += [{'deviceId':'fixture-device','requestId':'duplicate-request'}]
 print(json.dumps({'pending':pending,'paired':[{'deviceId':'fixture-device'}] if paired else []}))
elif args[:2]==['devices','approve']:
 assert args[2]=='own-request', 'Approved another device'
 if mode=='approval_failed':print('fixture-secret-error');raise SystemExit(1)
 (root/'approved').touch();print('{}')
elif args[:3]==['gateway','call','agent']:
 config=json.loads((root/'gateway-snapshot.json').read_text())
 assert config['tools']['deny']==['*'], 'Inference still has tools'
 subprocess.run([config['agents']['defaults']['cliBackends']['claude-cli']['command']],check=True,capture_output=True)
 assert (root/'native-tool-free').exists()
 assert '--expect-final' in args and args[args.index('--timeout')+1]=='150000'
 params=json.loads(args[args.index('--params')+1]);assert params['deliver'] is False and params['timeout']==120
 assert params['sessionKey']=='agent:main:phoenix-'+params['idempotencyKey']
 marker=params['message'].split('nothing else: ')[1]
 assert marker=='PHOENIX_INFERENCE_'+params['idempotencyKey']
 if mode=='model_command_failed':print('fixture-secret-error');raise SystemExit(1)
 result={'payloads':[{'text': 'wrong' if mode=='wrong_reply' else marker}],
 'meta':{'agentMeta':{'provider':'claude-cli','model':'wrong-model' if mode=='wrong_model' else 'claude-sonnet-4-6','usage':{'output':'1' if mode=='invalid_usage' else 0 if mode=='no_model_usage' else 1}}}}
 if mode=='extra_reply':result['payloads'].append({'text':'extra text'})
 print(json.dumps(result if mode=='embedded_fallback' else {'status':'ok','result':result}))
else:raise AssertionError('Unexpected command')
''',
            }
            for name, body in tools.items():
                file = root / name
                file.write_text('#!/usr/bin/env python3\n' + body)
                file.chmod(0o700)
            env = {'PATH': str(root) + ':' + os.environ['PATH'], 'HOME': tmp,
                   'STAGING_HOST': host, 'STAGING_PRIVATE_REPOSITORY': repository, 'STAGING_MODEL': model,
                   'OPENCLAW_GATEWAY_URL': 'ws://wrong-host:18789', 'OPENCLAW_PROFILE': 'wrong-profile',
                   'OPENCLAW_CONFIG_PATH': '/wrong/config', 'OPENCLAW_STATE_DIR': '/wrong/state',
                   'NODE_OPTIONS': '--import=' + str(preload), 'FIXTURE_CASE': case}
            result = subprocess.run(['bash', str(SCRIPT)], env=env, capture_output=True, text=True, timeout=15)
            self.assertNotIn('fixture-secret', result.stdout + result.stderr)
            result.ssh_called = (root / 'ssh-called').exists()
            result.cli_called = (root / 'cli-called').exists()
            restored = json.loads((state/'openclaw.json').read_text())
            self.assertEqual(restored['tools'], original_config['tools'])
            self.assertEqual(restored['agents']['defaults']['cliBackends'], original_config['agents']['defaults']['cliBackends'])
            # The running gateway, not just the file, must be back on the original policy.
            running = json.loads((root / 'gateway-snapshot.json').read_text())
            self.assertEqual(running['tools'], original_config['tools'])
            self.assertEqual(running['agents']['defaults']['cliBackends'], original_config['agents']['defaults']['cliBackends'])
            return result

    def test_pairing_inference_and_private_reads(self):
        for case in ('success', 'already_paired'):
            with self.subTest(case=case):
                result = self.run_smoke(case)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('Private MCP directory read for test', result.stdout)
                self.assertIn('Smoke test passed', result.stdout)
                self.assertRegex(result.stdout, r'Inference run: [0-9a-f-]{36}')

    def test_failures_never_pass_or_print_secret_output(self):
        for case in ('ambiguous', 'approval_failed', 'model_command_failed',
                     'wrong_reply', 'extra_reply', 'wrong_model', 'wrong_configured_model', 'remote_gateway',
                     'no_auth', 'empty_gateway_token', 'unauthenticated_gateway_allowed',
                     'embedded_fallback', 'no_model_usage', 'invalid_usage', 'public_repo',
                     'anonymous_allowed', 'mcp_failure', 'mcp_error_result', 'invalid_mcp_json', 'tools_not_disabled'):
            with self.subTest(case=case):
                result = self.run_smoke(case)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('Smoke test passed', result.stdout)
                if case in ('wrong_configured_model', 'remote_gateway', 'no_auth', 'empty_gateway_token'):
                    self.assertFalse(result.cli_called)
                if case == 'model_command_failed':
                    self.assertIn('CLI exit 1', result.stderr)
                    self.assertRegex(result.stdout, r'Inference run: [0-9a-f-]{36}')
                if case == 'mcp_failure':
                    self.assertIn('HTTP 500', result.stderr)

    def test_write_capabilities_outside_inference_window_are_rejected(self):
        for case in ['native_tools_enabled','unsafe_tool_allowlist']:
            with self.subTest(case=case):
                result = self.run_smoke(case)
                self.assertNotEqual(result.returncode,0)
                self.assertFalse(result.cli_called)

    def test_missing_fixture_or_non_staging_host_fails_before_ssh(self):
        for values in ({'repository': ''}, {'host': 'openclaw-vps.fixture.ts.net'}, {'model': ''}, {'model': 'bad;model'}):
            result = self.run_smoke(**values)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(result.ssh_called)


if __name__ == '__main__':
    unittest.main()
