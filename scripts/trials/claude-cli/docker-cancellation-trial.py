#!/usr/bin/env python3
"""Real Docker SIGTERM/SIGINT cleanup; daemon errors never count as removal."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

source = Path(sys.argv[1]) if len(sys.argv) == 2 else Path.home() / '.openclaw/claude-cli-container'
code = '''import importlib.machinery,importlib.util,sys
loader=importlib.machinery.SourceFileLoader('launcher',sys.argv[1]);s=importlib.util.spec_from_loader('launcher',loader);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
name=sys.argv[2]
raise SystemExit(m.run_container(['docker','run','--rm','--name',name,'--network','none','--user','1000:1000','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--entrypoint','/bin/sleep','openclaw-claude-cli:latest','120'],name))
'''


def state(name):
    result = subprocess.run(['docker', 'inspect', '--format', '{{json .State}}', name], capture_output=True, text=True, timeout=5)
    if result.returncode == 0:
        return json.loads(result.stdout)
    error = result.stderr.lower()
    if ('no such object: ' + name) in error or ('no such container: ' + name) in error:
        return None
    raise RuntimeError('Scoped Docker inspection failed; private diagnostics withheld')


def starttime(pid):
    return Path('/proc/' + str(pid) + '/stat').read_text().rsplit(')', 1)[1].split()[19]


evidence = {'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'signals': {}}
for sig in (signal.SIGTERM, signal.SIGINT):
    name = 'c-cancel-' + uuid.uuid4().hex[:12]
    child = subprocess.Popen([sys.executable, '-c', code, str(source), name], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    owner = starttime(child.pid)
    owner_fd = os.pidfd_open(child.pid)
    try:
        if starttime(child.pid) != owner:
            raise RuntimeError('Fixture launcher ownership changed')
        deadline = time.monotonic() + 15
        while True:
            current = state(name)
            if current and current['Running'] and current['Pid'] > 0:
                break
            if child.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('Real Docker fixture never reached a running container')
            time.sleep(.1)
        signal.pidfd_send_signal(owner_fd, sig)
        child.communicate(timeout=35)
        removed = state(name) is None
        evidence['signals'][signal.Signals(sig).name] = {'exit': child.returncode, 'actual_container_removed': removed}
        if child.returncode != 128 + sig or not removed:
            raise RuntimeError('Real Docker cancellation acceptance failed')
    finally:
        if child.poll() is None:
            signal.pidfd_send_signal(owner_fd, signal.SIGKILL)
            child.communicate(timeout=5)
        os.close(owner_fd)
        if state(name) is not None:
            subprocess.run(['docker', 'rm', '-f', name], check=True, stdout=subprocess.DEVNULL, timeout=20)
print(json.dumps(evidence))
Path(__file__).with_name('docker-cancellation-evidence.json').write_text(json.dumps(evidence) + '\n')
