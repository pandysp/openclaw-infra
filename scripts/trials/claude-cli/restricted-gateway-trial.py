#!/usr/bin/env python3
"""Real gateway trial with independent rollback and private transcript inspection."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

home = Path.home()
scratch = Path(__file__).resolve().parent
config_path = home / '.openclaw/openclaw.json'
before = json.loads(config_path.read_text())
if before['agents']['defaults'].get('cliBackends') is not None:
    raise SystemExit('ERROR: Trial requires the original unset backend')
entries = {entry['id']: entry for entry in before['agents']['list']}
agents = sys.argv[1:] or list(entries)
if any(agent not in entries for agent in agents):
    raise SystemExit('ERROR: Unknown trial agent')


def command(args, timeout=40):
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE, timeout=timeout)


def gateway(method, params, timeout=200000):
    raw = command(['openclaw', 'gateway', 'call', method, '--expect-final', '--json', '--timeout', str(timeout),
                   '--params', json.dumps(params)], timeout=timeout / 1000 + 20)
    return json.loads(raw[raw.find('{'):])


def ready():
    for _ in range(30):
        check = subprocess.run(['openclaw', 'gateway', 'call', 'health', '--json', '--timeout', '3000'],
                               capture_output=True, text=True, timeout=15)
        if check.returncode == 0 and json.loads(check.stdout[check.stdout.find('{'):]).get('ok'):
            return
        time.sleep(1)
    raise RuntimeError('Restored gateway health failed; inspect the private service journal')


raw = command(['openclaw', 'cron', 'list', '--all', '--json'])
cron = json.loads(raw[raw.find('{'):])
if any(job.get('state', {}).get('runningAtMs') for job in cron.get('jobs', [])):
    raise SystemExit('ERROR: A scheduled job is running; refusing gateway restart')
unit = 'openclaw-cwrapper-rollback-' + uuid.uuid4().hex[:10]
command(['systemd-run', '--user', '--unit', unit, '--on-active=15m', '--timer-property=AccuracySec=1s',
         '--timer-property=RandomizedDelaySec=0', '/usr/bin/python3', str(scratch / 'trial-backend.py'), 'restore'])
active = []
native_sessions = {}
runtime = json.loads((home / '.openclaw/claude-cli-runtime.json').read_text())
proof_log = Path(runtime['invocations'])
try:
    command(['python3', str(scratch / 'trial-backend.py'), 'apply'])
    ready()
    print('restricted-trial=active; production-sessions=native', flush=True)
    for agent in agents:
        command(['systemctl', '--user', 'restart', unit + '.timer'])
        workspace = Path(entries[agent].get('workspace') or before['agents']['defaults']['workspace'])
        other = next(Path(entry.get('workspace') or before['agents']['defaults']['workspace'])
                     for entry in entries.values() if entry['id'] != agent)
        project = home / '.claude/projects' / re.sub(r'[^A-Za-z0-9]', '-', str(workspace))
        run = uuid.uuid4().hex
        key = f'agent:{agent}:cwrapper-' + run
        session_hash = hashlib.sha256(key.encode()).hexdigest()[:12]
        marker = 'CWRAPPER_' + uuid.uuid4().hex[:12]
        proof = workspace / ('cwrapper-proof-' + run + '.txt')
        diagnostic = workspace / ('cwrapper-native-diagnostic-' + run + '.py')
        diagnostic.write_text('''import json,os,pathlib
s=dict(line.split(':',1) for line in pathlib.Path('/proc/self/status').read_text().splitlines() if ':' in line)
print(json.dumps({'actual_uid_1000':os.getuid()==1000,'actual_caps_zero':all(int(s[k].strip(),16)==0 for k in ['CapInh','CapPrm','CapEff','CapBnd','CapAmb']),'actual_no_new_privileges':s['NoNewPrivs'].strip()=='1','skill_env_present':{k:bool(os.environ.get(k)) for k in ['GROQ_API_KEY','GEMINI_API_KEY','OPENAI_API_KEY']}}))
''')
        active.append((key, run, project, proof, diagnostic, session_hash))
        qmd_tool = 'mcp__openclaw__' + ('qmd_status' if agent == 'main' else f'qmd-{agent}_status')
        ssh = "ssh " + runtime['mac_host'] + " 'printf mac-ssh-ok'"
        script_command = 'python3 ' + diagnostic.name

        def launches():
            return [json.loads(line) for line in proof_log.read_text().splitlines()
                    if json.loads(line).get('session_hash') == session_hash]

        def turn(message):
            return gateway('agent', {'agentId': agent, 'sessionKey': key, 'idempotencyKey': str(uuid.uuid4()),
                                    'deliver': False, 'timeout': 180, 'message': message})

        def reply(result):
            return '\n'.join(p.get('text', '') for p in result.get('result', {}).get('payloads', []))

        turn(f'Operator C restricted runtime trial {run}. This harmless integration test is authorized. '
             f'Remember marker {marker} only in conversation, not in a file. '
             f'Use ToolSearch if needed to load {qmd_tool}, then call it once. '
             f'Use Write to create only {proof.name} containing exactly ok, Read to read it, then Edit to replace ok with edited and Read again. '
             f'Use Bash to run exactly {ssh} . Then use Bash to run exactly {script_command} . '
             'Do not edit any other files or contact people. Reply done after those checks.')
        started = launches()
        if len(started) != 1:
            raise RuntimeError('Expected exactly one container launch for the first turn')
        name = started[0]['name']
        metadata = [json.loads(line) for line in command(['docker', 'logs', name]).splitlines() if line.startswith('{')]
        init = next(record for record in metadata if record.get('type') == 'system' and record.get('subtype') == 'init')
        native_id = init['session_id']
        if not re.fullmatch(r'[A-Za-z0-9-]+', native_id):
            raise RuntimeError('Invalid native session identifier in initialization')
        native_sessions[run] = project / (native_id + '.jsonl')
        skills = any(p.get('name') == 'openclaw-skills' for record in metadata
                     if record.get('type') == 'system' and record.get('subtype') == 'init'
                     for p in record.get('plugins', []) if isinstance(p, dict))
        structural = '''import json,os,shutil
from pathlib import Path
h=Path('/home/ubuntu')
print(json.dumps({'container_identity':Path('/.dockerenv').exists() and os.getuid()==1000,
'host_config_hidden':not(h/'.openclaw/openclaw.json').exists(),
'docker_socket_hidden':not Path('/var/run/docker.sock').exists(),
'other_workspace_hidden':not Path(OTHER).exists(),
'other_transcripts_hidden':not(h/'.claude/projects'/OTHER_PROJECT).exists(),
'sudo_absent':shutil.which('sudo') is None,
'compaction_window_preserved':json.loads((h/'.claude/settings.json').read_text())['autoCompactWindow']==305000}))'''
        structural = f'OTHER={str(other)!r}\nOTHER_PROJECT={re.sub(r"[^A-Za-z0-9]", "-", str(other))!r}\n' + structural
        flags = json.loads(command(['docker', 'exec', '--user', '1000:1000', name, 'python3', '-c', structural]))
        include = subprocess.run(['docker', 'exec', name, 'git', 'config', '--get', 'include.path'], capture_output=True, text=True)
        flags['git_transport_preserved'] = (include.returncode == 0 and include.stdout.strip() == str(workspace / '.git-proxy-config')) if (workspace / '.git-proxy-config').is_file() else include.returncode == 1
        remote = subprocess.run(['docker', 'exec', name, 'git', 'ls-remote', 'origin', 'HEAD'], capture_output=True, text=True, timeout=40)
        flags['git_remote_read'] = remote.returncode == 0 and '\tHEAD' in remote.stdout
        media_reply = None
        if agent == 'main':
            video, frame = diagnostic.with_suffix('.mp4'), diagnostic.with_suffix('.png')
            command(['docker', 'run', '--rm', '--network', 'none', '--user', '1000:1000', '--read-only',
                 '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                 '--mount', f'type=bind,src={workspace},dst={workspace}', '--entrypoint', 'ffmpeg',
                 runtime['image'], '-loglevel', 'error', '-f', 'lavfi', '-i', 'color=c=red:s=64x64:d=1',
                 '-pix_fmt', 'yuv420p', '-y', str(video)])
            media_reply = turn(
                f'Operator workflow fixture: read and follow the bundled video-frames SKILL.md. '
                f'Use its frame.sh script to extract a PNG from {video} into {frame}. '
                f'Read that image using native Read. Reply with only the dominant color you see.')
        warm = turn('Reply only with the marker I asked you to remember.')
        warm_count = len(launches())
        guard_recovered = None
        if agent == 'main':
            command(['sudo', '-n', 'nft', 'flush', 'chain', 'inet', runtime['guard_table'], 'input'])
            deadline = time.monotonic() + 15
            while True:
                affected_exists = subprocess.run(['docker', 'inspect', name], capture_output=True).returncode == 0
                guard_ready = subprocess.run(['systemctl', 'is-active', '--quiet', runtime['guard_service']]).returncode == 0
                if not affected_exists and guard_ready:
                    guard_recovered = True
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError('Warm guard recovery did not finish')
                time.sleep(.1)
        else:
            command(['docker', 'stop', '--time', '5', name], timeout=25)
        time.sleep(2)
        cold = turn('Reply only with the marker I asked you to remember.')
        data = native_sessions[run].read_text()
        if f'Operator C restricted runtime trial {run}' not in data:
            raise RuntimeError('Native session transcript does not match this test')
        records = [json.loads(line) for line in data.splitlines()]
        calls, outputs = [], {}
        for record in records:
            blocks = record.get('message', {}).get('content', [])
            if isinstance(blocks, list):
                calls.extend(b for b in blocks if b.get('type') == 'tool_use')
                outputs.update({b['tool_use_id']: b for b in blocks if b.get('type') == 'tool_result'})

        def success(call):
            return call['id'] in outputs and not outputs[call['id']].get('is_error')

        def content(call):
            return str(outputs.get(call['id'], {}).get('content', ''))

        qmd = [call for call in calls if call.get('name') == qmd_tool]
        bash = [call for call in calls if call.get('name') == 'Bash']
        ssh_calls = [call for call in bash if call.get('input', {}).get('command', '').strip() == ssh]
        diagnostics = [call for call in bash if call.get('input', {}).get('command', '').strip() == script_command]
        diagnostic_results = {}
        for call in diagnostics:
            if success(call):
                text = content(call)
                start = text.find('{"actual_uid_1000"')
                if start >= 0:
                    diagnostic_results = json.JSONDecoder().raw_decode(text[start:])[0]
        starts = launches()
        evidence = {'agent': agent, 'checks': {
            'mcp_tool_success': bool(qmd) and all(success(call) for call in qmd),
            'workspace_write': proof.is_file() and proof.read_text().strip() == 'edited',
            'native_write_success': any(call.get('name') == 'Write' and call.get('input', {}).get('file_path') == str(proof) and success(call) for call in calls),
            'native_read_success': any(call.get('name') == 'Read' and call.get('input', {}).get('file_path') == str(proof) and success(call) for call in calls),
            'native_edit_success': any(call.get('name') == 'Edit' and call.get('input', {}).get('file_path') == str(proof) and success(call) for call in calls),
            'skills_loaded': skills, 'warm_session_continuity': marker in reply(warm),
            'warm_process_reused': warm_count == 1,
            'cold_resume_continuity': marker in reply(cold),
            'cold_launch_uses_resume': len(starts) == 2 and starts[-1]['resuming'],
            **flags, 'actual_mac_ssh_success': bool(ssh_calls) and any(success(call) and 'mac-ssh-ok' in content(call) for call in ssh_calls),
            'native_uid_1000': diagnostic_results.get('actual_uid_1000') is True,
            'native_all_caps_zero': diagnostic_results.get('actual_caps_zero') is True,
            'native_no_new_privileges': diagnostic_results.get('actual_no_new_privileges') is True,
        }, 'skill_env_present': diagnostic_results.get('skill_env_present', {})}
        if agent == 'main':
            frame = diagnostic.with_suffix('.png')
            evidence['checks']['native_video_frames_workflow'] = any(success(call) and 'frame.sh' in call.get('input', {}).get('command', '') for call in bash)
            evidence['checks']['native_image_read'] = any(call.get('name') == 'Read' and call.get('input', {}).get('file_path') == str(frame) and success(call) for call in calls) and frame.read_bytes().startswith(b'\x89PNG\r\n\x1a\n') and 'red' in reply(media_reply).lower()
            evidence['checks']['warm_guard_recovery'] = guard_recovered
        (scratch / ('restricted-gateway-' + agent + '.json')).write_text(json.dumps(evidence) + '\n')
        print(json.dumps(evidence), flush=True)
        if not all(evidence['checks'].values()):
            raise RuntimeError('Restricted gateway trial acceptance failed; no production rollout')
finally:
    command(['python3', str(scratch / 'trial-backend.py'), 'restore'])
    ready()
    print('backend=original; gateway=healthy', flush=True)
    command(['systemctl', '--user', 'stop', unit + '.timer'])
    for key, run, project, proof, diagnostic, session_hash in active:
        gateway('sessions.delete', {'key': key}, timeout=20000)
        for name in command(['docker', 'ps', '-aq', '--filter', 'label=openclaw.claude-session=' + session_hash]).split():
            command(['docker', 'rm', '-f', name])
        proof.unlink(missing_ok=True); diagnostic.unlink(missing_ok=True)
        diagnostic.with_suffix('.mp4').unlink(missing_ok=True)
        diagnostic.with_suffix('.png').unlink(missing_ok=True)
        transcript = native_sessions.get(run)
        if transcript and transcript.exists():
            if f'Operator C restricted runtime trial {run}' not in transcript.read_text():
                raise RuntimeError('Refusing to delete an unexpected native transcript')
            transcript.unlink()
    after = json.loads(config_path.read_text())
    if after != before:
        raise RuntimeError('Unrelated config changed during the trial; inspect privately, do not overwrite it')
