#!/usr/bin/env python3
"""Prove one Playwright browser loads pages through the home proxy, end to end.

Usage: playwright-check.py <mcp-url>

Speaks MCP (streamable HTTP) like the gateway's adapter does, then:
  1. loads an IP echo page: the address must differ from this server's own,
  2. loads a private address: the home proxy itself must refuse it (HTTP 403 and
     squid's access-denied page, not a device's own 403),
  3. closes the browser so the check leaves no Chromium running.
Exits non-zero with the reason on any failure.
"""
import json
import re
import sys
import urllib.request

ECHO = 'https://api.ipify.org'
PRIVATE = 'http://192.168.0.1/'
IPV4 = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')
# Playwright MCP reports the navigation's response status in its page summary.
STATUS = re.compile(r'^- HTTP status: (\d{3})\b', re.MULTILINE)
# squid's ERR_ACCESS_DENIED page; a device behind the proxy answering 403 lacks it.
SQUID_DENIED = ('ERROR: The requested URL could not be retrieved', 'Access Denied')


class Client:
    def __init__(self, url):
        self.url, self.session, self.next_id = url, None, 0

    def call(self, method, params=None, notify=False):
        body = {'jsonrpc': '2.0', 'method': method, 'params': params or {}}
        if not notify:
            self.next_id += 1
            body['id'] = self.next_id
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
        if self.session:
            headers['Mcp-Session-Id'] = self.session
        request = urllib.request.Request(self.url, json.dumps(body).encode(), headers, method='POST')
        with urllib.request.urlopen(request, timeout=120) as response:
            self.session = response.headers.get('Mcp-Session-Id') or self.session
            raw = response.read().decode()
        if notify:
            return None
        data = [line[5:] for line in raw.splitlines() if line.startswith('data:')]
        message = json.loads(data[-1] if data else raw)
        if 'error' in message:
            sys.exit(f'ERROR: {method} failed: {message["error"]}')
        return message['result']

    def tool(self, name, arguments=None):
        result = self.call('tools/call', {'name': name, 'arguments': arguments or {}})
        text = '\n'.join(part.get('text', '') for part in result.get('content', []) if part.get('type') == 'text')
        if result.get('isError'):
            sys.exit(f'ERROR: {name} failed: {text[:500]}')
        return text


def main(url):
    with urllib.request.urlopen(ECHO, timeout=30) as response:
        server_ip = response.read().decode().strip()
    client = Client(url)
    client.call('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                               'clientInfo': {'name': 'openclaw-provision-check', 'version': '1'}})
    client.call('notifications/initialized', notify=True)
    try:
        client.tool('browser_navigate', {'url': ECHO})
        found = IPV4.findall(client.tool('browser_snapshot'))
        if not found:
            sys.exit(f'ERROR: {ECHO} showed no IPv4 address through the browser')
        if found[0] == server_ip:
            sys.exit(f'ERROR: the browser left from this server ({server_ip}), not through the home proxy')
        status = STATUS.search(client.tool('browser_navigate', {'url': PRIVATE}))
        page = client.tool('browser_snapshot')
        if not status or status.group(1) != '403' or not all(mark in page for mark in SQUID_DENIED):
            sys.exit(f'ERROR: the home proxy did not refuse {PRIVATE} (HTTP status: '
                     f'{status.group(1) if status else "none"}); check its policy on the Mac')
    finally:
        client.tool('browser_close')
    print(json.dumps({'server_ip': server_ip, 'browser_ip': found[0], 'private_refused': True}))


if __name__ == '__main__':
    main(*sys.argv[1:])
