#!/usr/bin/env python3
"""Trial dispatcher: production sessions remain on the original native backend."""
import os
from pathlib import Path
import sys

home = Path('/home/ubuntu')
agent = os.environ.get('OPENCLAW_MCP_AGENT_ID', '')
if not os.environ.get('OPENCLAW_MCP_SESSION_KEY', '').startswith(f'agent:{agent}:cwrapper-'):
    native = home / '.npm-global/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe'
    os.execv(str(native), [str(native), *sys.argv[1:]])
if os.environ['OPENCLAW_MCP_SESSION_KEY'].startswith(f'agent:{agent}:cwrapper-cancel-'):
    os.environ['PATH'] = str(Path(__file__).resolve().parent / 'cancel-bin') + ':' + os.environ['PATH']
launcher = home / '.openclaw/claude-cli-container'
os.execv(str(launcher), [str(launcher), *sys.argv[1:]])
