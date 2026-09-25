#!/usr/bin/env bash
# Inventory by default. --owned-node removes only the node proven by Phoenix's
# authenticated host/IP check; a prefix or offline timestamp is not ownership.
# An empty PHOENIX_TAILSCALE_NODE_ID means the run never proved a node (deploy
# failed first): then only the absence of a device with the run name counts as
# clean, and a device carrying that name is an unproven leftover, not ours to delete.
# Auth: TAILSCALE_API_KEY or TS_OAUTH_CLIENT_ID + TS_OAUTH_SECRET in the environment.
set -euo pipefail
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != --dry-run && "$1" != --owned-node ) ]]; then
    echo 'Usage: cleanup-staging-tailnet.sh [--dry-run|--owned-node]' >&2
    exit 2
fi
python3 - "${1:---dry-run}" <<'PY'
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

api = 'https://api.tailscale.com/api/v2'

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None

opener = urllib.request.build_opener(NoRedirect())
owned = sys.argv[1] == '--owned-node'
name = os.environ.get('PHOENIX_RESOURCE_NAME', '')
node_id = os.environ.get('PHOENIX_TAILSCALE_NODE_ID', '')
if owned and (not re.fullmatch(r'openclaw-staging-[0-9]+-[0-9]+', name)
              or not re.fullmatch(r'[A-Za-z0-9]*', node_id)):
    sys.exit('Owned-node cleanup requires the unique Phoenix run name and a well-formed node ID')
phase = 'authentication'
try:
    token = os.environ.get('TAILSCALE_API_KEY', '')
    if not token:
        client_id = os.environ.get('TS_OAUTH_CLIENT_ID', '')
        client_secret = os.environ.get('TS_OAUTH_SECRET', '')
        if not client_id or not client_secret:
            raise ValueError('Missing Tailscale credentials')
        data = urllib.parse.urlencode({'client_id': client_id, 'client_secret': client_secret,
                                       'grant_type': 'client_credentials'}).encode()
        request = urllib.request.Request(api + '/oauth/token', data=data)
        with opener.open(request, timeout=30) as response:
            token = json.load(response)['access_token']
    if not isinstance(token, str) or not token:
        raise ValueError('Empty Tailscale token')
    tailnet = urllib.parse.quote(os.environ.get('TAILNET', '-'), safe='')
    def inventory():
        request = urllib.request.Request(api + '/tailnet/' + tailnet + '/devices',
                                         headers={'Authorization': 'Bearer ' + token})
        with opener.open(request, timeout=30) as response:
            devices = json.load(response)['devices']
        if not isinstance(devices, list) or any(
            not isinstance(d, dict) or not isinstance(d.get('hostname'), str)
            or not isinstance(d.get('id'), str) or not d['id']
            or not isinstance(d.get('nodeId'), str) or not d['nodeId'] for d in devices
        ):
            raise ValueError('Invalid device inventory')
        return devices

    phase = 'device inventory'
    devices = inventory()
    selected = [{'id': d['id'], 'hostname': d['hostname'], 'lastSeen': d.get('lastSeen')}
                for d in devices if d['hostname'].startswith('openclaw-staging')]
    if owned:
        phase = 'ownership validation'
        matching = [d for d in devices if node_id and d['nodeId'] == node_id]
        if not matching:
            if any(d['hostname'] == name for d in devices):
                raise ValueError('Run device exists but its ownership was not proven')
            print(json.dumps({'owned_node_absent': True, 'deleted': 0}))
            sys.exit(0)
        if len(matching) != 1 or matching[0]['hostname'] != name:
            raise ValueError('Owned node identity changed')
        device_id = matching[0]['id']
        request = urllib.request.Request(api + '/device/' + urllib.parse.quote(device_id, safe=''),
                                         method='DELETE', headers={'Authorization': 'Bearer ' + token})
        delete_error = None
        phase = 'owned-device deletion'
        try:
            with opener.open(request, timeout=30) as response:
                response.read()
        except (urllib.error.URLError, TimeoutError) as error:
            delete_error = type(error).__name__
        phase = 'owned-device absence check'
        remaining = inventory()
        if any(d['id'] == device_id or d['nodeId'] == node_id for d in remaining):
            raise ValueError('Owned device still present after deletion')
        if delete_error:
            raise RuntimeError('Deletion reported failure; readback found the device absent')
        print(json.dumps({'owned_node_absent': True, 'deleted': 1, 'device_id': device_id}))
        sys.exit(0)
except (urllib.error.URLError, TimeoutError, ValueError, KeyError, TypeError, RuntimeError) as error:
    print('Tailscale cleanup failed during ' + phase + ' (' + type(error).__name__ +
          '); check API permissions and run ownership. Private diagnostics withheld.', file=sys.stderr)
    sys.exit(1)

print(json.dumps({'inventory_only': True, 'devices': selected, 'deleted': 0}))
if selected:
    print('Staging device records need an ownership check. No devices were deleted.', file=sys.stderr)
    sys.exit(1)
PY
