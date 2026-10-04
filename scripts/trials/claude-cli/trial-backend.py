#!/usr/bin/env python3
"""Atomic trial backend change and independent systemd-timer rollback."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import os

home = Path.home()
scratch = Path(__file__).resolve().parent

def atomic_json(path, value):
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

config_path = home / '.openclaw/openclaw.json'
wrapper = str(scratch / 'restricted-dispatcher.py')
desired = {'claude-cli': {'command': wrapper}}
# Trials run with containers enabled (openclaw_claude_cli_enabled: true).
containers = {'claude-cli': {'command': str(home / '.openclaw/claude-cli-container')}}
config = json.loads(config_path.read_text())
defaults = config['agents']['defaults']
current = defaults.get('cliBackends')
if sys.argv[1] == 'apply':
    if current != containers:
        raise SystemExit('ERROR: Trials need the container backend; enable containers first')
    defaults['cliBackends'] = desired
elif sys.argv[1] == 'restore':
    if current not in (desired, containers):
        raise SystemExit('ERROR: Rollback found a different backend override; operator intervention required')
    defaults['cliBackends'] = containers
else:
    raise SystemExit('ERROR: Expected apply or restore')
atomic_json(config_path, config)
subprocess.run(['systemctl', '--user', 'restart', 'openclaw-gateway'], check=True)
print('trial-backend=' + sys.argv[1])
