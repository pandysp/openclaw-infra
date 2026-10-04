#!/usr/bin/env python3
"""Real user-systemd cutover recovery after a validated fixture-driver SIGKILL.

Usage: shared-auth-recovery-trial.py HELPER.cjs
Only an owned fixture service/home are used; production gateway/auth are untouched.
The checksum codec must be next to the supplied helper.
"""
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

if len(sys.argv) not in (2, 3) or sys.platform != 'linux':
    raise SystemExit('Usage (Linux): shared-auth-recovery-trial.py HELPER.cjs [STARTUP.service.j2]')
source = Path(sys.argv[1]).resolve(strict=True)
home = Path.home()
suffix = uuid.uuid4().hex[:12]
service = 'openclaw-auth-fixture-' + suffix + '.service'
recovery = 'openclaw-auth-recovery-' + suffix
unit = home / '.config/systemd/user' / service
startup = home / '.config/systemd/user' / ('openclaw-auth-startup-fixture-' + suffix + '.service')
root = Path(tempfile.mkdtemp(prefix='openclaw-auth-fixture-'))
fixture_home = root / 'home'
legacy = fixture_home / '.claude'
storage = legacy / 'shared/auth'
legacy.mkdir(parents=True)
storage.mkdir(parents=True)
(fixture_home / '.config/openclaw').mkdir(parents=True)
modules = fixture_home / '.npm-global/lib/node_modules/openclaw'
modules.mkdir(parents=True)
(modules / 'node_modules').symlink_to(home / '.npm-global/lib/node_modules/openclaw/node_modules', target_is_directory=True)
helper = root / 'claude-oauth-seed.cjs'
shutil.copy2(source, helper)
shutil.copy2(source.with_suffix('.py'), root / 'claude-oauth-seed.py')
blob = json.dumps({'claudeAiOauth': {'accessToken': 'synthetic-fixture-access', 'refreshToken': ''}}).encode()
(legacy / '.credentials.json').write_bytes(blob)
environment_file = fixture_home / '.config/openclaw/claude-auth.env'
environment_file.write_text('CLAUDE_SECURESTORAGE_CONFIG_DIR=' + str(legacy) + '\n')
unit.write_text('[Service]\nExecStart=/bin/sleep infinity\nEnvironmentFile=' + str(environment_file) + '\n')
env = {**os.environ, 'XDG_RUNTIME_DIR': '/run/user/' + str(os.getuid())}
driver = owner_fd = owner_started = None


def command(args, timeout=30):
    result = subprocess.run(args, capture_output=True, text=True, env=env, timeout=timeout)
    if result.returncode:
        raise RuntimeError('Owned authentication recovery fixture failed; diagnostics withheld')
    return result.stdout.strip()


def active():
    p = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', service], env=env, capture_output=True)
    if p.returncode not in (0, 3):
        raise RuntimeError('Fixture service state check failed')
    return p.returncode == 0


try:
    production_before = command(['systemctl', '--user', 'show', '--property=InvocationID', '--value', 'openclaw-gateway'])
    command(['systemctl', '--user', 'daemon-reload'])
    command(['systemctl', '--user', 'start', service])
    code = '''import json,os,pathlib,subprocess,sys,time
h,helper,legacy,storage,service,recovery=sys.argv[1:]
e=dict(os.environ,HOME=h)
subprocess.run(['systemd-run','--user','--unit='+recovery,'--on-active=5s',
 '--timer-property=OnUnitInactiveSec=5s','--timer-property=AccuracySec=1s','--timer-property=RandomizedDelaySec=0',
 '/usr/bin/env','HOME='+h,'/usr/bin/node',helper,'recover',legacy,storage,service],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
deadline=time.monotonic()+12
while subprocess.check_output(['systemctl','--user','show',recovery+'.timer','--property=LastTriggerUSecMonotonic','--value'],text=True).strip() in ['', '0', 'n/a']:
 if time.monotonic()>deadline:raise RuntimeError('First recovery firing did not occur')
 time.sleep(.1)
while subprocess.check_output(['systemctl','--user','show',recovery+'.service','--property=ActiveState','--value'],text=True).strip() != 'inactive':
 if time.monotonic()>deadline:raise RuntimeError('First recovery firing did not complete successfully')
 time.sleep(.1)
subprocess.run(['systemctl','--user','stop',service],check=True)
subprocess.run(['/usr/bin/node',helper,'migrate',legacy,storage],env=e,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
started=pathlib.Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19]
print(json.dumps({'pid':os.getpid(),'starttime':started,'earlier_recovery_firing_completed':True}),flush=True)
time.sleep(120)
'''
    driver = subprocess.Popen(['python3', '-c', code, str(fixture_home), str(helper), str(legacy), str(storage), service, recovery],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    owner_started = Path('/proc/' + str(driver.pid) + '/stat').read_text().rsplit(')', 1)[1].split()[19]
    owner_fd = os.pidfd_open(driver.pid)
    readable, _, _ = select.select([driver.stdout], [], [], 15)
    if not readable:
        raise RuntimeError('Fixture driver never completed its guarded cutover')
    line = driver.stdout.readline()
    if not line:
        driver.wait(timeout=5)
        raise RuntimeError('Fixture driver exited before cutover ownership handshake; private diagnostics withheld')
    owner = json.loads(line)
    if owner['pid'] != driver.pid:
        raise RuntimeError('Fixture PID ownership mismatch')
    actual = Path('/proc/' + str(driver.pid) + '/stat').read_text().rsplit(')', 1)[1].split()[19]
    if actual != owner['starttime'] or actual != owner_started or active():
        raise RuntimeError('Fixture did not reach the intended stopped-gateway window')
    signal.pidfd_send_signal(owner_fd, signal.SIGKILL)
    driver.wait(timeout=5)
    deadline = time.monotonic() + 30
    while not active() and time.monotonic() < deadline:
        time.sleep(0.2)
    evidence = {
        'host_timer_survived_driver_sigkill': active(),
        'recurring_timer_recovered_after_earlier_firing': owner['earlier_recovery_firing_completed'],
        'preserved_login_bytes_equal': hashlib.sha256((storage / '.credentials.json').read_bytes()).digest() == hashlib.sha256(blob).digest(),
        'authoritative_storage_selected': environment_file.read_text() == 'CLAUDE_SECURESTORAGE_CONFIG_DIR=' + str(storage) + '\n',
        'production_gateway_untouched': bool(production_before) and production_before == command(['systemctl', '--user', 'show', '--property=InvocationID', '--value', 'openclaw-gateway']),
    }
    if not all(evidence.values()):
        raise RuntimeError('Independent auth cutover recovery acceptance failed')
    if len(sys.argv) == 3:
        command(['systemctl', '--user', 'stop', recovery + '.timer'])
        deadline = time.monotonic() + 10
        while command(['systemctl', '--user', 'show', recovery + '.service', '--property=ActiveState', '--value']) in ['active', 'activating']:
            if time.monotonic() > deadline:
                raise RuntimeError('Recurring recovery service did not finish before start-time trial')
            time.sleep(.1)
        command(['systemctl', '--user', 'stop', service])
        (storage / '.credentials.json').replace(legacy / '.credentials.json')
        (storage / '.migration.json').unlink()
        environment_file.write_text('CLAUDE_SECURESTORAGE_CONFIG_DIR=' + str(legacy) + '\n')
        template = Path(sys.argv[2]).read_text().replace('/home/ubuntu/.local/bin/claude-oauth-seed', str(helper)).replace('/home/ubuntu', str(fixture_home))
        template = template.replace('{{ claude_secure_storage_dir }}', str(storage)).replace('openclaw-gateway.service', service)
        startup.write_text(template + '\nEnvironment=HOME=' + str(fixture_home) + '\n')
        consumer = root / 'consumer.py'
        consumer.write_text("import os,pathlib,json,time\np=pathlib.Path(os.environ['CLAUDE_SECURESTORAGE_CONFIG_DIR'])/'.credentials.json'\npathlib.Path(" + repr(str(root / 'startup-consumed.json')) + ").write_text(json.dumps({'login_exists':p.exists()}))\ntime.sleep(120)\n")
        unit.write_text('[Unit]\nRequires=' + startup.name + '\nAfter=' + startup.name + '\n[Service]\nExecStart=/usr/bin/python3 ' + str(consumer) + '\nEnvironmentFile=' + str(environment_file) + '\n')
        command(['systemctl', '--user', 'daemon-reload'])
        command(['/usr/bin/env', 'HOME=' + str(fixture_home), '/usr/bin/node', str(helper), 'migrate', str(legacy), str(storage)])
        # The recurring timer was stopped. Only the persistent dependency
        # can select the surviving login before this service consumes its env.
        command(['systemctl', '--user', 'start', service])
        deadline = time.monotonic() + 10
        while not (root / 'startup-consumed.json').exists() and time.monotonic() < deadline:
            time.sleep(.1)
        evidence['start_time_recovery_without_timer'] = json.loads((root / 'startup-consumed.json').read_text())['login_exists']
        if not evidence['start_time_recovery_without_timer']:
            raise RuntimeError('Persistent start-time auth recovery failed')
    evidence['production_gateway_untouched'] = bool(production_before) and production_before == command(['systemctl', '--user', 'show', '--property=InvocationID', '--value', 'openclaw-gateway'])
    if not evidence['production_gateway_untouched']:
        raise RuntimeError('Production gateway changed during the complete fixture trial')
    print(json.dumps(evidence))
    Path(__file__).with_name('shared-auth-recovery-evidence.json').write_text(json.dumps(evidence) + '\n')
finally:
    if driver is not None:
        if driver.poll() is None:
            if owner_fd is None:
                owner_fd = os.pidfd_open(driver.pid)
            actual = Path('/proc/' + str(driver.pid) + '/stat').read_text().rsplit(')', 1)[1].split()[19]
            if actual != owner_started:
                raise RuntimeError('Refusing cleanup kill after fixture ownership changed')
            signal.pidfd_send_signal(owner_fd, signal.SIGKILL)
        driver.wait(timeout=5)
        driver.stdout.close()
        driver.stderr.close()
    if owner_fd is not None:
        os.close(owner_fd)
    for owned in [recovery + '.timer', recovery + '.service', service, startup.name]:
        stopped = subprocess.run(['systemctl', '--user', 'stop', owned], env=env, capture_output=True, text=True)
        if stopped.returncode and 'not loaded' not in stopped.stderr and 'not found' not in stopped.stderr:
            raise RuntimeError('Owned authentication fixture cleanup failed')
    unit.unlink()
    startup.unlink(missing_ok=True)
    command(['systemctl', '--user', 'daemon-reload'])
    shutil.rmtree(root)
