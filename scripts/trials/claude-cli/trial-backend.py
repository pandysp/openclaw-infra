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
config = json.loads(config_path.read_text())
defaults = config['agents']['defaults']
current = defaults.get('cliBackends')
if sys.argv[1] == 'apply':
    if current is not None:
        raise SystemExit('ERROR: Trial backend is not originally unset; refusing to overwrite it')
    defaults['cliBackends'] = desired
elif sys.argv[1] == 'restore':
    if current is not None and current != desired:
        raise SystemExit('ERROR: Rollback found a different backend override; operator intervention required')
    defaults.pop('cliBackends', None)
else:
    raise SystemExit('ERROR: Expected apply or restore')
atomic_json(config_path, config)
subprocess.run(['systemctl', '--user', 'restart', 'openclaw-gateway'], check=True)
print('trial-backend=' + sys.argv[1])
