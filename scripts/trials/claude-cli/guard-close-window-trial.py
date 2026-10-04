#!/usr/bin/env python3
"""Real Linux lock/inode regression; Docker, systemd and nft are boundaries."""
import errno
import importlib.machinery
import importlib.util
import json
import fcntl
from pathlib import Path
import signal
import subprocess
import sys
import uuid
from unittest.mock import patch


def load(name,path):
    loader=importlib.machinery.SourceFileLoader(name,str(path))
    spec=importlib.util.spec_from_loader(name,loader)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


home=Path('/home/ubuntu')
scratch=Path(__file__).resolve().parent
installed_guard=Path('/usr/local/lib/openclaw/claude-cli-guard.py')
installed_launcher=home/'.openclaw/claude-cli-container'
guard=load('guard',installed_guard)
launcher=load('launcher',installed_launcher)
table='openclaw_close_'+uuid.uuid4().hex[:8]
state=Path('/run/openclaw-claude-cli')/(table+'.lock')
created=[]
scans=[]
restores=[]
real_open=open
real_flock=fcntl.flock
readers=[]
observations={}
lock_calls=[]
injected_error=None
caught_final_eio=False


def client_probe():
    code='''import importlib.machinery,importlib.util,subprocess,sys
from unittest.mock import patch
loader=importlib.machinery.SourceFileLoader('launcher',sys.argv[1]);s=importlib.util.spec_from_loader('launcher',loader);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
with patch.object(subprocess,'run',return_value=subprocess.CompletedProcess([],0,stdout='active\\n')):
 try:m.create_guarded(['docker','create','fixture'],sys.argv[2],'fixture.service');print('admitted')
 except SystemExit:print('closed')
'''
    probe=subprocess.Popen(['python3','-c',code,str(installed_launcher),table],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    out,err=probe.communicate(timeout=10)
    if probe.returncode:raise RuntimeError('Kernel admission probe failed; fixture diagnostics withheld')
    return out.strip()=='admitted'


def becoming_ready():
    observations['live_admission']=client_probe()
    code="import fcntl,sys;f=open(sys.argv[1]);fcntl.flock(f,fcntl.LOCK_SH);print('holding',flush=True);sys.stdin.readline()"
    reader=subprocess.Popen(['python3','-c',code,str(state)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    readers.append(reader)
    if reader.stdout.readline().strip()!='holding':raise RuntimeError('Lease holder failed')
    signal.raise_signal(signal.SIGTERM)


def flock_with_teardown_probe(stream,mode):
    lock_calls.append(mode)
    if len(lock_calls)==3:
        # An earlier admitted reader still holds SH. New admission must already
        # be revoked before teardown waits for its exclusive creation lease.
        observations['new_admission_closed_before_ex_wait']=not client_probe()
        readers[0].stdin.write('release\n');readers[0].stdin.flush()
    return real_flock(stream,mode)


def boundary(command,**kwargs):
    if command[:2]==['systemctl','is-active']:
        return subprocess.CompletedProcess(command,0,stdout='active\n',stderr='')
    if command[:2]==['docker','create']:created.append(True)
    if command[:2]==['nft','--file']:restores.append(True)
    return subprocess.CompletedProcess(command,0)


class FaultFile:
    def __init__(self,stream):self.stream,self.writes=stream,0
    def __getattr__(self,name):return getattr(self.stream,name)
    def __enter__(self):return self
    def write(self,value):
        self.writes+=1
        if self.writes==3:
            global injected_error
            injected_error=OSError(errno.EIO,'injected final admission failure')
            raise injected_error
        return self.stream.write(value)
    def __exit__(self,*args):
        result=self.stream.__exit__(*args)
        # Stale byte remains 1, the exclusive flock is now closed, and systemd
        # is deliberately still reported active. No Docker operation is real.
        try:launcher.create_guarded(['docker','create','fixture'],table,'fixture.service')
        except SystemExit:pass
        return result


handlers={s:signal.getsignal(s) for s in (signal.SIGTERM,signal.SIGINT)}
try:
    with patch.object(guard,'open',side_effect=lambda *a,**kw:FaultFile(real_open(*a,**kw)),create=True), \
         patch.object(guard,'stop_containers',side_effect=lambda _:scans.append(True)), \
         patch.object(guard,'snapshot',return_value=[]), \
         patch.object(guard,'ready',side_effect=becoming_ready), \
         patch.object(guard.fcntl,'flock',side_effect=flock_with_teardown_probe), \
         patch.object(subprocess,'run',side_effect=boundary):
        try:guard.main('/fixture/policy.nft',table,state)
        except OSError as error:
            if error.errno!=errno.EIO:raise
            caught_final_eio=error is injected_error
    evidence={'final_scans':len(scans),'nft_restores':len(restores),
              'stale_ready_byte':state.read_text()=='1','post_final_scan_create_admitted':bool(created),
              'intended_final_write_eio_observed':caught_final_eio,**observations}
    print(json.dumps(evidence))
    (scratch/'guard-close-window-evidence.json').write_text(json.dumps(evidence)+'\n')
    accepted=(caught_final_eio and len(scans)==2 and len(restores)==2 and evidence['stale_ready_byte']
              and not created and observations.get('live_admission') is True
              and observations.get('new_admission_closed_before_ex_wait') is True)
    if '--expect-closed' in sys.argv and not accepted:raise RuntimeError('Guard lifetime admission regression failed')
finally:
    for reader in readers:
        if reader.poll() is None:reader.kill()
        reader.communicate(timeout=5)
    for sig,handler in handlers.items():signal.signal(sig,handler)
    state.unlink(missing_ok=True)
