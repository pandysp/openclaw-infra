#!/usr/bin/env python3
"""Real gateway cancellation and forced launcher death; require actual live Bash."""
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid

mode = sys.argv[1] if len(sys.argv) == 2 else 'chat'
if mode not in {'chat', 'kill'}:
    raise SystemExit('Usage: gateway-cancellation-trial.py [chat|kill]')
home = Path.home()
scratch = Path(__file__).resolve().parent
config = home / '.openclaw/openclaw.json'
before = json.loads(config.read_text())
if before['agents']['defaults'].get('cliBackends') is not None:
    raise RuntimeError('Requires native production backend')
runtime = json.loads((home / '.openclaw/claude-cli-runtime.json').read_text())
run = uuid.uuid4().hex
key = 'agent:main:cwrapper-cancel-' + run
hashed = hashlib.sha256(key.encode()).hexdigest()[:12]
unit = 'openclaw-cancel-rollback-' + run[:8]
workspace = Path(before['agents']['defaults']['workspace'])
project = home / '.claude/projects' / re.sub(r'[^A-Za-z0-9]', '-', str(workspace))
native_id = owner_fd = None
marker = scratch / 'cancel-cleanup-started'
marker.unlink(missing_ok=True)
shim = scratch / 'cancel-bin/docker'
if shutil.which('docker', path=str(shim.parent) + ':' + os.environ['PATH']) != str(shim):
    raise RuntimeError('Cancellation fault injector is not executable or not selected')


def cmd(args, timeout=40):
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE, timeout=timeout)


def rpc(method, params, timeout=30000):
    output = cmd(['openclaw', 'gateway', 'call', method, '--json', '--timeout', str(timeout),
                  '--params', json.dumps(params)], timeout=timeout / 1000 + 15)
    return json.loads(output[output.find('{'):])


def healthy():
    for _ in range(30):
        try:
            if rpc('health', {}, 3000).get('ok'):
                return
        except subprocess.CalledProcessError:
            pass  # Expected during the bounded gateway startup window.
        time.sleep(1)
    raise RuntimeError('Gateway failed to become healthy')


def container_state(name):
    inspected = subprocess.run(['docker', 'inspect', '--format', '{{json .State}}', name],
                               text=True, capture_output=True, timeout=5)
    if inspected.returncode:
        missing = inspected.stderr.lower()
        if ('no such object: ' + name) in missing or ('no such container: ' + name) in missing:
            return None
        raise RuntimeError('Scoped Docker inspection failed; private diagnostics withheld')
    return json.loads(inspected.stdout)


checker = '''import json,os,pathlib
pids=[]
for p in pathlib.Path('/proc').iterdir():
 if not p.name.isdigit() or int(p.name)==os.getpid():continue
 try:
  if p.joinpath('comm').read_text().strip()!='python3':continue
  argv=p.joinpath('cmdline').read_bytes().split(b'\\0')
  if len(argv)>=3 and argv[0].rsplit(b'/',1)[-1]==b'python3' and argv[1:3]==[b'-c',b'import time; time.sleep(120)']:pids.append(int(p.name))
 except (FileNotFoundError,ProcessLookupError):pass
print(json.dumps(pids))
'''


def sleep_pids(name):
    return json.loads(cmd(['docker', 'exec', name, 'python3', '-c', checker]))


cron = rpc('cron.list', {'includeDisabled': True})
if any(job.get('state', {}).get('runningAtMs') for job in cron.get('jobs', [])):
    raise RuntimeError('Scheduled job running; no restart')
cmd(['systemd-run', '--user', '--unit', unit, '--on-active=5m', '--timer-property=AccuracySec=1s',
     '/usr/bin/python3', str(scratch / 'trial-backend.py'), 'restore'])
try:
    cmd(['python3', str(scratch / 'trial-backend.py'), 'apply'])
    healthy()
    params = {'agentId': 'main', 'sessionKey': key, 'idempotencyKey': str(uuid.uuid4()),
              'deliver': False, 'timeoutMs': 180000,
              'message': f'Operator cancellation fixture {run}. Use native Bash to run exactly python3 -c "import time; time.sleep(120)" with timeout 180000, in the foreground. Do not read or change files, do not contact anyone. Reply done after it completes.'}
    if rpc('chat.send', params).get('runId') != params['idempotencyKey']:
        raise RuntimeError('Gateway returned an unexpected chat run')
    deadline = time.monotonic() + 120
    name = None
    while time.monotonic() < deadline:
        names = cmd(['docker', 'ps', '--format', '{{.Names}}', '--filter',
                     'label=openclaw.claude-session=' + hashed]).split()
        if names:
            name = names[0]
            events = [json.loads(line) for line in cmd(['docker', 'logs', name]).splitlines() if line.startswith('{')]
            bash = False
            for event in events:
                if event.get('type') == 'system' and event.get('subtype') == 'init':
                    native_id = event['session_id']
                    if not isinstance(native_id, str) or not re.fullmatch(r'[A-Za-z0-9-]+', native_id):
                        raise RuntimeError('Invalid scoped native session ID')
                if event.get('type') == 'assistant':
                    for block in event.get('message', {}).get('content', []):
                        if isinstance(block, dict) and block.get('type') == 'tool_use' and block.get('name') == 'Bash':
                            inputs = block.get('input', {})
                            bash |= inputs.get('run_in_background') is not True and shlex.split(inputs.get('command', '')) == ['python3', '-c', 'import time; time.sleep(120)']
            if bash and sleep_pids(name):
                break
        time.sleep(.2)
    else:
        raise RuntimeError('Gateway fixture never reached a real running foreground sleep')

    owner, started = cmd(['docker', 'inspect', '--format', '{{index .Config.Labels "openclaw.claude-owner"}}', name]).strip().split(':')
    owner_fd = os.pidfd_open(int(owner))
    fields = Path('/proc', owner, 'stat').read_text().rsplit(')', 1)[1].split()
    if fields[19] != started or fields[0].upper() in {'Z', 'X'}:
        raise RuntimeError('Fixture launcher ownership changed; refusing host signals')
    mounts = json.loads(cmd(['docker', 'inspect', '--format', '{{json .Mounts}}', name]))
    artifacts = {Path(binding['Source']).parent for binding in mounts if binding['Source'].startswith('/tmp/openclaw-claude-cli-')}
    prefix = f"openclaw-claude-cli-{runtime['guard_table']}-{os.getuid()}-{owner}-{started}-"
    if not artifacts or any(path.parent != Path('/tmp') or not path.name.startswith(prefix) or path.resolve() != path for path in artifacts):
        raise RuntimeError('Fixture did not capture correctly owned host artifacts')

    beginning = time.monotonic()
    if mode == 'chat':
        if not rpc('chat.abort', {'sessionKey': key, 'agentId': 'main', 'runId': params['idempotencyKey']}).get('aborted'):
            raise RuntimeError('Gateway did not acknowledge cancellation of this exact run')
    else:
        signal.pidfd_send_signal(owner_fd, signal.SIGKILL)
    deadline = time.monotonic() + 30
    while True:
        state = container_state(name)
        if state is None:
            break
        if time.monotonic() > deadline:
            raise RuntimeError('Actual cancelled or orphaned container survived its cleanup deadline')
        time.sleep(.1)
    deadline = time.monotonic() + 15
    while not select.select([owner_fd], [], [], 0)[0] or any(path.exists() for path in artifacts):
        if time.monotonic() > deadline:
            raise RuntimeError('Launcher or its owned host artifacts survived cleanup')
        time.sleep(.1)
    evidence = {'mode': mode, 'foreground_sleep_observed_before_stop': True,
                'actual_gateway_abort': mode == 'chat', 'forced_launcher_sigkill_tested': mode == 'kill',
                'slow_client_cleanup_entered': marker.exists(), 'actual_container_removed': True,
                'launcher_exited': True, 'owned_host_artifacts_removed': True,
                'seconds_until_all_gone': round(time.monotonic() - beginning, 3)}
    print(json.dumps(evidence))
    (scratch / f'gateway-{mode}-cleanup-evidence.json').write_text(json.dumps(evidence) + '\n')
    if mode == 'chat' and not marker.exists():
        raise RuntimeError('Slow-client fault was not exercised during actual chat cancellation')
finally:
    if owner_fd is not None:
        os.close(owner_fd)
    cmd(['python3', str(scratch / 'trial-backend.py'), 'restore'])
    healthy()
    cmd(['systemctl', '--user', 'stop', unit + '.timer'])
    rpc('sessions.delete', {'key': key})
    for identity in cmd(['docker', 'ps', '-aq', '--filter', 'label=openclaw.claude-session=' + hashed]).split():
        cmd(['docker', 'rm', '-f', identity])
    if native_id:
        transcript = project / (native_id + '.jsonl')
        if transcript.exists():
            if run not in transcript.read_text():
                raise RuntimeError('Refusing to remove an unmarked native transcript')
            transcript.unlink()
    marker.unlink(missing_ok=True)
    if json.loads(config.read_text()) != before:
        raise RuntimeError('Unrelated config changed; inspect privately')
    print('backend=original; gateway=healthy')
