#!/usr/bin/env python3
"""Actual Docker suffix/guard service: a stopped guard cannot authorize late create."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

service = 'openclaw-claude-cli-network.service'
table = 'openclaw_claude_cli'
source = Path.home() / '.openclaw/claude-cli-container'
name = 'openclaw-claude-' + uuid.uuid4().hex[:12]
code = '''import importlib.machinery,importlib.util,json,os,pathlib,sys
loader=importlib.machinery.SourceFileLoader('launcher',sys.argv[1]);s=importlib.util.spec_from_loader('launcher',loader);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
name=sys.argv[2]
owner=str(os.getpid())+':'+pathlib.Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19]
command=['docker','create','--rm','--name',name,'--label','openclaw.claude-guard=openclaw_claude_cli','--label','openclaw.claude-owner='+owner,'--network','none','--user','1000:1000','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--entrypoint','/bin/sleep','openclaw-claude-cli:latest','120']
print('paused-before-admission',flush=True)
sys.stdin.readline()
raise SystemExit(m.run_container(['docker','start','--attach',name],name,prepare=lambda:m.create_guarded(command,'openclaw_claude_cli','openclaw-claude-cli-network.service')))
'''
child = subprocess.Popen([sys.executable, '-c', code, str(source), name], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
try:
    if child.stdout.readline().strip() != 'paused-before-admission':
        raise RuntimeError('Launch-race fixture failed to reach the barrier')
    subprocess.run(['sudo','-n','systemctl','stop',service],check=True,timeout=105)
    child.stdin.write('continue\n'); child.stdin.flush()
    out, err = child.communicate(timeout=25)
    absent = subprocess.run(['docker','inspect',name],capture_output=True).returncode != 0
    evidence={'late_launch_refused':child.returncode!=0 and ('admission is closed' in err or 'not ready' in err),
              'no_late_container':absent}
    print(json.dumps(evidence))
    (Path(__file__).resolve().parent/'guard-launch-race-evidence.json').write_text(json.dumps(evidence)+'\n')
    if not all(evidence.values()):raise RuntimeError('Guard launch-race acceptance failed')
finally:
    if child.poll() is None:
        child.kill();child.communicate(timeout=5)
    if subprocess.run(['docker','inspect',name],capture_output=True).returncode==0:
        subprocess.run(['docker','rm','-f',name],check=True,stdout=subprocess.DEVNULL,timeout=20)
    subprocess.run(['sudo','-n','systemctl','start',service],check=True,timeout=105)
