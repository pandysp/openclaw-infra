#!/usr/bin/env python3
"""Refuse to start an agent without its authenticated OpenClaw tool connection."""
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request

args = sys.argv[1:]
paths = []
for index, arg in enumerate(args):
    if arg == '--mcp-config' and index + 1 < len(args):
        paths.append(args[index + 1])
    elif arg.startswith('--mcp-config='):
        paths.append(arg.partition('=')[2])
if len(paths) != 1:
    raise SystemExit('ERROR: Expected one generated OpenClaw MCP configuration')
server = json.loads(Path(paths[0]).read_text())['mcpServers']['openclaw']
if not os.environ.get('OPENCLAW_MCP_TOKEN'):
    raise SystemExit('ERROR: OpenClaw MCP authorization is missing; check gateway backend preparation')
headers = {key: re.sub(r'\$\{([A-Z0-9_]+)\}', lambda match: os.environ.get(match[1], ''), value)
           for key, value in server['headers'].items()}
headers.update({'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'})
payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
           'params': {'protocolVersion': '2025-03-26', 'capabilities': {},
                      'clientInfo': {'name': 'openclaw-cli-readiness', 'version': '1'}}}
request = urllib.request.Request(server['url'], json.dumps(payload).encode(), headers)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open(request, timeout=10) as response:
        data = response.read(1048577)
        if len(data) > 1048576:
            raise SystemExit('ERROR: OpenClaw MCP readiness response is oversized')
        result = json.loads(data)
except urllib.error.HTTPError as error:
    raise SystemExit(f'ERROR: OpenClaw MCP readiness returned HTTP {error.code}; check proxy target and authorization')
except (urllib.error.URLError, ValueError):
    raise SystemExit('ERROR: OpenClaw MCP readiness failed; check proxy service, network guard and generated target')
if not isinstance(result.get('result', {}).get('serverInfo'), dict):
    raise SystemExit('ERROR: OpenClaw MCP initialization failed; no native-tool-only fallback is permitted')
os.execv('/usr/local/bin/claude', ['/usr/local/bin/claude', *args])
