#!/usr/bin/env python3
"""Real systemd/nft lifecycle with deliberately slow Docker/nft boundaries."""
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

scratch = Path(__file__).resolve().parent
suffix = uuid.uuid4().hex[:8]
table = 'openclaw_slow_' + suffix
unit = 'openclaw-guard-slow-' + suffix + '.service'
fixture = scratch / ('slow-' + suffix)
fixture.mkdir()
policy = fixture / 'policy.nft'
policy.write_text((scratch / 'network-guard-prototype.nft').read_text().replace('openclaw_claude_prototype', table))
(fake := fixture / 'docker').write_text('''#!/usr/bin/python3
import sys,time
if sys.argv[1]=='ps': time.sleep(4); print('synthetic-container')
elif sys.argv[1]=='rm': time.sleep(13)
else: raise SystemExit('Unexpected fault-injection command')
''')
fake.chmod(0o755)
(fake := fixture / 'nft').write_text('''#!/usr/bin/python3
import os,pathlib,sys,time
if '--file' in sys.argv:
 pathlib.Path(__file__).with_name('nft-applying').touch()
 time.sleep(12)
os.execv('/usr/sbin/nft',['/usr/sbin/nft',*sys.argv[1:]])
''')
fake.chmod(0o755)
try:
    subprocess.run(['sudo','-n','systemd-run','--unit',unit,'--property=Type=notify',
        '--property=TimeoutStartSec=75','--property=TimeoutStopSec=60','--property=KillMode=mixed',
        '--setenv=PATH='+str(fixture)+':/usr/bin:/usr/sbin:/bin',
        '/usr/bin/python3',str(scratch/'claude-cli-guard.py'),str(policy),table],
        check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=70)
    # Flushing our own fixture table then stopping tests final restoration,
    # not a second copy of the already-installed startup policy.
    subprocess.run(['sudo','-n','/usr/sbin/nft','flush','chain','inet',table,'input'],check=True)
    marker=fixture/'nft-applying'
    marker.unlink()
    start=time.monotonic()
    during_restore = sys.argv[1:] == ['--stop-during-restore']
    if during_restore:
        subprocess.run(['sudo','-n','systemctl','kill','--kill-whom=main','--signal=SIGTERM',unit],check=True)
        deadline=time.monotonic()+40
        while not marker.exists():
            if time.monotonic()>deadline: raise RuntimeError('Final nft restoration never started')
            time.sleep(.05)
    subprocess.run(['sudo','-n','systemctl','stop',unit],check=True,timeout=65)
    elapsed=time.monotonic()-start
    state=subprocess.check_output(['systemctl','show',unit,'-p','Result','--value'],text=True).strip()
    restored=json.loads(subprocess.check_output(['sudo','-n','/usr/sbin/nft','--json','list','table','inet',table],text=True))
    rules=[x['rule'] for x in restored['nftables'] if 'rule' in x and x['rule']['chain']=='input']
    evidence={'slow_shutdown_seconds':round(elapsed,3),'exceeded_old_25s_deadline':elapsed>25,
              'systemd_exit_success':state=='success','canonical_input_restored':len(rules)>=4}
    if during_restore: evidence['stop_requested_during_final_nft_restoration']=True
    (scratch/'guard-slow-cleanup-evidence.json').write_text(json.dumps(evidence)+'\n')
    print(json.dumps(evidence))
    if not all(v for k,v in evidence.items() if k!='slow_shutdown_seconds'):
        raise RuntimeError('Slow guard cleanup acceptance failed')
finally:
    loaded = subprocess.run(['systemctl','show',unit,'-p','LoadState','--value'],capture_output=True,text=True)
    if loaded.stdout.strip() == 'loaded':
        subprocess.run(['sudo','-n','systemctl','stop',unit],check=True,timeout=65)
    for family in ('inet','bridge'):
        subprocess.run(['sudo','-n','/usr/sbin/nft','destroy','table',family,table],check=True)
    for file in fixture.iterdir():file.unlink()
    fixture.rmdir()
