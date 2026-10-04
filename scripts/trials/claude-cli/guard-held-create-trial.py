#!/usr/bin/env python3
"""Real Docker create lease versus installed guard teardown; no gateway/auth use.

Usage: guard-held-create-trial.py LAUNCHER.py
The fixture holds the client return after real stopped creation. The guard must
revoke admission but wait for that creation lease; the late result never starts.
"""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import uuid

source = Path(sys.argv[1]).resolve(strict=True)
runtime = json.loads((Path.home() / '.openclaw/claude-cli-runtime.json').read_text())
table, service = runtime['guard_table'], runtime['guard_service']
name = 'c-held-create-' + uuid.uuid4().hex[:12]
root = Path(tempfile.mkdtemp(prefix=name + '-'))
child = stop = owner_fd = owner_start = None


def command(args, timeout=30):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('Held-create fixture command failed; diagnostics withheld')
    return result.stdout.strip()


def state():
    result = subprocess.run(['docker', 'inspect', '--format', '{{json .State}}', name], capture_output=True, text=True, timeout=5)
    if result.returncode == 0:
        return json.loads(result.stdout)
    if ('no such object: ' + name) in result.stderr.lower() or ('no such container: ' + name) in result.stderr.lower():
        return None
    raise RuntimeError('Scoped Docker inspection failed')


code = '''import importlib.machinery,importlib.util,os,pathlib,subprocess,sys,time
source,name,root,table,service=sys.argv[1:];root=pathlib.Path(root)
loader=importlib.machinery.SourceFileLoader('launcher',source);s=importlib.util.spec_from_loader('launcher',loader);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
run=m.subprocess.run;owner=str(os.getpid())+':'+pathlib.Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19]
def held(args,**kw):
 result=run(args,**kw)
 if args[:2]==['docker','create']:
  root.joinpath('created').write_text(owner)
  while not root.joinpath('release').exists():time.sleep(.05)
 return result
m.subprocess.run=held
create=['docker','create','--name',name,'--network','none','--user','1000:1000','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--label','openclaw.claude-guard='+table,'--label','openclaw.claude-owner='+owner,'--entrypoint','/bin/sleep','openclaw-claude-cli:latest','120']
# Successful prepare would invoke docker start; recording the attach command
# wrapper distinguishes refusal from merely starting and quickly removing it.
popen=m.subprocess.Popen
def observed(args,**kw):
 if args[:2]==['docker','start']:root.joinpath('start-attempted').write_text('1')
 return popen(args,**kw)
m.subprocess.Popen=observed
raise SystemExit(m.run_container(['docker','start','--attach',name],name,prepare=lambda:m.create_guarded(create,table,service)))
'''
try:
    if command(['docker', 'ps', '-aq', '--filter', 'label=openclaw.claude-guard=' + table]):
        raise RuntimeError('Refusing guard teardown while other guarded runtimes exist')
    command(['systemctl', 'is-active', service])
    child = subprocess.Popen(['python3', '-c', code, str(source), name, str(root), table, service],
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    owner_start = Path('/proc/' + str(child.pid) + '/stat').read_text().rsplit(')', 1)[1].split()[19]
    deadline = time.monotonic() + 15
    while not (root / 'created').exists():
        if child.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError('Held-create fixture never acquired admission and created its stopped container')
        time.sleep(.05)
    pid, started = (root / 'created').read_text().split(':')
    if int(pid) != child.pid:
        raise RuntimeError('Held-create fixture owner mismatch')
    owner_fd = os.pidfd_open(child.pid)
    if Path('/proc/' + pid + '/stat').read_text().rsplit(')', 1)[1].split()[19] != started:
        raise RuntimeError('Held-create fixture PID reused')
    current = state()
    if not current or current['Running']:
        raise RuntimeError('Fixture did not reach real stopped creation')
    stop = subprocess.Popen(['sudo', '-n', 'systemctl', 'stop', service], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    admission = Path('/run/openclaw-claude-cli') / (table + '.lock')
    deadline = time.monotonic() + 10
    while admission.read_text() != '0':
        if stop.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError('Guard never revoked admission during held create')
        time.sleep(.05)
    waited = stop.poll() is None
    (root / 'release').write_text('1')
    child.communicate(timeout=35)
    stop.communicate(timeout=100)
    if stop.returncode:
        raise RuntimeError('Installed guard teardown failed')
    evidence = {'real_stopped_create_observed': True, 'teardown_waited_for_creation_lease': waited,
                'late_creation_never_started': not (root / 'start-attempted').exists(),
                'container_removed': state() is None, 'launcher_refused_after_revocation': child.returncode != 0,
                'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
    if not all(v for k, v in evidence.items() if k != 'source_sha256'):
        raise RuntimeError('Held-create teardown acceptance failed')
    print(json.dumps(evidence))
    Path(__file__).with_name('guard-held-create-evidence.json').write_text(json.dumps(evidence) + '\n')
finally:
    if child is not None and child.poll() is None:
        if owner_fd is None:
            owner_fd = os.pidfd_open(child.pid)
        if Path('/proc/' + str(child.pid) + '/stat').read_text().rsplit(')', 1)[1].split()[19] != owner_start:
            raise RuntimeError('Refusing cleanup kill after fixture ownership changed')
        signal.pidfd_send_signal(owner_fd, signal.SIGKILL)
        child.communicate(timeout=5)
    if owner_fd is not None:
        os.close(owner_fd)
    if stop is not None:
        stop.communicate(timeout=100)
    try:
        if state() is not None:
            command(['docker', 'rm', '-f', name])
    finally:
        command(['sudo', '-n', 'systemctl', 'start', service], timeout=110)
        import shutil
        shutil.rmtree(root)
