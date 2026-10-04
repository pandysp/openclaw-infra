#!/usr/bin/env python3
"""Real bridge guard tests; HTTP bodies and addresses are never printed."""
import json
from pathlib import Path
import subprocess
import uuid

s = Path(__file__).resolve().parent
runtime = json.loads((s / 'restricted-runtime.json').read_text())
suffix = uuid.uuid4().hex[:8]
peer_network = 'c-peer-' + suffix
containers = []


def output(args):
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE, timeout=30).strip()


def server(network):
    name = 'c-peer-server-' + uuid.uuid4().hex[:8]
    containers.append(name)
    output(['docker', 'run', '-d', '--name', name, '--network', network, '--read-only', '--user', '1000:1000',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--entrypoint', 'python3',
            runtime['image'], '-m', 'http.server', '8888', '--bind', '::', '--directory', '/tmp'])
    data = json.loads(output(['docker', 'inspect', name]))[0]['NetworkSettings']['Networks'][network]
    return data['IPAddress'], data['GlobalIPv6Address']


def probe(network, urls):
    code = '''import json,urllib.request,urllib.error
urls=json.loads(input());out={};opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
for name,url in urls.items():
 try:
  with opener.open(urllib.request.Request(url),timeout=2) as response: out[name]=response.status
 except urllib.error.HTTPError as error: out[name]=error.code
 except urllib.error.URLError: out[name]=None
print(json.dumps(out))
'''
    result = subprocess.run(['docker', 'run', '--rm', '-i', '--network', network, '--read-only', '--user', '1000:1000',
                             '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--entrypoint', 'python3',
                             runtime['image'], '-c', code], input=json.dumps(urls), capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise RuntimeError('Network fixture failed; diagnostics withheld')
    return json.loads(result.stdout)


try:
    output(['docker', 'network', 'create', '--ipv6', '--subnet', 'fd27:0c1a:' + suffix[:4] + '::/64',
            '--opt', 'com.docker.network.bridge.name=peer-' + suffix, peer_network])
    same4, same6 = server(runtime['network'])
    other4, other6 = server(peer_network)
    urls = {'same_bridge_ipv4': f'http://{same4}:8888/', 'same_bridge_ipv6': f'http://[{same6}]:8888/',
            'custom_bridge_ipv4': f'http://{other4}:8888/', 'custom_bridge_ipv6': f'http://[{other6}]:8888/'}
    control = probe('host', urls)
    denied = probe(runtime['network'], urls)
    allowed = probe(runtime['network'], {'proxy_health': runtime['mcp_url'].replace('/openclaw/mcp', '/health'),
                                         'public_web': 'https://www.cloudflare.com/cdn-cgi/trace',
                                         'metadata': 'http://169.254.169.254/hetzner/v1/metadata',
                                         'userdata': 'http://169.254.169.254/hetzner/v1/userdata',
                                         'direct_qmd': 'http://172.19.0.1:8191/mcp'})
    evidence = {'unrestricted_controls_http_200': all(v == 200 for v in control.values()),
                **{name + '_blocked': value is None for name, value in denied.items()},
                'proxy_allowed': allowed['proxy_health'] == 200, 'public_web_allowed': allowed['public_web'] == 200,
                'metadata_blocked': allowed['metadata'] is None, 'userdata_blocked': allowed['userdata'] is None,
                'direct_qmd_blocked': allowed['direct_qmd'] is None}
    print(json.dumps(evidence))
    (s / 'bridge-guard-evidence.json').write_text(json.dumps(evidence) + '\n')
    if not all(evidence.values()):
        raise RuntimeError('Bridge network guard acceptance failed')
finally:
    for name in containers:
        output(['docker', 'rm', '-f', name])
    output(['docker', 'network', 'rm', peer_network])
