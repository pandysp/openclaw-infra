"""Runs inside an agent's container during a gateway trial turn and prints booleans only.

From OpenClaw 2026.7.1 the container ends with its turn, so every in-container check
happens here, inside a real turn, instead of through docker exec afterwards. The
trial prepends SETTINGS and asks the agent to run the copy with Bash.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess

HOME = Path('/home/ubuntu')


# Three Git calls at 30 s each stay inside Claude Code's two-minute Bash timeout,
# so the branch deletion in git_checks always gets its turn.
def run(*args, timeout=30):
    return subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)


def privileges(status_text):
    status = dict(line.split(':', 1) for line in status_text.splitlines() if ':' in line)
    return {
        'actual_uid_1000': os.getuid() == 1000,
        'actual_caps_zero': all(int(status[k].strip(), 16) == 0 for k in ['CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb']),
        'actual_no_new_privileges': status['NoNewPrivs'].strip() == '1',
    }


def isolation(home, other_workspace, other_project):
    return {
        'container_identity': Path('/.dockerenv').exists() and os.getuid() == 1000,
        'host_config_hidden': not (home / '.openclaw/openclaw.json').exists(),
        'docker_socket_hidden': not Path('/var/run/docker.sock').exists(),
        'other_workspace_hidden': not Path(other_workspace).exists(),
        'other_transcripts_hidden': not (home / '.claude/projects' / other_project).exists(),
        'sudo_absent': shutil.which('sudo') is None,
        'compaction_window_preserved': json.loads((home / '.claude/settings.json').read_text())['autoCompactWindow'] == 305000,
    }


def git_checks(proxy_config, branch):
    include = run('git', 'config', '--get', 'include.path')
    remote = run('git', 'ls-remote', 'origin', 'HEAD')
    checks = {
        'git_transport_preserved': (include.returncode == 0 and include.stdout.strip() == proxy_config)
        if proxy_config else include.returncode == 1,
        'git_remote_read': remote.returncode == 0 and '\tHEAD' in remote.stdout,
        'git_push': False,
    }
    try:
        checks['git_push'] = run('git', 'push', 'origin', 'HEAD:refs/heads/' + branch).returncode == 0
    finally:
        # A timed-out push may still have created the branch, so always try to delete it.
        deleted = run('git', 'push', 'origin', '--delete', branch)
        checks['git_branch_removed'] = deleted.returncode == 0 or (
            not checks['git_push'] and 'remote ref does not exist' in deleted.stderr)
    return checks


def mac_absence(home, hosts_file, mac_host):
    # Docker leaves empty mountpoint files in persistent homes; only content counts.
    def has_content(path):
        return path.exists() and path.stat().st_size > 0

    ssh = home / '.ssh'
    config = ssh / 'config'
    checks = {
        'no_mac_key_or_pin': not has_content(ssh / 'id_ed25519_openclaw_mac_air')
        and not has_content(ssh / 'known_hosts_openclaw_mac_air'),
        # A missing SSH config is not proof of denial: the launcher always mounts one.
        'no_mac_ssh_block': config.is_file() and 'openclaw_mac_air' not in config.read_text(),
    }
    if mac_host:
        # Docker writes --add-host entries into the container's /etc/hosts.
        checks['no_mac_host_entry'] = not any(
            mac_host in line.split()[1:] for line in hosts_file.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith('#'))
    return checks


def probe(settings):
    result = privileges(Path('/proc/self/status').read_text())
    result['skill_env_present'] = {k: bool(os.environ.get(k)) for k in ['GROQ_API_KEY', 'GEMINI_API_KEY', 'OPENAI_API_KEY']}
    result.update(isolation(HOME, settings['other_workspace'], settings['other_project']))
    result.update(git_checks(settings['proxy_config'], settings['branch']))
    if settings['check_mac_absence']:
        result.update(mac_absence(HOME, Path('/etc/hosts'), settings['mac_host']))
    return result


if __name__ == '__main__':
    print(json.dumps(probe(SETTINGS)))  # noqa: F821 - prepended by the trial
