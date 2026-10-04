#!/usr/bin/env python3
"""Trial dispatcher: every session runs in a container; cancel trials get the fault shim."""
import os
from pathlib import Path
import sys

home = Path('/home/ubuntu')
agent = os.environ.get('OPENCLAW_MCP_AGENT_ID', '')
launcher = home / '.openclaw/claude-cli-container'
if os.environ.get('OPENCLAW_MCP_SESSION_KEY', '').startswith(f'agent:{agent}:cwrapper-cancel-'):
    os.environ['PATH'] = str(Path(__file__).resolve().parent / 'cancel-bin') + ':' + os.environ['PATH']
os.execv(str(launcher), [str(launcher), *sys.argv[1:]])
