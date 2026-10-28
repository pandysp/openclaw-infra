#!/usr/bin/env python3
"""Check on the server that every TLS client we run rejects the eMudhra test chains.

Runs ON the server (scripts/verify-tls-trust.sh pipes it over SSH). For each target it
connects to the 12 unsafe test sites of the October 2026 eMudhra incident
(https://emudhra.we-have-a-problem.com/) and 4 healthy controls. A target passes
only if all 12 fail certificate verification and all 4 controls connect. A site
that cannot be reached at all is reported as inconclusive and fails the run;
it never counts as a rejection.

Targets:
  host     /usr/bin/node as installed, and every running provisioned user service's
           Node process, using that process's own executable (/proc/<pid>/exe, so a
           service still on a replaced Node is tested on that old Node) and its
           trust environment.
  images   Node, Python, curl and uv inside each local OpenClaw image, plus a check that
           every running container uses its image's current build.

Usage: tls-trust-check.py NODE_MIN_VERSION [host] [images]   (both sections by default)
Prints one line per target and exits 1 if any target fails.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

UNSAFE = [f'{kind}-g3{branch}{cross}-emudhra.we-have-a-problem.com'
          for cross in ('', '-aaa') for kind in ('dv', 'ov', 'ev') for branch in ('b', 'c')]
HEALTHY = ['emudhra.we-have-a-problem.com', 'registry.npmjs.org', 'pypi.org', 'github.com']
TRUST_ENV = ['NODE_OPTIONS', 'NODE_EXTRA_CA_CERTS', 'NODE_USE_SYSTEM_CA', 'NODE_TLS_REJECT_UNAUTHORIZED',
             'SSL_CERT_FILE', 'SSL_CERT_DIR']
UNIT_DIR = Path.home()/'.config/systemd/user'
# Test-only overrides, like restart-replaced-programs.sh's.
CGROUP_ROOT = os.environ.get('CGROUP_ROOT', '/sys/fs/cgroup')
CLAUDE_LAUNCHER = Path(os.environ.get('CLAUDE_LAUNCHER', '/usr/local/lib/openclaw/claude-cli-bin/claude'))
# Runtimes each image must contain; a missing runtime fails instead of being skipped.
# The sandbox images always exist; the other two only where their feature is on.
IMAGES = {
    'openclaw-sandbox:trixie': ['python', 'curl'],
    'openclaw-sandbox-custom:latest': ['node', 'python', 'curl', 'uv'],
    'openclaw-claude-cli:latest': ['node', 'python', 'curl', 'uv'],
    'openclaw-workspace-sync:latest': ['python'],
}
# Each optional image is required where provisioning left its feature's marker:
# the gateway's container launcher (removed when containers are off) and the
# workspace backup timers.
FEATURE_MARKERS = {
    'openclaw-claude-cli:latest': CLAUDE_LAUNCHER.exists,
    'openclaw-workspace-sync:latest': lambda: any(UNIT_DIR.glob('workspace-git-sync-*.timer')),
}

# Each probe prints one JSON object per host: {"host", "outcome": accept|reject|error, "detail"}.
NODE_PROBE = r'''
const https = require('node:https');
const certError = c => /^(CERT_|UNABLE_TO_|SELF_SIGNED|DEPTH_ZERO|ERR_TLS_CERT|KEY_USAGE|INVALID_CA|PATH_LENGTH)/.test(c || '');
for (const host of process.argv.slice(1)) {
  const req = https.get(`https://${host}/`, { timeout: 15000 }, res => {
    res.resume(); console.log(JSON.stringify({ host, outcome: 'accept', detail: `HTTP ${res.statusCode}` })); });
  req.on('error', e => console.log(JSON.stringify({ host, outcome: certError(e.code) ? 'reject' : 'error', detail: e.code || e.message })));
  req.on('timeout', () => req.destroy(Object.assign(new Error('timeout'), { code: 'ETIMEDOUT' })));
}
'''
PYTHON_PROBE = r'''
import json, ssl, sys, urllib.error, urllib.request
for host in sys.argv[1:]:
    try:
        urllib.request.urlopen(f'https://{host}/', timeout=15).close()
        result = ('accept', 'HTTP ok')
    except urllib.error.HTTPError as e:
        result = ('accept', f'HTTP {e.code}')
    except urllib.error.URLError as e:
        verify = isinstance(e.reason, ssl.SSLCertVerificationError)
        result = ('reject' if verify else 'error', str(e.reason)[:160])
    except Exception as e:
        result = ('error', repr(e)[:160])
    print(json.dumps({'host': host, 'outcome': result[0], 'detail': result[1]}))
'''
# curl exit 60: the peer certificate cannot be authenticated with the known CAs.
CURL_PROBE = r'''
for host in "$@"; do
  code=0; curl -sS -o /dev/null --max-time 15 "https://$host/" 2>/dev/null || code=$?
  case $code in 0) outcome=accept ;; 60) outcome=reject ;; *) outcome=error ;; esac
  printf '{"host":"%s","outcome":"%s","detail":"curl exit %s"}\n' "$host" "$outcome" "$code"
done
'''
# uv resolves one package with each host as its index. Any answer means TLS was
# accepted, even one uv then refuses ("No solution found", wrong content type);
# only a failed request ("error sending request") is not a verdict.
UV_PROBE = r'''
for host in "$@"; do
  code=0; out=$(printf 'six\n' | uv pip compile --no-cache --quiet --index-url "https://$host/simple/" - 2>&1) || code=$?
  if printf '%s' "$out" | grep -qE 'invalid peer certificate'; then outcome=reject
  elif printf '%s' "$out" | grep -q 'error sending request'; then outcome=error
  else outcome=accept; fi
  detail=$(printf '%s' "$out" | grep -oE 'invalid peer certificate: [A-Za-z]+|cause: .{0,60}|error: .{0,60}' | tail -1 | tr -d '"\\')
  printf '{"host":"%s","outcome":"%s","detail":"uv exit %s %s"}\n' "$host" "$outcome" "$code" "$detail"
done
'''
FAILURES = []


def report(target, completed):
    """Judge one probe run's output and print a single line."""
    rows = {}
    for line in completed.stdout.splitlines():
        if line.startswith('{'):
            row = json.loads(line)
            rows[row['host']] = row
    accepted = [h for h in UNSAFE if rows.get(h, {}).get('outcome') == 'accept']
    unclear = [h for h in UNSAFE if rows.get(h, {}).get('outcome') not in ('accept', 'reject')]
    broken = [h for h in HEALTHY if rows.get(h, {}).get('outcome') != 'accept']
    rejected = len(UNSAFE) - len(accepted) - len(unclear)
    ok = not accepted and not unclear and not broken
    line = f'{"PASS" if ok else "FAIL"} {target}: unsafe rejected {rejected}/{len(UNSAFE)}, healthy {len(HEALTHY) - len(broken)}/{len(HEALTHY)}'
    if accepted:
        line += f'; ACCEPTED compromised chains: {", ".join(accepted)}'
    if unclear:
        line += '; inconclusive (not a certificate rejection): ' + ', '.join(
            f'{h} [{rows.get(h, {}).get("detail", "no result")}]' for h in unclear)
    if broken:
        line += '; healthy controls failing: ' + ', '.join(
            f'{h} [{rows.get(h, {}).get("detail", "no result")}]' for h in broken)
    if not rows:
        line += f'; probe produced no results (exit {completed.returncode}): {completed.stderr.strip()[:300]}'
    print(line, flush=True)
    if not ok:
        FAILURES.append(target)


def fail(message):
    print(f'FAIL {message}', flush=True)
    FAILURES.append(message)


def run(argv, **kwargs):
    return subprocess.run(argv, capture_output=True, text=True, timeout=600, **kwargs)


def version_tuple(text):
    return tuple(int(part) for part in text.lstrip('v').split('.'))


def systemctl(*args):
    result = run(['systemctl', '--user', *args])
    if result.returncode:
        raise SystemExit(f'ERROR: systemctl --user {" ".join(args)} failed: {result.stderr.strip()}')
    return result.stdout


def node_processes():
    """Distinct Node runtimes in running provisioned user services.

    Every Node process counts, children included; processes sharing an executable
    and trust environment within one service are probed once.
    Yields (unit, pid, exe, trust environment).
    """
    units = systemctl('list-units', '--type=service', '--state=running', '--no-legend', '--plain')
    for unit in (line.split()[0] for line in units.splitlines() if line.strip()):
        if not systemctl('show', '-P', 'FragmentPath', unit).strip().startswith(f'{UNIT_DIR}/'):
            continue
        cgroup = Path(CGROUP_ROOT + systemctl('show', '-P', 'ControlGroup', unit).strip())
        if not cgroup.is_dir():
            raise SystemExit(f'ERROR: cannot find the control group of running {unit}')
        seen = set()
        for pid in sorted(int(pid) for procs in cgroup.rglob('cgroup.procs') for pid in procs.read_text().split()):
            try:
                exe = os.readlink(f'/proc/{pid}/exe')
                identity = os.stat(f'/proc/{pid}/exe')
                raw = Path(f'/proc/{pid}/environ').read_bytes()
            except OSError:
                continue  # exited meanwhile
            if not Path(exe.removesuffix(' (deleted)')).name.startswith('node'):
                continue
            environ = dict(item.split('=', 1) for item in raw.decode(errors='replace').split('\0') if '=' in item)
            trust = {name: environ[name] for name in TRUST_ENV if environ.get(name)}
            key = (exe, identity.st_dev, identity.st_ino, tuple(sorted(trust.items())))
            if key not in seen:
                seen.add(key)
                yield unit, pid, exe, trust


def check_host(node_min, probe_dir):
    node = shutil.which('node')
    if not node:
        return fail('host: node is not installed')
    version = run([node, '--version']).stdout.strip()
    if version_tuple(version) < version_tuple(node_min):
        fail(f'host: installed Node {version} is below the floor {node_min}')
    report(f'host {node} {version} (fresh process)', run([node, f'{probe_dir}/probe.js', *UNSAFE, *HEALTHY]))
    probed = 0
    for unit, pid, exe, trust in node_processes():
        probed += 1
        if trust.get('NODE_TLS_REJECT_UNAUTHORIZED') == '0':
            fail(f'host {unit} pid {pid}: NODE_TLS_REJECT_UNAUTHORIZED=0 disables certificate checks')
        # Only trust settings travel; the process's other variables hold credentials.
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(Path.home()), **trust}
        version = run([f'/proc/{pid}/exe', '--version'], env=env).stdout.strip()
        label = f'host {unit} pid {pid} on {exe} {version}' + (f' with {", ".join(sorted(trust))}' if trust else '')
        # The service's own (possibly replaced) executable, through /proc.
        report(label, run([f'/proc/{pid}/exe', f'{probe_dir}/probe.js', *UNSAFE, *HEALTHY], env=env))
    if not probed:
        fail('host: no running provisioned Node service found (is the gateway running?)')


def check_images(probe_dir):
    for image, runtimes in IMAGES.items():
        inspect = run(['docker', 'image', 'inspect', '--format', '{{.Id}} {{.Created}}', image])
        if inspect.returncode:
            if image in FEATURE_MARKERS and not FEATURE_MARKERS[image]() and 'No such image' in inspect.stderr:
                print(f'SKIP image {image}: its feature is off on this server', flush=True)
            else:
                fail(f'image {image}: {inspect.stderr.strip()}')
            continue
        image_id, created = inspect.stdout.split()
        print(f'  image {image} {image_id[7:19]} built {created}', flush=True)
        for runtime in runtimes:
            command = {'node': ['node', '/probe/probe.js'], 'python': ['python3', '/probe/probe.py'],
                       'curl': ['sh', '/probe/probe-curl.sh'], 'uv': ['sh', '/probe/probe-uv.sh']}[runtime]
            # Only the image's own trust: no host environment and no inherited NODE_OPTIONS.
            completed = run(['docker', 'run', '--rm', '--network', 'bridge', '-v', f'{probe_dir}:/probe:ro',
                             '--entrypoint', command[0], image, *command[1:], *UNSAFE, *HEALTHY])
            report(f'image {image} {runtime}', completed)
    # By the image each container was created from (.Config.Image): `docker ps` shows a
    # bare image ID instead of the name once the tag has moved to a newer build.
    ids = run(['docker', 'ps', '-q', '--no-trunc'])
    if ids.returncode:
        return fail(f'containers: {ids.stderr.strip()}')
    current = {image: run(['docker', 'image', 'inspect', '--format', '{{.Id}}', image]).stdout.strip() for image in IMAGES}
    ours, stale = 0, []
    if ids.stdout.split():
        inspect = run(['docker', 'inspect', '--format', '{{.Name}} {{.Config.Image}} {{.Image}}', *ids.stdout.split()])
        if inspect.returncode:
            return fail(f'containers: {inspect.stderr.strip()}')
        for name, image, image_id in (line.split() for line in inspect.stdout.splitlines()):
            if image in IMAGES:
                ours += 1
                if image_id != current[image]:
                    stale.append(f'{name.lstrip("/")} ({image})')
    if stale:
        fail(f'containers on an older image build (restart the gateway to recreate them): {", ".join(stale)}')
    else:
        print(f'PASS running containers: {ours} from OpenClaw images, all on the current builds', flush=True)


def main(args):
    if not args:
        raise SystemExit(__doc__)
    node_min, sections = args[0], args[1:] or ['host', 'images']
    if unknown := set(sections) - {'host', 'images'}:
        raise SystemExit(f'ERROR: unknown section(s) {", ".join(sorted(unknown))}; use host and/or images')
    with tempfile.TemporaryDirectory(prefix='tls-trust-') as probe_dir:
        os.chmod(probe_dir, 0o755)  # readable by the images' UID 1000 users
        for name, content in (('probe.js', NODE_PROBE), ('probe.py', PYTHON_PROBE),
                              ('probe-curl.sh', CURL_PROBE), ('probe-uv.sh', UV_PROBE)):
            Path(probe_dir, name).write_text(content)
            os.chmod(Path(probe_dir, name), 0o644)
        if 'host' in sections:
            check_host(node_min, probe_dir)
        if 'images' in sections:
            check_images(probe_dir)
    print(f'{len(FAILURES)} failing target(s)' if FAILURES else 'All TLS trust checks passed', flush=True)
    return 1 if FAILURES else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
