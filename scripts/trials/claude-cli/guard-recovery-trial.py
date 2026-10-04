#!/usr/bin/env python3
"""Disposable real-Docker guard recovery proof; no native gateway override."""
import json
from pathlib import Path
import subprocess
import time
import uuid

scratch = Path(__file__).resolve().parent
runtime = json.loads((Path.home() / '.openclaw/claude-cli-runtime.json').read_text())
suffix = uuid.uuid4().hex[:8]
# A second guard on its own fixture table, so the installed guard is untouched.
table = 'openclaw_recovery_' + suffix
unit = 'openclaw-guard-recovery-' + suffix
policy = scratch / ('recovery-' + suffix + '.nft')
policy.write_text(Path('/etc/openclaw/claude-cli-network.nft').read_text().replace(runtime['guard_table'], table))
created = []


def run(args, **kwargs):
    return subprocess.check_output(args, text=True, timeout=30, **kwargs).strip()


def fixture(guard):
    name = 'guard-fixture-' + uuid.uuid4().hex[:8]
    created.append(name)
    run(['docker', 'run', '-d', '--name', name, '--network', runtime['network'], '--user', '1000:1000',
         '--cap-drop', 'ALL', '--read-only', '--security-opt', 'no-new-privileges',
         '--label', 'openclaw.claude-guard=' + guard, '--entrypoint', 'python3', runtime['image'],
         '-c', 'import time;time.sleep(300)'])
    return name


def restart_count():
    return int(run(['systemctl', 'show', unit + '.service', '-p', 'NRestarts', '--value']) or 0)


def has_rules(family):
    listed = subprocess.run(['sudo', '-n', 'nft', '--json', 'list', 'table', family, table],
                            capture_output=True, text=True, timeout=10)
    return listed.returncode == 0 and any('rule' in entry for entry in json.loads(listed.stdout)['nftables'])


def exists(name):
    return subprocess.run(['docker', 'inspect', name], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=5).returncode == 0


try:
    run(['sudo', '-n', 'systemd-run', '--unit', unit, '--property=Type=notify', '--property=NotifyAccess=main',
         '--property=Restart=on-failure', '--property=RestartSec=1', '--property=TimeoutStartSec=45',
         '/usr/bin/python3', '/usr/local/lib/openclaw/claude-cli-guard.py', str(policy), table])
    unrelated = fixture('unrelated-' + uuid.uuid4().hex[:8])
    evidence = []
    for label, modification in [('inet_table_removed', ['delete', 'table', 'inet', table]),
                                 ('bridge_table_removed', ['delete', 'table', 'bridge', table]),
                                 ('input_rules_flushed', ['flush', 'chain', 'inet', table, 'input'])]:
        affected = fixture(table)
        restarts = restart_count()
        started = time.monotonic()
        run(['sudo', '-n', 'nft', *modification])
        # The guard stops affected containers and exits; systemd restarts it,
        # and only a restarted, active guard with both tables counts.
        for _ in range(200):
            active = subprocess.run(['systemctl', 'is-active', '--quiet', unit + '.service'], timeout=5).returncode == 0
            if not exists(affected) and active and restart_count() > restarts and all(has_rules(f) for f in ['inet', 'bridge']):
                break
            time.sleep(.1)
        else:
            raise RuntimeError('Guard recovery did not complete within 20 seconds')
        item = {'case': label, 'affected_container_removed': not exists(affected),
                'other_guard_container_preserved': exists(unrelated),
                'both_guard_tables_restored': True, 'recovery_seconds': round(time.monotonic() - started, 3)}
        evidence.append(item)
        print(json.dumps(item), flush=True)
    (scratch / 'guard-recovery-evidence.json').write_text(json.dumps(evidence) + '\n')
finally:
    subprocess.run(['sudo', '-n', 'systemctl', 'stop', unit + '.service'], check=True, timeout=30)
    policy.unlink(missing_ok=True)
    # A stopped guard keeps its policy (fail closed); remove this fixture's tables.
    for family in ['inet', 'bridge']:
        subprocess.run(['sudo', '-n', 'nft', 'delete', 'table', family, table], stderr=subprocess.DEVNULL, timeout=10)
    for name in created:
        if exists(name):
            run(['docker', 'rm', '-f', name])
