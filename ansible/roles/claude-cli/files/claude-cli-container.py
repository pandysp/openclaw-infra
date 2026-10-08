#!/usr/bin/env python3
"""Host-side launcher. Agent code runs only after the container's privilege drop."""
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
import uuid

HOME = Path('/home/ubuntu')
NATIVE = HOME / '.npm-global/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe'
RUNTIME = HOME / '.openclaw/claude-cli-runtime.json'
# The one login; containers bind its parent to share Claude's refresh locks.
SECURE_STORAGE = HOME / '.claude/shared/auth'


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


def output(args):
    return subprocess.check_output(args, text=True, timeout=15)


def proxy_address(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'http' or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SystemExit('ERROR: Invalid CLI proxy URL; check claude-cli-runtime.json')
    address = ipaddress.IPv4Address(parsed.hostname)
    if not parsed.port or address.is_loopback or address.is_link_local or address.is_unspecified:
        raise SystemExit('ERROR: CLI proxy must use a bridge address and explicit port')
    return [str(address), parsed.port]


def validate_guard(data, interface, endpoint):
    """Check containment invariants; approved endpoint policy stays in nftables."""
    chains = {(item['chain']['family'], item['chain']['name']): item['chain']
              for item in data['nftables'] if 'chain' in item}
    for family, name, priority in [('inet', 'input', -10), ('inet', 'forward', -10), ('bridge', 'forward', -200)]:
        chain = chains.get((family, name), {})
        expected = {'type': 'filter', 'hook': name, 'prio': priority, 'policy': 'accept',
                    'comment': 'openclaw-cli:' + interface}
        if any(chain.get(key) != value for key, value in expected.items()):
            raise SystemExit('ERROR: CLI network guard base hooks are missing or mismatched; reprovision')

    def rules(family, name):
        return [item['rule']['expr'] for item in data['nftables'] if 'rule' in item
                and item['rule']['family'] == family and item['rule']['chain'] == name]

    def match(key, right):
        return {'match': {'op': '==', 'left': {'meta': {'key': key}}, 'right': right}}

    def reject(expression):
        return expression.get('reject', {}).get('type') in ['icmp', 'icmpx'] and expression['reject'].get('expr') == 'port-unreachable'

    source = match('iifname', interface)
    incoming = rules('inet', 'input')
    if not incoming or len(incoming[-1]) != 2 or incoming[-1][0] != source or not reject(incoming[-1][-1]):
        raise SystemExit('ERROR: CLI network guard host-service rejection is missing')
    established = {'match': {'op': 'in', 'left': {'ct': {'key': 'state'}}, 'right': ['established', 'related']}}
    neighbor = {'match': {'op': '==', 'left': {'payload': {'protocol': 'icmpv6', 'field': 'type'}},
                          'right': {'set': ['nd-neighbor-solicit', 'nd-neighbor-advert']}}}
    allowed = [[source, established, {'accept': None}], [source, neighbor, {'accept': None}]]
    actual = set()
    for expression in incoming[:-1]:
        if expression in allowed:
            continue
        if len(expression) != 4 or expression[0] != source or expression[-1] != {'accept': None}:
            raise SystemExit('ERROR: CLI network guard has a broad or unscoped host-service exception')
        destination, port = expression[1].get('match', {}), expression[2].get('match', {})
        if destination.get('op') != '==' or destination.get('left') != {'payload': {'protocol': 'ip', 'field': 'daddr'}} or port.get('op') != '==' or port.get('left') != {'payload': {'protocol': 'tcp', 'field': 'dport'}}:
            raise SystemExit('ERROR: CLI network guard host-service exception is invalid')
        actual.add((destination['right'], port['right']))
    if tuple(endpoint) not in actual or any(rule not in incoming for rule in allowed):
        raise SystemExit('ERROR: CLI network guard MCP route or response handling is missing')
    outgoing = rules('inet', 'forward')
    metadata = {'match': {'op': '==', 'left': {'payload': {'protocol': 'ip', 'field': 'daddr'}}, 'right': '169.254.169.254'}}
    for predicate in [metadata, match('oifkind', 'bridge')]:
        if not any(len(expr) == 3 and expr[:2] == [source, predicate] and reject(expr[-1]) for expr in outgoing):
            raise SystemExit('ERROR: CLI network guard metadata or local-bridge rejection is missing')
    if any(expr[0] != source or not reject(expr[-1]) for expr in outgoing):
        raise SystemExit('ERROR: CLI network guard forward policy has an unscoped rule or early accept')
    if rules('bridge', 'forward') != [[match('ibrname', interface), {'drop': None}]]:
        raise SystemExit('ERROR: CLI network guard same-bridge isolation is missing')


def guard_lifetime_present(metadata):
    identity = (os.major(metadata.st_dev), os.minor(metadata.st_dev), metadata.st_ino)
    # Kernel lock ownership, not credentials or process environment. Only a
    # writable FD can hold this POSIX WRITE lock on the root-owned state file.
    for line in Path('/proc/locks').read_text().splitlines():
        fields = line.split()
        if len(fields) == 8 and fields[1:4] == ['POSIX', 'ADVISORY', 'WRITE'] and fields[6:] == ['0', 'EOF']:
            major, minor, inode = fields[5].split(':')
            if (int(major, 16), int(minor, 16), int(inode)) == identity:
                return True
    return False


def create_guarded(command, table, service):
    path = Path('/run/openclaw-claude-cli') / (table + '.lock')
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise SystemExit('ERROR: CLI guard admission is missing; restore the guard service') from None
    with os.fdopen(descriptor) as state:
        metadata = os.fstat(state.fileno())
        if metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise SystemExit('ERROR: CLI guard admission must be root-owned and read-only')
        try:
            fcntl.flock(state, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('ERROR: CLI guard is recovering; retry after it is ready') from None
        def check_admission():
            state.seek(0)
            if state.read() != '1':
                raise SystemExit('ERROR: CLI guard admission is closed; restore the guard service')
            if not guard_lifetime_present(metadata):
                raise SystemExit('ERROR: CLI guard has no live admission owner; restore the guard service')

        check_admission()
        active = subprocess.run(['systemctl', 'is-active', service], capture_output=True, text=True, timeout=5)
        if active.returncode or active.stdout.strip() != 'active':
            raise SystemExit('ERROR: CLI network guard service is not ready; restore it before launching')
        try:
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=15)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            raise SystemExit('ERROR: CLI container creation failed; private diagnostics withheld') from None
        # Teardown can lose its exclusive-lock acquisition. Never start a late
        # creation whose guardian has already revoked its lifetime evidence.
        check_admission()


def run_container(command, name, prepare=None):
    phase = 'launch'
    cancelled = None

    def interrupted(signum, frame):
        nonlocal cancelled
        cancelled = signum
        if phase == 'wait':
            raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    process = None
    try:
        if prepare is not None:
            prepare()
        if cancelled is not None:
            raise SystemExit(128 + cancelled)
        process = subprocess.Popen(command)
        phase = 'wait'
        if cancelled is not None:
            raise SystemExit(128 + cancelled)
        return process.wait()
    finally:
        phase = 'cleanup'
        try:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        finally:
            remaining = output(['docker', 'ps', '-aq', '--filter', 'name=^/' + name + '$']).strip()
            if remaining:
                subprocess.run(['docker', 'rm', '-f', name], check=True, stdout=subprocess.DEVNULL, timeout=15)


def mac_access(mac_host, mount):
    """Mount the dedicated Mac SSH files and pin the host's address; nothing without a host."""
    if not mac_host:
        return []
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', mac_host):
        raise SystemExit('ERROR: Invalid configured Mac SSH hostname')
    mount(HOME / '.ssh/id_ed25519_openclaw_mac_air')
    mount(HOME / '.ssh/known_hosts_openclaw_mac_air')
    address = socket.getaddrinfo(mac_host, 22, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    return ['--add-host', mac_host + ':' + address]


def main(args, runtime_path=RUNTIME):
    # Read-only queries without a session; OpenClaw checks the login before turns.
    if args in [['--version'], ['--help'], ['auth', 'status', '--json']]:
        os.execv(str(NATIVE), [str(NATIVE), *args])
    runtime = json.loads(Path(runtime_path).read_text())
    config = json.loads((HOME / '.openclaw/openclaw.json').read_text())
    # OpenClaw 2026.9.8 names no agent: it starts Claude in the agent's workspace,
    # so the workspace must belong to exactly one configured agent.
    workspace = Path.cwd()
    owners = [agent_id for agent_id, entry in config['agents']['entries'].items()
              if Path(entry.get('workspace') or config['agents']['defaults']['workspace']) == workspace]
    if len(owners) != 1 or not re.fullmatch(r'[A-Za-z0-9_-]+', owners[0]) or workspace.resolve() != workspace:
        raise SystemExit('ERROR: CLI must start in exactly one configured agent workspace; refusing native execution')
    agent = owners[0]
    # OpenClaw passes Claude's own session ID on every turn (--session-id new, --resume after);
    # only /btw side questions run without one, and they keep no session.
    session = next((args[index + 1] for index, arg in enumerate(args[:-1]) if arg in ('--session-id', '--resume')), '')
    if not (re.fullmatch(r'[A-Za-z0-9-]+', session) or (not session and '--no-session-persistence' in args)):
        raise SystemExit('ERROR: CLI launch names no Claude session; refusing native execution')
    # Deployment-wide Claude flags. When OpenClaw already passes the whole list (/btw passes
    # --tools ""), it is not repeated; when it passes only one of its flags, refuse rather
    # than guess which wins.
    extra = list(runtime['extra_args'])
    if extra and any(args[i:i + len(extra)] == extra for i in range(len(args))):
        extra = []
    elif any(flag in args for flag in extra if flag.startswith('-')):
        raise SystemExit('ERROR: OpenClaw already passes a configured extra CLI flag with another value; refusing ambiguous execution')
    project = HOME / '.claude/projects' / re.sub(r'[^A-Za-z0-9]', '-', str(workspace))
    project.mkdir(parents=True, exist_ok=True)
    if project.resolve() != project:
        raise SystemExit('ERROR: Refusing a symlinked CLI transcript directory')

    mounts = {}

    def mount(source, target=None, writable=False):
        source = Path(source)
        if not source.exists() or source.resolve() != source or ',' in str(source):
            raise SystemExit('ERROR: Missing or noncanonical CLI mount; check runtime provisioning')
        target = str(target or source)
        value = f'type=bind,source={source},target={target}' + ('' if writable else ',readonly')
        if target in mounts and mounts[target] != value:
            raise SystemExit('ERROR: Conflicting CLI container mounts')
        mounts[target] = value

    if not (SECURE_STORAGE / '.credentials.json').is_file():
        raise SystemExit('ERROR: The shared Claude login is missing; run ./scripts/provision.sh --tags openclaw')
    container_home = Path(runtime['homes']) / agent
    for directory in [container_home, container_home / '.claude', container_home / '.claude/projects', container_home / '.claude/shared', container_home / '.ssh']:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.resolve() != directory:
            raise SystemExit('ERROR: Refusing a symlinked CLI container home')
    mount(container_home, HOME, writable=True)
    mount(NATIVE, '/usr/local/bin/claude')
    mount(workspace, writable=True)
    mount(project, writable=True)
    # Directory bind shares atomic credential replacements and both SDK locks.
    mount(SECURE_STORAGE.parent, writable=True)
    mount(HOME / '.claude/settings.json')
    ssh = runtime['ssh'][agent]
    mac_options = mac_access(ssh['mac_host'], mount)
    mount(ssh['config'], HOME / '.ssh/config')
    if ssh['workspace_key'] is not None:
        mount(ssh['workspace_key'], '/run/openclaw-ssh/workspace-key')
        mount(HOME / '.ssh/workspace-github-known-hosts', '/run/openclaw-ssh/github-known-hosts')

    if not re.fullmatch(r'[A-Za-z0-9_]+', runtime['guard_table']):
        raise SystemExit('ERROR: Invalid CLI network guard configuration')
    started = Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19]
    owner = str(os.getpid()) + ':' + started
    # Ownership exists in the name from mkdir onward, even before Docker create.
    artifacts = tempfile.TemporaryDirectory(dir='/tmp', prefix=f"openclaw-claude-cli-{runtime['guard_table']}-{os.getuid()}-{os.getpid()}-{started}-")
    # Compaction and /btw carry no MCP configuration by design; when one is passed it must be OpenClaw's.
    for index, arg in enumerate(args):
        flag, separator, inline = arg.partition('=')
        if flag not in {'--mcp-config', '--append-system-prompt-file', '--plugin-dir'}:
            continue
        if not separator and index + 1 >= len(args):
            raise SystemExit('ERROR: Missing CLI artifact argument')
        source = Path(inline if separator else args[index + 1])
        if not source.is_absolute() or not str(source).startswith('/tmp/openclaw') or source.resolve() != source:
            raise SystemExit('ERROR: Unexpected CLI artifact path')
        if flag == '--mcp-config':
            data = json.loads(source.read_text())
            server = data['mcpServers']['openclaw']
            origin = urlsplit(server['url'])
            if server['type'] != 'http' or origin.scheme != 'http' or origin.hostname != '127.0.0.1' or origin.path != '/mcp' or not origin.port or origin.query or origin.fragment:
                raise SystemExit('ERROR: Expected the generated OpenClaw loopback MCP configuration')
            if urlsplit(runtime['mcp_url']).path != '/openclaw/mcp':
                raise SystemExit('ERROR: CLI MCP relay must use the fixed /openclaw/mcp path')
            proxy_address(runtime['mcp_url'])
            atomic_json(Path(runtime['mcp_target']), {'port': origin.port})
            server['url'] = runtime['mcp_url']
            rewritten = Path(artifacts.name) / 'mcp.json'
            atomic_json(rewritten, data)
            mount(rewritten, source)
        else:
            mount(source)
        if flag == '--plugin-dir':
            for skill in (source / 'skills').iterdir():
                if skill.is_symlink():
                    target = skill.resolve(strict=True)
                    if not target.is_relative_to(workspace):
                        # Since 2026.7.1, channel plugins' skills live in their installed packages.
                        # The OpenClaw package root covers its skills/ and bundled extension skills.
                        roots = [HOME / '.openclaw/skills', HOME / '.npm-global/lib/node_modules/openclaw', HOME / '.openclaw/npm/projects']
                        if not any(target.is_relative_to(root) for root in roots):
                            raise SystemExit(f'ERROR: CLI skill source is outside expected skill roots: {target}')
                        mount(target)

    network = json.loads(output(['docker', 'network', 'inspect', runtime['network']]))[0]
    if network['Driver'] != 'bridge' or not network['EnableIPv6']:
        raise SystemExit('ERROR: CLI requires its provisioned dual-stack bridge network')
    interface = network.get('Options', {}).get('com.docker.network.bridge.name', 'br-' + network['Id'][:12])
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,15}', interface) or not re.fullmatch(r'[A-Za-z0-9_]+', runtime['guard_table']):
        raise SystemExit('ERROR: Invalid CLI network guard configuration')
    if not re.fullmatch(r'[A-Za-z0-9_.@-]+[.]service', runtime['guard_service']):
        raise SystemExit('ERROR: Invalid CLI network guard service name')
    service = subprocess.run(['systemctl', 'is-active', runtime['guard_service']], capture_output=True, text=True, timeout=5)
    if service.returncode != 0 or service.stdout.strip() != 'active':
        raise SystemExit('ERROR: CLI network guard service is not ready; restore it before launching')
    guard = {'nftables': []}
    for family in ['inet', 'bridge']:
        guard['nftables'] += json.loads(output(['sudo', '-n', 'nft', '-j', 'list', 'table', family, runtime['guard_table']]))['nftables']
    validate_guard(guard, interface, proxy_address(runtime['mcp_url']))
    git_proxy = workspace / '.git-proxy-config'
    has_pat = (HOME / '.openclaw/github-tokens' / agent).is_file()
    if has_pat != git_proxy.is_file():
        raise SystemExit('ERROR: Workspace Git proxy configuration does not match its provisioned token')
    if has_pat:
        endpoint = urlsplit(runtime['mcp_url'])
        base = f'http://{endpoint.hostname}:{endpoint.port}'
        original = git_proxy.read_text()
        content, changed = re.subn(r'(?<=url ")http://[^/"\n]+', base, original)
        if changed != 1:
            raise SystemExit('ERROR: Expected one managed workspace Git proxy URL')
        # The provisioned shared endpoint needs no overlay: the workspace's
        # directory bind then sees atomic alias updates during warm sessions.
        if content != original:
            rewritten_git = Path(artifacts.name) / 'git-proxy-config'
            rewritten_git.write_text(content)
            mount(rewritten_git, git_proxy)
    name = 'openclaw-claude-' + uuid.uuid4().hex[:12]
    command = ['docker', 'create', '--rm', '-i', '--name', name, '--network', runtime['network'],
               '--label', 'openclaw.claude-owner=' + owner,
               '--label', 'openclaw.claude-session=' + (session or 'none'), '--label', 'openclaw.claude-agent=' + agent,
               '--label', 'openclaw.claude-guard=' + runtime['guard_table'],
               '--user', '1000:1000', '--read-only', '--cap-drop', 'ALL',
               '--security-opt', 'no-new-privileges', '--stop-timeout', '10',
               '--tmpfs', '/tmp:rw,exec,nosuid,nodev,size=256m', '--tmpfs', '/run:rw,noexec,nosuid,size=1m',
               *mac_options,
               '-e', 'HOME=/home/ubuntu', '-e', 'CLAUDE_SECURESTORAGE_CONFIG_DIR=' + str(SECURE_STORAGE),
               '-e', 'GIT_TERMINAL_PROMPT=0',
               '-e', f'GIT_CONFIG_COUNT={3 if has_pat else 2}', '-e', 'GIT_CONFIG_KEY_0=user.name', '-e', 'GIT_CONFIG_VALUE_0=OpenClaw Agent',
               '-e', 'GIT_CONFIG_KEY_1=user.email', '-e', 'GIT_CONFIG_VALUE_1=openclaw@localhost']
    if has_pat:
        command += ['-e', 'GIT_CONFIG_KEY_2=include.path', '-e', f'GIT_CONFIG_VALUE_2={git_proxy}']
    for variable in runtime['env_names']:
        if not re.fullmatch(r'[A-Z][A-Z0-9_]*_(?:API_KEY|API_TOKEN)', variable):
            raise SystemExit('ERROR: Unsafe CLI skill environment name in runtime configuration')
    for variable in sorted(os.environ):
        if variable.startswith('OPENCLAW_MCP_') or variable in runtime['env_names']:
            command += ['-e', variable]
    for binding in mounts.values():
        command += ['--mount', binding]
    command += ['-w', str(workspace), runtime['image'], *args, *extra]
    with Path(runtime['invocations']).open('a') as stream:
        stream.write(json.dumps({'agent': agent, 'session': session, 'name': name, 'resuming': '--resume' in args}) + '\n')

    try:
        return run_container(['docker', 'start', '--attach', '--interactive', name], name,
            prepare=lambda: create_guarded(command, runtime['guard_table'], runtime['guard_service']))
    finally:
        artifacts.cleanup()


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
