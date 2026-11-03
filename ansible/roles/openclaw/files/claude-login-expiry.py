#!/usr/bin/env python3
"""Tell the owner, weeks ahead, that the shared Claude login is about to end.

Every agent turn uses this one login, so its end stops all agents at once.
A setup token ends at expiresAt; a `claude auth login` renews itself only
until refreshTokenExpiresAt (30 days after signing in). Runs daily and once
per provisioning run; sends a message while fewer than WARN_DAYS are left.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

credentials = Path(os.environ['CLAUDE_LOGIN_CREDENTIALS'])
warn_days = int(os.environ['CLAUDE_LOGIN_WARN_DAYS'])

oauth = json.loads(credentials.read_text()).get('claudeAiOauth') or {}
end_ms = oauth.get('refreshTokenExpiresAt') if oauth.get('refreshToken') else oauth.get('expiresAt')
if not oauth.get('accessToken') or not isinstance(end_ms, (int, float)) or end_ms <= 0:
    problem = 'is signed out'
else:
    days = (end_ms / 1000 - time.time()) / 86400
    end = time.strftime('%Y-%m-%d', time.gmtime(end_ms / 1000))
    if days >= warn_days:
        print(f'OK: the shared Claude login lasts until {end} ({int(days)} days)')
        sys.exit(0)
    problem = f'ended on {end}' if days <= 0 else f'ends on {end}, in {max(1, int(days))} days'

message = (f'The Claude login on the OpenClaw server {problem}. All agents stop answering then. '
           'Renew it: run `claude setup-token` with the agents\' Claude account, store the token with '
           '`pulumi config set claudeSetupToken --secret`, set `claudeSetupTokenExpires` to one year from today, '
           'then run ./scripts/provision.sh --tags openclaw (see AGENTS.md, Key Rotation).')
print(f'WARN: the shared Claude login {problem}; telling the owner')
subprocess.run([os.environ['OPENCLAW_BIN'], 'message', 'send',
                '--channel', os.environ['OWNER_CHANNEL'], '--target', os.environ['OWNER_TARGET'],
                '--message', message], check=True)
