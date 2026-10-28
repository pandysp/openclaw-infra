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
            state.mkdir()
            (state / 'claude-cli-runtime.json').write_text(json.dumps(
                {'extra_args': [] if case == 'native_tools_enabled' else ['--tools', '']}))
            (state / 'openclaw.json').write_text(json.dumps({
                'gateway': {'mode': 'remote' if case == 'remote_gateway' else 'local', 'port': 18789,
                            'auth': {'mode': 'none' if case == 'no_auth' else 'token',
                                     'token': '' if case == 'empty_gateway_token' else 'fixture-secret-gateway'}}, 
                'tools': {'deny': ['fixture_existing_deny'],
                          'allow': ['github_get_file_contents','github-test_get_file_contents'],
                          'alsoAllow': ['group:plugins'] if case == 'unsafe_tool_allowlist' else []},
                'agents': {'defaults': {'model': {'primary': 'anthropic/wrong-model' if case == 'wrong_configured_model' else 'anthropic/claude-sonnet-4-6'},
                            'models': {'anthropic/claude-sonnet-4-6': {'agentRuntime': {'id': 'claude-cli'}}},
                            'workspace': str(root / 'workspace')},
                           'entries': {'main': {}}},
                'plugins': {'entries': {'openclaw-mcp-adapter': {'config': {'servers': [
                    {'name': 'github', 'env': {'GITHUB_PERSONAL_ACCESS_TOKEN': 'fixture-secret-pat'}}
                ]}}}}
            }))
            (root / 'workspace').mkdir()
            original_config = json.loads((state/'openclaw.json').read_text())
            # The real gateway pins the config it started with: tools.* file
            # edits are reload class "none" and never swap the runtime snapshot (OpenClaw at openclaw_docs_reviewed_version,
            # src/gateway/config-reload-plan.ts).
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
  if (fs.existsSync(refused)) {
    if (mode !== 'policy_timeout_forever') fs.rmSync(refused);
    if (mode.startsWith('policy_timeout')) throw new DOMException('fixture-secret-timeout', 'TimeoutError');
    if (mode === 'policy_abort') throw new DOMException('fixture-secret-abort', 'AbortError');
    throw new TypeError('fetch failed');
  }
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
assert os.environ.get('XDG_RUNTIME_DIR','').startswith('/run/user/')
if sys.argv[1:]==['--user','start','openclaw-memory-sources.service']:
 # Fixture copy job: mirrors notes/*.txt like the memory-sources container.
 notes=root/'workspace/notes';copies=root/'.openclaw/memory-sources/main/notes';copies.mkdir(parents=True,exist_ok=True)
 if (root/'fixture-case').read_text()!='memory_copy_missing':
  for source in notes.glob('*.txt'):(copies/(source.name+'.md')).write_text('# notes/'+source.name+'\\n\\n'+source.read_text())
 for copy in copies.glob('*.md'):
  if not (notes/copy.name[:-3]).exists():copy.unlink()
 raise SystemExit(0)
if sys.argv[1:3]==['--user','list-units']:
 if sys.argv[-1]=='workspace-git-sync-*.service':raise SystemExit(0)
 assert sys.argv[-1]=='workspace-git-sync-*.timer';print('workspace-git-sync-main.timer loaded active waiting Hourly');raise SystemExit(0)
if sys.argv[1:3] in (['--user','stop'],['--user','start']) and sys.argv[3:]==['workspace-git-sync-main.timer']:
 with (root/'sync-timers').open('a') as f: f.write(sys.argv[2]+'\\n')
 raise SystemExit(0)
assert sys.argv[1:]==['--user','restart','openclaw-gateway'], sys.argv
shutil.copyfile(root/'.openclaw/openclaw.json', root/'gateway-snapshot.json')
(root/'gateway-restarting').touch()
with (root/'restarts').open('a') as f: f.write('restart\\n')
''',
                '.openclaw/claude-cli-container': '''import json,os,pathlib,sys,uuid
# Fixture launcher: logs the container it would start and writes Claude's transcript.
import re
home=pathlib.Path(os.environ['HOME']);session=str(uuid.uuid4())
workspace=json.loads((home/'.openclaw/openclaw.json').read_text())['agents']['defaults']['workspace']
transcripts=home/'.claude/projects'/re.sub('[^A-Za-z0-9]','-',workspace);transcripts.mkdir(parents=True,exist_ok=True)
events=[{'message':{'content':[{'type':'text','text':sys.argv[1]}]}}]+(json.loads(sys.argv[2]) if len(sys.argv)>2 else [])
(transcripts/(session+'.jsonl')).write_text(''.join(json.dumps(event)+'\\n' for event in events))
if (home/'fixture-case').read_text()!='no_container_launch':
 with (home/'.openclaw/claude-cli-invocations.jsonl').open('a') as f:
  f.write(json.dumps({'agent':'main','session':session,'name':'openclaw-claude-fixture','resuming':False})+'\\n')
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
if args[:1]==['devices']:
 raise SystemExit('A local token-authenticated CLI needs no pairing')
elif args==['memory','status','--agent','main','--deep','--json']:
 print('[memory] status log line before JSON');print(json.dumps([{'agentId':'main','status':{'provider':'deepinfra','model':'google/embeddinggemma-2'},'embeddingProbe':{'ok':True}}]))
elif args==['memory','index','--agent','main']:
 print('[memory] indexed')
elif args[:3]==['memory','search','--query']:
 assert args[4:]==['--agent','main','--max-results','5','--json'], args
 hits=[{'path':str(copy),'snippet':copy.read_text()} for copy in (root/'.openclaw/memory-sources/main/notes').glob('*.md') if args[3] in copy.read_text()]
 print(json.dumps({'results':[] if mode=='search_misses' else hits}))
elif args[:3]==['gateway','call','agent'] and 'phoenix-tool-' in args[args.index('--params')+1]:
 config=json.loads((root/'gateway-snapshot.json').read_text())
 assert '*' not in config['tools'].get('deny',[]), 'Tool turn ran with tools disabled'
 params=json.loads(args[args.index('--params')+1]);assert params['deliver'] is False
 assert 'github_get_file_contents' in params['message'] and 'private-repo' in params['message']
 calls=[] if mode=='tool_not_called' else [{'message':{'content':[{'type':'tool_use','id':'t1','name':'mcp__openclaw__github_get_file_contents','input':{}}]}},
  {'message':{'content':[{'type':'tool_result','tool_use_id':'t1','content':[{'type':'text','text':'README.md'}]}]}}]
 subprocess.run([str(root/'.openclaw/claude-cli-container'),params['message'],json.dumps(calls)],check=True,capture_output=True)
 print(json.dumps({'status':'ok','result':{'payloads':[{'text':'LICENSE' if mode=='tool_turn_wrong' else 'README.md'}]}}))
elif args[:3]==['gateway','call','agent'] and 'phoenix-shell-' in args[args.index('--params')+1]:
 import hashlib,re
 config=json.loads((root/'gateway-snapshot.json').read_text())
 assert config['tools']['deny']==['*'], 'The shell turn must not reach OpenClaw tools'
 runtime=json.loads((root/'.openclaw/claude-cli-runtime.json').read_text())
 assert runtime['extra_args']==['--tools','Bash'], 'Claude did not get exactly its Bash tool'
 assert (root/'sync-timers').read_text().splitlines()[-1]=='stop', 'Backups ran during the shell window'
 params=json.loads(args[args.index('--params')+1]);assert params['deliver'] is False
 marker=re.search(r'PHOENIX_EXEC_[0-9a-f-]+',params['message']).group(0)
 canary_path=re.search(r'cat (\\S+)',params['message']).group(1);canary=pathlib.Path(canary_path).read_text()
 digest=hashlib.sha256(marker.encode()).hexdigest()
 def use(i,command):return {'message':{'content':[{'type':'tool_use','id':i,'name':'Bash','input':{'command':command}}]}}
 def result(i,text,error=False):return {'message':{'content':[{'type':'tool_result','tool_use_id':i,'content':text,'is_error':error}]}}
 calls=[use('s1','printf %s '+marker+' | sha256sum'),result('s1','' if mode=='shell_missing' else digest+'  -')]
 if mode!='no_cat':calls+=[use('s2','cat '+canary_path),result('s2',canary if mode=='canary_leak' else 'cat: No such file or directory',mode!='canary_leak')]
 subprocess.run([str(root/'.openclaw/claude-cli-container'),params['message'],json.dumps(calls)],check=True,capture_output=True)
 if mode=='extra_launch':subprocess.run([str(root/'.openclaw/claude-cli-container'),'heartbeat'],check=True,capture_output=True)
 if mode=='workspace_changed':(root/'workspace/written.txt').write_text('x')
 lines=[] if mode=='shell_missing' else [digest]
 lines.append('MISSING')
 print(json.dumps({'status':'ok','result':{'payloads':[{'text':'\\n'.join(lines)}]}}))
elif args[:3]==['gateway','call','agent']:
 config=json.loads((root/'gateway-snapshot.json').read_text())
 assert config['tools']['deny']==['*'], 'Inference still has tools'
 assert '--expect-final' in args and args[args.index('--timeout')+1]=='150000'
 params=json.loads(args[args.index('--params')+1]);assert params['deliver'] is False and params['timeout']==120
 subprocess.run([str(root/'.openclaw/claude-cli-container'),params['message']],check=True,capture_output=True)
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
            # The shell window never outlives the smoke test, even when it fails.
            self.assertEqual(json.loads((state / 'claude-cli-runtime.json').read_text())['extra_args'],
                             [] if case == 'native_tools_enabled' else ['--tools', ''])
            self.assertEqual(list(state.glob('phoenix-canary-*')), [])
            result.timers = (root / 'sync-timers').read_text().splitlines() if (root / 'sync-timers').exists() else []
            # The test note and its copy never outlive the smoke test, even when it fails.
            self.assertEqual(list((root / 'workspace/notes').glob('phoenix-*')) if (root / 'workspace/notes').exists() else [], [])
            result.cli_called = (root / 'cli-called').exists()
            restored = json.loads((state/'openclaw.json').read_text())
            self.assertEqual(restored['tools'], original_config['tools'])
            # The running gateway, not just the file, must be back on the original policy.
            running = json.loads((root / 'gateway-snapshot.json').read_text())
            self.assertEqual(running['tools'], original_config['tools'])
            return result

    def test_inference_and_private_reads(self):
        for case in ('success', 'policy_timeout_once'):
            with self.subTest(case=case):
                result = self.run_smoke(case)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('Private MCP directory read for test', result.stdout)
                self.assertIn('Smoke test passed', result.stdout)
                self.assertRegex(result.stdout, r'Inference run: [0-9a-f-]{36}')
                self.assertEqual(result.timers, ['stop', 'start'], 'backups must resume after a clean shell window')

    def test_failures_never_pass_or_print_secret_output(self):
        for case in ('model_command_failed',
                     'wrong_reply', 'extra_reply', 'wrong_model', 'wrong_configured_model', 'remote_gateway',
                     'no_auth', 'empty_gateway_token', 'unauthenticated_gateway_allowed',
                     'embedded_fallback', 'no_model_usage', 'invalid_usage', 'public_repo',
                     'anonymous_allowed', 'mcp_failure', 'mcp_error_result', 'invalid_mcp_json', 'tools_not_disabled',
                     'no_container_launch', 'policy_timeout_forever', 'policy_abort',
                     'memory_copy_missing', 'search_misses', 'tool_turn_wrong',
                     'shell_missing', 'canary_leak', 'no_cat', 'extra_launch', 'workspace_changed',
                     'tool_not_called'):
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
                if case in ('extra_launch', 'workspace_changed'):
                    self.assertEqual(result.timers, ['stop'], 'backups must stay paused after an uncontained shell window')
                if case == 'policy_timeout_forever':
                    self.assertIn('120 s deadline exceeded', result.stderr)

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
