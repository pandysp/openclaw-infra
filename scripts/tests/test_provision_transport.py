#!/usr/bin/env python3
"""Exercise the provisioner at external CLI boundaries, without infrastructure."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
HOST = 'openclaw-vps.example.ts.net'
PEER = {'HostName': 'openclaw-vps', 'DNSName': HOST + '.', 'Online': True,
        'sshHostKeys': ['ssh-ed25519 AAAATEST']}
CLI = '''import json,os,pathlib,re,subprocess,sys,textwrap
root=pathlib.Path(__file__).resolve().parents[1]
cmd=pathlib.Path(sys.argv[0]).name
with (root/'calls').open('a') as f:f.write(cmd+'\\n')
if cmd=='tailscale':
 assert sys.argv[1:]==['status','--json'], 'SSH readiness belongs to Ansible'
 sequence=json.loads(os.environ.get('TAILSCALE_SEQUENCE','[]'))
 count=(root/'calls').read_text().splitlines().count('tailscale')
 print(json.dumps(sequence[min(count-1,len(sequence)-1)]) if sequence else os.environ['TAILSCALE_FIXTURE'])
 sys.exit(int(os.environ.get('TAILSCALE_EXIT','0')))
if cmd=='ansible-galaxy':
 assert not any(k.startswith('PROVISION_') or k in ['AWS_SECRET_ACCESS_KEY','PULUMI_CONFIG_PASSPHRASE'] for k in os.environ), 'Installer received credentials'
 sys.exit(0)
if cmd=='sleep':sys.exit(0)
if cmd=='ansible-playbook':
 files=[pathlib.Path(a[1:]) for a in sys.argv if a.startswith('@')]
 assert len(files)==1 and files[0].stat().st_mode & 0o777==0o600
 data=json.loads(files[0].read_text())
 if os.environ.get('VALIDATE_SSH_KEY'):
  key=root/'consumer-key'
  source=pathlib.Path(os.environ['WORKSPACE_TASKS']).read_text()
  task=re.search(r'(?ms)^- name: Install deploy keys\\n.*?(?=^- name:|\\Z)',source).group()
  destination='dest: "/home/ubuntu/.ssh/{{ item.key_file }}"'
  assert task.count(destination)==1
  task=task.replace(destination,'dest: '+json.dumps(str(key)))
  playbook=root/'consume-key.yml'
  playbook.write_text('- hosts: localhost\\n  gather_facts: false\\n  vars:\\n    active_workspaces:\\n      - {agent_id: main, key_file: fixture, deploy_key_var: workspace_deploy_key}\\n  tasks:\\n'+textwrap.indent(task,'    '))
  subprocess.run([os.environ['REAL_ANSIBLE'],'-i','localhost,','-c','local',str(playbook),'-e','@'+str(files[0])],check=True,capture_output=True)
  subprocess.run(['ssh-keygen','-y','-f',str(key)],check=True,capture_output=True)
 for k,v in json.loads(os.environ['EXPECTED_SECRETS']).items():
  assert data[k]==v, 'Incorrect serialization: '+k
 keys=pathlib.Path(os.environ['OPENCLAW_SSH_KNOWN_HOSTS'])
 assert keys.stat().st_mode & 0o777==0o600
 assert keys.read_text()=='openclaw-vps.example.ts.net ssh-ed25519 AAAATEST\\n'
 assert os.environ['OPENCLAW_SSH_HOST']=='openclaw-vps.example.ts.net'
 inventory=subprocess.run([sys.executable,os.environ['INVENTORY_SCRIPT'],'--list'],check=True,capture_output=True,text=True)
 variables=json.loads(inventory.stdout)['_meta']['hostvars']['openclaw-vps.example.ts.net']
 assert variables['ansible_host_key_checking'] is True
 (root/'ansible-ran').touch()
 sys.exit(0)
sys.exit('Unexpected external CLI')
'''


class ProvisionTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='provision transport ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for directory in ['bin', 'scripts', 'ansible', '.codex']:
            (self.root/directory).mkdir()
        shutil.copy2(ROOT/'scripts/provision.sh', self.root/'scripts/provision.sh')
        fixture = self.root/'bin/fixture'
        fixture.write_text('#!'+sys.executable+'\n'+CLI)
        fixture.chmod(0o700)
        for name in ['tailscale', 'ansible-playbook', 'ansible-galaxy', 'sleep', 'ssh', 'pulumi']:
            (self.root/'bin'/name).symlink_to(fixture)
        self.key = '-----BEGIN OPENSSH PRIVATE KEY-----\nfixture-key\n-----END OPENSSH PRIVATE KEY-----\n'
        self.codex = json.dumps({'token': 'fixture-codex"\\ä'}, indent=2)+'\n'
        (self.root/'.codex/auth.json').write_text(self.codex)
        self.env = {'HOME':str(self.root), 'TMPDIR':str(self.root),
                    'PATH':str(self.root/'bin')+':'+os.environ['PATH'],
                    'FIXTURE_ROOT':str(self.root), 'INVENTORY_SCRIPT':str(ROOT/'ansible/inventory/pulumi_inventory.py'),
                    'REAL_ANSIBLE':shutil.which('ansible-playbook'),
                    'WORKSPACE_TASKS':str(ROOT/'ansible/roles/workspace/tasks/main.yml'),
                    'TAILSCALE_FIXTURE':json.dumps({'MagicDNSSuffix':'example.ts.net','Peer':{'one':PEER}}),
                    'PROVISION_GATEWAY_TOKEN':'fixture-gateway', 'PROVISION_CLAUDE_SETUP_TOKEN':'fixture-claude',
                    'PROVISION_TAILSCALE_HOSTNAME':'openclaw-vps', 'PROVISION_AGENT_IDS':'test',
                    'PROVISION_WORKSPACE_DEPLOY_KEY':self.key, 'PROVISION_WORKSPACE_TEST_DEPLOY_KEY':self.key,
                    'PROVISION_OBSIDIAN_VAULT_PASSWORD':'fixture-quote"\\slash\nline: true\nü',
                    'EXPECTED_SECRETS':json.dumps({'gateway_token':'fixture-gateway','workspace_deploy_key':self.key,
                        'workspace_test_deploy_key':self.key,'codex_auth_json':self.codex,'github_token':'',
                        'telegram_test_user_id':'','obsidian_vault_password':'fixture-quote"\\slash\nline: true\nü'})}

    def run_provisioner(self):
        result = subprocess.run(['bash',str(self.root/'scripts/provision.sh'),'--tags','openclaw'],
                                env=self.env,capture_output=True,text=True,timeout=30)
        self.assertNotIn('fixture-',result.stdout+result.stderr)
        self.assertEqual(list(self.root.glob('tmp.*')),[])
        return result

    def test_json_credentials_and_authenticated_day2_inventory(self):
        result = self.run_provisioner()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertTrue((self.root/'ansible-ran').exists())
        self.assertNotIn('ssh',(self.root/'calls').read_text().splitlines())

    def test_online_peer_waits_for_key_publication(self):
        self.env['TAILSCALE_SEQUENCE']=json.dumps([
            {'MagicDNSSuffix':'example.ts.net','Peer':{'one':PEER|{'sshHostKeys':[]}}},
            {'MagicDNSSuffix':'example.ts.net','Peer':{'one':PEER}}])
        result=self.run_provisioner()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual((self.root/'calls').read_text().splitlines().count('tailscale'),2)

    def test_shell_stripped_deploy_key_is_accepted_by_openssh(self):
        key=self.root/'generated-key'
        subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(key)],check=True,capture_output=True)
        self.env['PROVISION_WORKSPACE_DEPLOY_KEY']=key.read_text().rstrip('\n')
        expected=json.loads(self.env['EXPECTED_SECRETS']); expected['workspace_deploy_key']=key.read_text().rstrip('\n')
        self.env['EXPECTED_SECRETS']=json.dumps(expected)
        self.env['VALIDATE_SSH_KEY']='1'
        result=self.run_provisioner()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_invalid_codex_auth_fails_before_connecting(self):
        (self.root/'.codex/auth.json').write_text('fixture-invalid-secret')
        result = self.run_provisioner()
        self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.root/'calls').exists())

    def test_missing_optional_codex_auth_is_empty(self):
        (self.root/'.codex/auth.json').unlink()
        expected=json.loads(self.env['EXPECTED_SECRETS']);expected['codex_auth_json']=''
        self.env['EXPECTED_SECRETS']=json.dumps(expected)
        self.assertEqual(self.run_provisioner().returncode,0)

    def test_missing_hostname_never_uses_default(self):
        del self.env['PROVISION_TAILSCALE_HOSTNAME']
        self.assertNotEqual(self.run_provisioner().returncode,0)
        self.assertFalse((self.root/'ansible-ran').exists())

    def test_ambiguous_or_untrusted_peer_cannot_reach_ansible(self):
        variants=[{'one':PEER,'two':PEER}, {'one':PEER|{'HostName':'openclaw-vps-1'}},
                  {'one':PEER|{'DNSName':'wrong.example.ts.net.'}},
                  {'one':PEER|{'sshHostKeys':[]}}, {'one':PEER|{'sshHostKeys':['ssh-ed25519 bad\nother bad']}}]
        for peers in variants:
            with self.subTest(peers=peers):
                self.env['TAILSCALE_FIXTURE']=json.dumps({'MagicDNSSuffix':'example.ts.net','Peer':peers})
                self.assertNotEqual(self.run_provisioner().returncode,0)
                self.assertFalse((self.root/'ansible-ran').exists())

    def test_failed_or_malformed_control_plane_does_not_fall_back(self):
        for raw,code in [('{}',0), ('[]',0), ('{}\n{}',0), (self.env['TAILSCALE_FIXTURE'],1)]:
            with self.subTest(raw=raw,code=code):
                self.env['TAILSCALE_FIXTURE']=raw; self.env['TAILSCALE_EXIT']=str(code)
                self.assertNotEqual(self.run_provisioner().returncode,0)
                self.assertFalse((self.root/'ansible-ran').exists())

    def test_real_ansible_rejects_missing_inventory_inputs(self):
        playbook=self.root/'probe.yml'
        playbook.write_text('- hosts: openclaw\n  gather_facts: false\n  tasks:\n    - ansible.builtin.debug:\n        msg: MUST_NOT_RUN\n')
        env={'PATH':str(self.root/'bin')+':'+os.environ['PATH'],'HOME':str(self.root),'ANSIBLE_CONFIG':str(ROOT/'ansible/ansible.cfg')}
        result=subprocess.run([shutil.which('ansible-playbook'),'-i',str(ROOT/'ansible/inventory/pulumi_inventory.py'),str(playbook)],
                              env=env,capture_output=True,text=True,timeout=30)
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertNotIn('MUST_NOT_RUN',result.stdout)


if __name__=='__main__':
    unittest.main()
