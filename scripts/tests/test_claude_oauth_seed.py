"""Consumer lifecycle and actual proper-lockfile refresh/provision concurrency."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'ansible/roles/openclaw/files/claude-oauth-seed.cjs'
MODULES = ROOT / 'node_modules'


class OAuthSeedTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        installed = self.home / '.npm-global/lib/node_modules/openclaw'
        installed.mkdir(parents=True)
        (installed / 'node_modules').symlink_to(MODULES, target_is_directory=True)
        self.storage = self.home / 'shared/auth'
        self.storage.mkdir(parents=True)
        self.source = self.home / 'source'
        self.live = self.storage / '.credentials.json'
        self.version = self.storage / '.credentials-seed.sha256'
        # The Linux process probe is an OS boundary; Darwin's ps uses other
        # flags. Data/lock/rename tests have no native writers in this fixture.
        bin_dir = self.home/'bin'
        bin_dir.mkdir()
        probe = bin_dir/'ps'
        probe.write_text('#!/bin/sh\nexit 1\n')
        probe.chmod(0o700)
        self.env = {**os.environ, 'HOME': str(self.home), 'PATH': str(bin_dir) + os.pathsep + os.environ['PATH']}
        self.assertTrue((MODULES / 'proper-lockfile').is_dir(), 'Run npm ci before the OAuth tests')

    def blob(self, name, refresh=True):
        return json.dumps({'claudeAiOauth': {'accessToken': 'synthetic-access-' + name,
                           'refreshToken': 'synthetic-refresh-' + name if refresh else ''}}).encode()

    def run_seed(self, rotate=False, adopt=False, request=None):
        result = subprocess.run(['node', str(SOURCE), str(self.source), str(self.live), str(self.version),
                                 *(['--rotate=' + request] if request is not None else ['--rotate'] if rotate else []), *(['--adopt'] if adopt else [])], env=self.env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_seed_refresh_reprovision_rotate_and_refresh_again(self):
        self.source.write_bytes(self.blob('initial'))
        self.assertEqual(self.run_seed(), 'SEEDED')
        self.live.write_bytes(self.blob('refreshed'))
        self.assertEqual(self.run_seed(), 'PRESERVED')
        self.assertEqual(self.live.read_bytes(), self.blob('refreshed'))
        self.source.write_bytes(self.blob('rotation'))
        self.assertEqual(self.run_seed(), 'ROTATED')
        self.live.write_bytes(self.blob('refreshed-again'))
        self.assertEqual(self.run_seed(), 'PRESERVED')
        self.assertEqual(self.live.read_bytes(), self.blob('refreshed-again'))
        self.live.unlink()
        self.assertEqual(self.run_seed(), 'SEEDED')
        for file in [self.live, self.version]:
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)

    def test_adoption_and_explicit_rotation(self):
        self.source.write_bytes(self.blob('configured'))
        self.live.write_bytes(self.blob('untracked-refreshed'))
        self.assertEqual(self.run_seed(), 'ADOPTED')
        self.assertEqual(self.live.read_bytes(), self.blob('untracked-refreshed'))
        self.assertEqual(self.run_seed(), 'PRESERVED')
        self.assertEqual(self.run_seed(rotate=True), 'ROTATED')
        self.assertEqual(self.live.read_bytes(), self.source.read_bytes())

    def test_setup_token_adopts_identical_login_and_rotates_oauth_source(self):
        self.source.write_bytes(self.blob('setup', refresh=False))
        self.live.write_bytes(self.source.read_bytes())
        self.assertEqual(self.run_seed(), 'ADOPTED')
        self.assertEqual(self.run_seed(), 'PRESERVED')
        self.live.write_bytes(self.blob('login'))
        self.version.unlink()
        self.assertEqual(self.run_seed(), 'ROTATED')
        self.assertEqual(self.live.read_bytes(), self.source.read_bytes())

    def test_invalid_source_never_changes_credentials_or_acquires_locks(self):
        self.source.write_text('{}')
        self.live.write_bytes(self.blob('existing'))
        result = subprocess.run(['node', str(SOURCE), str(self.source), str(self.live), str(self.version)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.live.read_bytes(), self.blob('existing'))
        self.assertFalse((self.storage / '.oauth_refresh.lock').exists())
        self.assertFalse(Path(str(self.storage) + '.lock').exists())
        self.assertNotIn('synthetic-', result.stdout + result.stderr)

    def test_refresh_and_rotation_serialize_and_heartbeat_advances(self):
        self.source.write_bytes(self.blob('old'))
        self.assertEqual(self.run_seed(), 'SEEDED')
        self.source.write_bytes(self.blob('new'))
        ready = self.home / 'ready'
        release = self.home / 'release'
        refresher = subprocess.Popen(['node', '-e', '''
const fs=require('node:fs/promises'),p=require('node:path');
const lockfile=require(process.argv[1]+'/proper-lockfile');
(async()=>{const dir=process.argv[2],releases=[];
for(const lockfilePath of [p.join(dir,'.oauth_refresh.lock'),dir+'.lock'])
 releases.push(await lockfile.lock(lockfilePath===dir+'.lock'?lockfilePath:dir,{lockfilePath,realpath:false,stale:60000,update:5000}));
const live=p.join(dir,'.credentials.json');const blob=JSON.parse(await fs.readFile(live));
await fs.writeFile(process.argv[3],'ready');
while(true){try{await fs.stat(process.argv[4]);break;}catch(e){if(e.code!=='ENOENT')throw e;}
 await new Promise(r=>setTimeout(r,50));}
blob.claudeAiOauth.accessToken='synthetic-refreshed-old';await fs.writeFile(live,JSON.stringify(blob));
for(const unlock of releases.reverse())await unlock();
})().catch(()=>{process.exitCode=1;});
''', str(MODULES), str(self.storage), str(ready), str(release)],
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        seeder = None
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(ready.exists(), 'Refresher never acquired its real locks')
            first = (self.storage / '.oauth_refresh.lock').stat().st_mtime_ns
            seeder = subprocess.Popen(['node', str(SOURCE), str(self.source), str(self.live), str(self.version)],
                                      env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            time.sleep(6)
            self.assertIsNone(seeder.poll(), 'Provisioning did not wait for the SDK-style locks')
            self.assertGreater((self.storage / '.oauth_refresh.lock').stat().st_mtime_ns, first)
            release.write_text('release')
            self.assertEqual(refresher.wait(timeout=5), 0)
            out, err = seeder.communicate(timeout=10)
            self.assertEqual(seeder.returncode, 0, err)
            self.assertEqual(out.strip(), 'ROTATED')
            self.assertEqual(self.live.read_bytes(), self.blob('new'))
            self.assertEqual(self.run_seed(), 'PRESERVED')
        finally:
            release.touch()
            for child in [refresher, seeder]:
                if child is not None and child.poll() is None:
                    child.kill()
                if child is not None:
                    child.wait(timeout=5)
            if seeder is not None:
                seeder.stdout.close(); seeder.stderr.close()

    def test_partial_version_write_failure_is_visible_and_retry_recovers(self):
        self.source.write_bytes(self.blob('new', refresh=False))
        self.live.write_bytes(self.blob('old', refresh=False))
        script = '''
const fs=require('node:fs/promises');const rename=fs.rename;
fs.rename=async(a,b)=>{if(b===process.argv[4])throw new Error('synthetic disk failure');return rename(a,b);};
const helper=require(process.argv[1]);
helper.seed(...process.argv.slice(2),false).then(()=>process.exitCode=2).catch(e=>{
 if(e.constructor.name==='PartialRotation')console.log('ROTATED_VERSION_PENDING');else process.exitCode=3;
});
'''
        result = subprocess.run(['node', '-e', script, str(SOURCE), str(self.source), str(self.live), str(self.version)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'ROTATED_VERSION_PENDING')
        self.assertEqual(self.live.read_bytes(), self.source.read_bytes())
        self.live.write_bytes(self.blob('refreshed-after-partial'))
        self.assertEqual(self.run_seed(), 'RECOVERED')
        self.assertEqual(self.live.read_bytes(), self.blob('refreshed-after-partial'))
        self.assertEqual(self.run_seed(), 'PRESERVED')

    def interrupted_migrate(self, legacy, fail_name):
        script = '''
const fs=require('node:fs/promises'),path=require('node:path'),rename=fs.rename;
fs.rename=async(a,b)=>{await rename(a,b);if(b===path.join(process.argv[3],process.argv[4]))throw Object.assign(new Error('interrupted rename'),{code:'EIO'});};
require(process.argv[1]).migrate(process.argv[2],process.argv[3]).catch(()=>process.exitCode=1);
'''
        result = subprocess.run(['node', '-e', script, str(SOURCE), str(legacy), str(self.storage), fail_name],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)

    def interrupted_rotation(self, target, after=False, rotate=False):
        script = '''
const fs=require('node:fs/promises'),rename=fs.rename;
fs.rename=async(a,b)=>{if(b===process.argv[5]){if(process.argv[6]==='after')await rename(a,b);throw Object.assign(new Error('interrupted rename'),{code:'EIO'});}return rename(a,b);};
require(process.argv[1]).seed(...process.argv.slice(2,5),process.argv[7]==='rotate').catch(()=>process.exitCode=1);
'''
        result = subprocess.run(['node', '-e', script, str(SOURCE), str(self.source), str(self.live), str(self.version),
                                 str(target), 'after' if after else 'before', 'rotate' if rotate else 'normal'],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        return json.loads((self.storage/'.rotation.json').read_text())

    def atomic_refresh(self, name):
        candidate = self.home/'sdk-refresh'
        candidate.write_bytes(self.blob(name))
        os.replace(candidate, self.live)

    def test_migration_replayed_login_name_is_one_login_at_startup(self):
        legacy = self.home/'legacy'
        legacy.mkdir()
        old = legacy/'.credentials.json'
        old.write_bytes(self.blob('preserved'))
        self.interrupted_migrate(legacy, '.credentials.json')
        os.link(self.live, old)
        selected = subprocess.run(['node', str(SOURCE), 'select-storage', str(legacy), str(self.storage)],
                                  env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(selected.returncode, 0, selected.stderr)
        self.assertIn(str(self.storage), (self.home/'.config/openclaw/claude-auth.env').read_text())
        self.assertTrue(old.exists(), 'Selection must retain the name that requires writer verification')
        self.assertIn('loginAlias', json.loads((self.storage/'.migration.json').read_text()))
        self.atomic_refresh('after-startup-selection')
        resumed = subprocess.run(['node', str(SOURCE), 'migrate', str(legacy), str(self.storage)],
                                 env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertFalse(old.exists())
        self.assertEqual(self.live.read_bytes(), self.blob('after-startup-selection'))

    def test_migration_replayed_tracking_name_resumes(self):
        legacy = self.home/'legacy'
        legacy.mkdir()
        (legacy/'.credentials.json').write_bytes(self.blob('preserved'))
        old_version = legacy/'.credentials-seed.sha256'
        old_version.write_text('a'*64+'\n')
        self.interrupted_migrate(legacy, '.credentials-seed.sha256')
        os.link(self.version, old_version)
        resumed = subprocess.run(['node', str(SOURCE), 'migrate', str(legacy), str(self.storage)],
                                 env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(resumed.stdout.strip(), 'MIGRATION_RESUMED')
        self.assertFalse(old_version.exists())
        self.assertEqual(self.version.read_text(), 'a'*64+'\n')

    def test_prepared_migration_never_accepts_an_unrelated_login(self):
        legacy = self.home/'legacy'
        legacy.mkdir()
        old = legacy/'.credentials.json'
        old.write_bytes(self.blob('original'))
        self.live.write_bytes(self.blob('unrelated'))
        (self.storage/'.migration.json').write_text(json.dumps({'legacy': str(legacy), 'directory': str(self.storage),
              'phase': 'moving', 'sourceHash': hashlib.sha256(old.read_bytes()).hexdigest()}))
        for command in ['migrate', 'select-storage']:
            result = subprocess.run(['node', str(SOURCE), command, str(legacy), str(self.storage)],
                                    env=self.env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 1)
        self.assertEqual(old.read_bytes(), self.blob('original'))
        self.assertEqual(self.live.read_bytes(), self.blob('unrelated'))

    def test_replayed_rotation_source_preserves_refresh_and_requires_explicit_adoption(self):
        self.source.write_bytes(self.blob('initial'))
        self.run_seed()
        previous = self.version.read_bytes()
        self.source.write_bytes(self.blob('replacement'))
        receipt = self.interrupted_rotation(self.live, after=True)
        os.link(self.live, receipt['replacement'])
        self.atomic_refresh('after-interruption')
        result = subprocess.run(['node', str(SOURCE), str(self.source), str(self.live), str(self.version)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertIn('ambiguous', result.stderr)
        self.assertEqual(self.live.read_bytes(), self.blob('after-interruption'))
        self.assertEqual(self.version.read_bytes(), previous)
        self.assertTrue(Path(receipt['replacement']).exists())
        adopted = subprocess.run(['node', str(SOURCE), str(self.source), str(self.live), str(self.version), '--adopt'],
                                 env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(adopted.returncode, 0, adopted.stderr)
        self.assertEqual(self.live.read_bytes(), self.blob('after-interruption'))
        self.assertFalse((self.storage/'.rotation.json').exists())

    def test_forced_rotation_tracker_equality_does_not_prove_commit(self):
        self.source.write_bytes(self.blob('initial'))
        self.run_seed()
        self.atomic_refresh('before-rotate')
        receipt = self.interrupted_rotation(self.live, rotate=True)
        self.assertEqual(receipt['phase'], 'prepared')
        self.assertEqual(self.version.read_text().strip(), receipt['seedHash'])
        self.atomic_refresh('after-prepared-crash')
        result = subprocess.run(['node', str(SOURCE), str(self.source), str(self.live), str(self.version)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.live.read_bytes(), self.blob('after-prepared-crash'))

    def test_committed_rotation_proof_preserves_refresh_with_a_replayed_source(self):
        self.source.write_bytes(self.blob('initial'))
        self.run_seed()
        self.source.write_bytes(self.blob('replacement'))
        receipt = self.interrupted_rotation(self.version)
        self.assertEqual(receipt['phase'], 'committed')
        os.link(self.live, receipt['replacement'])
        self.atomic_refresh('after-committed-crash')
        self.assertEqual(self.run_seed(), 'RECOVERED')
        self.assertEqual(self.live.read_bytes(), self.blob('after-committed-crash'))
        self.assertFalse(Path(receipt['replacement']).parent.exists())

    def test_prepared_rotation_with_unchanged_base_finishes_replacement(self):
        self.source.write_bytes(self.blob('initial'))
        self.run_seed()
        self.source.write_bytes(self.blob('replacement'))
        self.interrupted_rotation(self.live)
        self.assertEqual(self.run_seed(), 'RECOVERED')
        self.assertEqual(self.live.read_bytes(), self.source.read_bytes())

    def test_journalled_alias_survives_unlink_rollback_and_atomic_sdk_refresh(self):
        legacy = self.home/'legacy'
        legacy.mkdir()
        old = legacy/'.credentials.json'
        old.write_bytes(self.blob('original'))
        self.interrupted_migrate(legacy, '.credentials.json')
        os.link(self.live, old)
        witness = self.home/'original-inode'
        os.link(self.live, witness)
        preload = self.home/'alias-sync-eio.cjs'
        preload.write_text('''
const fs=require('node:fs/promises'),sync=require('node:fs'),path=require('node:path'),open=fs.open;
fs.open=async(p,...args)=>{const h=await open(p,...args),original=h.sync.bind(h);h.sync=async()=>{if(p===process.argv[3]&&!sync.existsSync(path.join(p,'.credentials.json')))throw Object.assign(new Error('unlink sync interrupted'),{code:'EIO'});return original();};return h;};
''')
        result = subprocess.run(['node', '--require', str(preload), str(SOURCE), 'migrate', str(legacy), str(self.storage)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertIn('loginAlias', json.loads((self.storage/'.migration.json').read_text()))
        self.assertFalse(old.exists())
        self.atomic_refresh('after-alias-unlink')
        os.link(witness, old)
        selected = subprocess.run(['node', str(SOURCE), 'select-storage', str(legacy), str(self.storage)],
                                  env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(selected.returncode, 0, selected.stderr)
        self.assertTrue(old.exists())
        self.assertEqual(self.live.read_bytes(), self.blob('after-alias-unlink'))
        resumed = subprocess.run(['node', str(SOURCE), 'migrate', str(legacy), str(self.storage)],
                                 env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)

    def test_recovery_persists_a_late_gateway_unit_before_service_action(self):
        legacy = self.home/'legacy'
        legacy.mkdir()
        self.live.write_bytes(self.blob('current'))
        unit = self.home/'.config/systemd/user/openclaw-gateway.service'
        unit.parent.mkdir(parents=True)
        unit.write_text('[Unit]\n')
        script = '''
const fs=require('node:fs/promises'),cp=require('node:child_process'),open=fs.open;let unitSynced=false,restarted=false;
fs.open=async(p,...args)=>{const h=await open(p,...args),sync=h.sync.bind(h);h.sync=async()=>{await sync();if(p.endsWith('/openclaw-gateway.service'))unitSynced=true;};return h;};
cp.spawnSync=(program,args)=>{if(program==='systemctl'&&(args.includes('start')||args.includes('restart'))){if(!unitSynced)throw new Error('volatile late unit');restarted=true;}return {status:0};};
require(process.argv[1]).recover(process.argv[2],process.argv[3]).then(()=>console.log(JSON.stringify({unitSynced,restarted}))).catch(()=>process.exitCode=1);
'''
        result = subprocess.run(['node', '-e', script, str(SOURCE), str(legacy), str(self.storage)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'unitSynced': True, 'restarted': True})

    def test_tracking_alias_proof_survives_unlink_rollback_and_new_tracking(self):
        legacy = self.home/'legacy'
        legacy.mkdir()
        (legacy/'.credentials.json').write_bytes(self.blob('original'))
        old = legacy/'.credentials-seed.sha256'
        old.write_text('a'*64+'\n')
        self.interrupted_migrate(legacy, '.credentials-seed.sha256')
        os.link(self.version, old)
        witness = self.home/'original-tracker-inode'
        os.link(self.version, witness)
        preload = self.home/'tracking-sync-eio.cjs'
        preload.write_text('''
const fs=require('node:fs/promises'),sync=require('node:fs'),path=require('node:path'),open=fs.open;
fs.open=async(p,...args)=>{const h=await open(p,...args),original=h.sync.bind(h);h.sync=async()=>{if(p===process.argv[3]&&!sync.existsSync(path.join(p,'.credentials-seed.sha256')))throw Object.assign(new Error('tracker unlink sync interrupted'),{code:'EIO'});return original();};return h;};
''')
        result = subprocess.run(['node', '--require', str(preload), str(SOURCE), 'migrate', str(legacy), str(self.storage)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertIn('trackingAlias', json.loads((self.storage/'.migration.json').read_text()))
        self.assertFalse(old.exists())
        current = self.home/'new-tracking'
        current.write_text('b'*64+'\n')
        os.replace(current, self.version)
        os.link(witness, old)
        resumed = subprocess.run(['node', str(SOURCE), 'migrate', str(legacy), str(self.storage)],
                                 env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertFalse(old.exists())
        self.assertEqual(self.version.read_text(), 'b'*64+'\n')

    def test_initial_move_journals_inode_before_source_sync_failure(self):
        legacy = self.home/'legacy'
        legacy.mkdir()
        old = legacy/'.credentials.json'
        old.write_bytes(self.blob('original'))
        witness = self.home/'initial-move-inode'
        os.link(old, witness)
        preload = self.home/'initial-move-sync-eio.cjs'
        preload.write_text('''
const fs=require('node:fs/promises'),sync=require('node:fs'),path=require('node:path'),open=fs.open;
fs.open=async(p,...args)=>{const h=await open(p,...args),original=h.sync.bind(h);h.sync=async()=>{if(p===process.argv[3]&&!sync.existsSync(path.join(p,'.credentials.json')))throw Object.assign(new Error('initial source sync interrupted'),{code:'EIO'});return original();};return h;};
''')
        result = subprocess.run(['node', '--require', str(preload), str(SOURCE), 'migrate', str(legacy), str(self.storage)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertIn('loginAlias', json.loads((self.storage/'.migration.json').read_text()))
        self.atomic_refresh('after-initial-move')
        os.link(witness, old)
        selected = subprocess.run(['node', str(SOURCE), 'select-storage', str(legacy), str(self.storage)],
                                  env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(selected.returncode, 0, selected.stderr)
        self.assertTrue(old.exists())
        self.assertEqual(self.live.read_bytes(), self.blob('after-initial-move'))
        resumed = subprocess.run(['node', str(SOURCE), 'migrate', str(legacy), str(self.storage)],
                                 env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertFalse(old.exists())

    def test_unique_shared_name_journals_source_identity_before_startup(self):
        legacy = self.home/'legacy'
        legacy.mkdir()
        old = legacy/'.credentials.json'
        old.write_bytes(self.blob('original'))
        witness = self.home/'source-before-journal'
        os.link(old, witness)
        self.interrupted_migrate(legacy, '.credentials.json')
        self.assertNotIn('loginAlias', json.loads((self.storage/'.migration.json').read_text()))
        selected = subprocess.run(['node', str(SOURCE), 'select-storage', str(legacy), str(self.storage)],
                                  env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(selected.returncode, 0, selected.stderr)
        self.assertIn('loginAlias', json.loads((self.storage/'.migration.json').read_text()))
        self.atomic_refresh('after-unique-selection')
        os.link(witness, old)
        selected = subprocess.run(['node', str(SOURCE), 'select-storage', str(legacy), str(self.storage)],
                                  env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(selected.returncode, 0, selected.stderr)
        self.assertEqual(self.live.read_bytes(), self.blob('after-unique-selection'))

    def test_seeding_cannot_erase_an_unfinished_migration(self):
        legacy = self.home/'legacy'
        legacy.mkdir()
        old = legacy/'.credentials.json'
        old.write_bytes(self.blob('original'))
        self.source.write_bytes(self.blob('configured'))
        self.interrupted_migrate(legacy, '.credentials.json')
        os.link(self.live, old)
        result = subprocess.run(['node', str(SOURCE), str(self.source), str(self.live), str(self.version)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertIn('migration is unfinished', result.stderr)
        self.assertTrue((self.storage/'.migration.json').exists())
        self.assertTrue(old.exists())
        resumed = subprocess.run(['node', str(SOURCE), 'migrate', str(legacy), str(self.storage)],
                                 env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(self.run_seed(), 'ADOPTED')
        self.assertEqual(self.live.read_bytes(), self.blob('original'))
        self.assertFalse((self.storage/'.migration.json').exists())

    def test_committed_migration_force_retry_preserves_refresh_after_rotation_cleanup(self):
        legacy = self.home/'legacy'
        legacy.mkdir()
        self.source.write_bytes(self.blob('configured'))
        self.run_seed()
        self.atomic_refresh('before-force')
        (self.storage/'.migration.json').write_text(json.dumps({'legacy': str(legacy), 'directory': str(self.storage), 'phase': 'moved'}))
        saved = self.home/'deleted-migration-receipt'
        script = '''
const fs=require('node:fs/promises'),unlink=fs.unlink;
fs.unlink=async p=>{if(p.endsWith('/.migration.json')){await fs.writeFile(process.argv[5],await fs.readFile(p));await unlink(p);throw Object.assign(new Error('receipt deletion interrupted'),{code:'EIO'});}return unlink(p);};
require(process.argv[1]).seed(...process.argv.slice(2,5),true).catch(()=>process.exitCode=1);
'''
        result = subprocess.run(['node', '-e', script, str(SOURCE), str(self.source), str(self.live), str(self.version), str(saved)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.storage/'.rotation.json').exists())
        receipt = json.loads(saved.read_text())
        self.assertTrue(receipt['committed'])
        self.assertEqual(receipt['operation'], 'replace')
        self.atomic_refresh('after-force-cleanup')
        (self.storage/'.migration.json').write_text(saved.read_text())
        self.assertEqual(self.run_seed(rotate=True), 'RECOVERED')
        self.assertEqual(self.live.read_bytes(), self.blob('after-force-cleanup'))
        self.assertFalse((self.storage/'.migration.json').exists())

    def test_force_request_survives_lock_release_error_and_later_refresh(self):
        self.source.write_bytes(self.blob('configured'))
        self.run_seed()
        self.atomic_refresh('before-force')
        script = '''
const path=require('node:path'),lock=require(path.join(process.env.HOME,'.npm-global/lib/node_modules/openclaw/node_modules/proper-lockfile')),acquire=lock.lock;
lock.lock=async(...args)=>{const release=await acquire(...args);return async()=>{await release();throw new Error('release EIO');};};
require(process.argv[1]).seed(...process.argv.slice(2),true).catch(()=>process.exitCode=1);
'''
        result = subprocess.run(['node', '-e', script, str(SOURCE), str(self.source), str(self.live), str(self.version)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.storage/'.rotation.json').exists())
        self.atomic_refresh('after-release-error')
        self.assertEqual(self.run_seed(rotate=True), 'PRESERVED')
        self.assertEqual(self.live.read_bytes(), self.blob('after-release-error'))
        self.assertEqual(self.run_seed(request='new-deliberate-reset'), 'ROTATED')
        self.assertEqual(self.live.read_bytes(), self.source.read_bytes())

    def test_all_completed_force_ids_remain_idempotent(self):
        self.source.write_bytes(self.blob('configured'))
        self.run_seed()
        self.atomic_refresh('before-r1')
        self.assertEqual(self.run_seed(request='R1'), 'ROTATED')
        self.atomic_refresh('before-r2')
        self.assertEqual(self.run_seed(request='R2'), 'ROTATED')
        self.atomic_refresh('after-r2')
        self.assertEqual(self.run_seed(request='R1'), 'PRESERVED')
        self.assertEqual(self.run_seed(request='R2'), 'PRESERVED')
        self.assertEqual(self.live.read_bytes(), self.blob('after-r2'))
        history = json.loads((self.home/'.config/openclaw/claude-auth-rotation').read_text())
        self.assertEqual(set(history['requests']), {'R1', 'R2'})

    def test_new_force_id_is_applied_after_recovering_a_prior_request(self):
        self.source.write_bytes(self.blob('configured'))
        self.run_seed()
        self.atomic_refresh('before-r1')
        script = '''
const fs=require('node:fs/promises'),rename=fs.rename;
fs.rename=async(a,b)=>{if(b===process.argv[4])throw Object.assign(new Error('tracking EIO'),{code:'EIO'});return rename(a,b);};
require(process.argv[1]).seed(...process.argv.slice(2),true,false,'R1').catch(()=>process.exitCode=1);
'''
        result = subprocess.run(['node', '-e', script, str(SOURCE), str(self.source), str(self.live), str(self.version)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.atomic_refresh('after-r1')
        self.assertEqual(self.run_seed(request='R2'), 'ROTATED')
        self.assertEqual(self.live.read_bytes(), self.source.read_bytes())
        history = json.loads((self.home/'.config/openclaw/claude-auth-rotation').read_text())
        self.assertEqual(set(history['requests']), {'R1', 'R2'})

    def test_adopting_a_pending_force_records_its_id_before_receipt_cleanup(self):
        legacy = self.home/'legacy'
        legacy.mkdir()
        self.source.write_bytes(self.blob('configured'))
        self.run_seed()
        self.atomic_refresh('before-force')
        (self.storage/'.migration.json').write_text(json.dumps({'legacy': str(legacy), 'directory': str(self.storage), 'phase': 'moved'}))
        script = '''
const fs=require('node:fs/promises'),rename=fs.rename;
fs.rename=async(a,b)=>{if(b.endsWith('/.rotation.json'))throw Object.assign(new Error('journal EIO'),{code:'EIO'});return rename(a,b);};
require(process.argv[1]).seed(...process.argv.slice(2),true,false,'R1').catch(()=>process.exitCode=1);
'''
        result = subprocess.run(['node', '-e', script, str(SOURCE), str(self.source), str(self.live), str(self.version)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads((self.storage/'.migration.json').read_text())['requestId'], 'R1')
        self.assertFalse((self.storage/'.rotation.json').exists())
        self.assertEqual(self.run_seed(adopt=True), 'ADOPTED')
        self.assertEqual(self.live.read_bytes(), self.blob('before-force'))
        history = self.home/'.config/openclaw/claude-auth-rotation'
        self.assertIn('R1', json.loads(history.read_text())['requests'])
        self.assertFalse((self.storage/'.migration.json').exists())
        self.atomic_refresh('after-adoption')
        self.assertEqual(self.run_seed(request='R1'), 'PRESERVED')
        self.assertEqual(self.live.read_bytes(), self.blob('after-adoption'))

    def test_adopted_request_history_failure_preserves_receipt_and_refresh(self):
        legacy = self.home/'legacy'
        legacy.mkdir()
        self.source.write_bytes(self.blob('configured'))
        self.run_seed()
        self.atomic_refresh('adopted-live')
        (self.storage/'.migration.json').write_text(json.dumps({'legacy': str(legacy), 'directory': str(self.storage),
            'phase': 'seeding', 'seedHash': self.version.read_text().strip(), 'operation': 'replace', 'requestId': 'R1', 'committed': False}))
        script = '''
const fs=require('node:fs/promises'),rename=fs.rename;
fs.rename=async(a,b)=>{await rename(a,b);if(b.endsWith('/claude-auth-rotation'))throw Object.assign(new Error('history rename EIO'),{code:'EIO'});};
require(process.argv[1]).seed(...process.argv.slice(2),false,true).catch(()=>process.exitCode=1);
'''
        result = subprocess.run(['node', '-e', script, str(SOURCE), str(self.source), str(self.live), str(self.version)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertTrue((self.storage/'.migration.json').exists())
        self.assertIn('R1', json.loads((self.home/'.config/openclaw/claude-auth-rotation').read_text())['requests'])
        self.atomic_refresh('after-adoption-error')
        self.assertEqual(self.run_seed(request='R1'), 'ADOPTED')
        self.assertEqual(self.live.read_bytes(), self.blob('after-adoption-error'))
        self.assertFalse((self.storage/'.migration.json').exists())

    def test_committed_adoption_receipt_resolves_force_without_reapplying(self):
        legacy = self.home/'legacy'
        legacy.mkdir()
        self.source.write_bytes(self.blob('configured'))
        self.run_seed()
        self.atomic_refresh('after-adoption')
        (self.storage/'.migration.json').write_text(json.dumps({'legacy': str(legacy), 'directory': str(self.storage),
            'phase': 'seeding', 'seedHash': self.version.read_text().strip(), 'operation': 'adopt', 'requestId': 'R1', 'committed': True}))
        self.assertEqual(self.run_seed(request='R1'), 'RECOVERED')
        self.assertEqual(self.live.read_bytes(), self.blob('after-adoption'))
        self.assertIn('R1', json.loads((self.home/'.config/openclaw/claude-auth-rotation').read_text())['requests'])
        self.assertFalse((self.storage/'.migration.json').exists())
        self.source.write_bytes(self.blob('new-configured-source'))
        self.assertEqual(self.run_seed(), 'ROTATED')
        self.assertEqual(self.live.read_bytes(), self.source.read_bytes())

    def test_preparation_retains_alias_and_migration_verifies_live_writers(self):
        unit = self.home/'.config/systemd/user/openclaw-gateway.service'
        unit.parent.mkdir(parents=True)
        unit.touch()
        legacy = self.home/'legacy'
        legacy.mkdir()
        old = legacy/'.credentials.json'
        old.write_bytes(self.blob('preserved'))
        self.interrupted_migrate(legacy, '.credentials.json')
        os.link(self.live, old)
        prepared = subprocess.run(['node', str(SOURCE), 'prepare-storage', str(legacy), str(self.storage)],
                                  env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.assertTrue(old.exists())
        self.atomic_refresh('after-prepare')
        script = '''
const cp=require('node:child_process');let stopped=0,verified=0;
cp.execFile=(program,args,options,callback)=>{if(program==='systemctl'&&args.includes('stop')){stopped++;callback(null,'','');return {};}if(program==='ps'){verified++;callback(null,'Sl\\n','');return {};}throw new Error('unexpected call');};
require(process.argv[1]).migrate(process.argv[2],process.argv[3],'--quiesce-gateway').then(()=>process.exitCode=2).catch(()=>{console.log(JSON.stringify({stopped,verified}));process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', script, str(SOURCE), str(legacy), str(self.storage)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'stopped': 1, 'verified': 1})
        self.assertTrue(old.exists())
        self.assertEqual(old.read_bytes(), self.blob('preserved'))
        self.assertEqual(self.live.read_bytes(), self.blob('after-prepare'))

    def test_selector_and_recovery_never_publish_shared_storage_with_legacy_writers(self):
        legacy = self.home/'legacy'
        legacy.mkdir()
        old = legacy/'.credentials.json'
        old.write_bytes(self.blob('preserved'))
        self.interrupted_migrate(legacy, '.credentials.json')
        os.link(self.live, old)
        control = self.home/'.config/openclaw'
        control.mkdir(parents=True)
        environment = control/'claude-auth.env'
        original = 'CLAUDE_SECURESTORAGE_CONFIG_DIR=' + str(legacy) + '\n'
        environment.write_text(original)
        (self.home/'bin/ps').write_text('#!/bin/sh\nprintf "Sl\\n"\n')
        for action in ['prepare-storage', 'select-storage']:
            result = subprocess.run(['node', str(SOURCE), action, str(legacy), str(self.storage)],
                                    env=self.env, capture_output=True, text=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(environment.read_text(), original)
            self.assertTrue(old.exists())
        unit = self.home/'.config/systemd/user/openclaw-gateway.service'
        unit.parent.mkdir(parents=True)
        unit.touch()
        systemctl = self.home/'bin/systemctl'
        systemctl.write_text('#!/bin/sh\nexit 0\n')
        systemctl.chmod(0o700)
        result = subprocess.run(['node', str(SOURCE), 'recover', str(legacy), str(self.storage)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(environment.read_text(), original)
        self.assertTrue(old.exists())
        self.assertEqual(self.live.read_bytes(), self.blob('preserved'))

    def test_ps_diagnostics_cannot_masquerade_as_zombie_or_dead_states(self):
        legacy = self.home/'legacy'
        legacy.mkdir()
        old = legacy/'.credentials.json'
        old.write_bytes(self.blob('preserved'))
        probe = self.home/'bin/ps'
        for value in ['Z diagnostic: process table unavailable', 'X error: process table unavailable', 'Zunexpected']:
            with self.subTest(value=value):
                probe.write_text('#!/bin/sh\nprintf "%s\\n" "' + value + '"\n')
                result = subprocess.run(['node', str(SOURCE), 'migrate', str(legacy), str(self.storage)],
                                        env=self.env, capture_output=True, text=True, timeout=15)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(old.read_bytes(), self.blob('preserved'))
                self.assertFalse(self.live.exists())
                self.assertFalse((self.storage/'.migration.json').exists())
        probe.write_text('#!/bin/sh\nprintf "Zs+\\nXl\\n"\n')
        result = subprocess.run(['node', str(SOURCE), 'migrate', str(legacy), str(self.storage)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'MIGRATED')
        self.assertFalse(old.exists())
        self.assertEqual(self.live.read_bytes(), self.blob('preserved'))

    def test_unflagged_cli_and_library_migration_verify_writers_without_stopping(self):
        legacy = self.home/'legacy'
        legacy.mkdir()
        old = legacy/'.credentials.json'
        old.write_bytes(self.blob('preserved'))
        calls = self.home/'probe-calls'
        (self.home/'bin/ps').write_text('#!/bin/sh\nprintf "probe\\n" >> "' + str(calls) + '"\nprintf "Sl\\n"\n')
        stopper = self.home/'bin/systemctl'
        stopper.write_text('#!/bin/sh\nprintf "unexpected-stop\\n" >> "' + str(calls) + '"\nexit 2\n')
        stopper.chmod(0o700)
        script = "require(process.argv[1]).migrate(process.argv[2],process.argv[3]).then(()=>process.exitCode=2).catch(()=>process.exitCode=1);"
        for command in [['node', str(SOURCE), 'migrate', str(legacy), str(self.storage)],
                        ['node', '-e', script, str(SOURCE), str(legacy), str(self.storage)]]:
            result = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(old.read_bytes(), self.blob('preserved'))
            self.assertFalse(self.live.exists())
            self.assertFalse((self.storage/'.migration.json').exists())
        self.assertEqual(calls.read_text().splitlines(), ['probe', 'probe'])

    def test_tracker_only_migration_requires_shutdown_and_writer_verification(self):
        unit = self.home/'.config/systemd/user/openclaw-gateway.service'
        unit.parent.mkdir(parents=True)
        unit.touch()
        self.live.write_bytes(self.blob('preserved-shared'))
        for phase in ['moving', None, 'seeding']:
            with self.subTest(phase=phase):
                legacy = self.home/('legacy-' + (phase or 'unjournalled'))
                legacy.mkdir()
                tracking = legacy/'.credentials-seed.sha256'
                tracking.write_text('a'*64+'\n')
                marker = self.storage/'.migration.json'
                if phase:
                    marker.write_text(json.dumps({'legacy': str(legacy), 'directory': str(self.storage), 'phase': phase,
                        **({'seedHash': 'a'*64, 'operation': 'adopt', 'committed': True} if phase == 'seeding' else {})}))
                    if phase == 'seeding':
                        self.version.write_text('a'*64+'\n')
                else:
                    marker.unlink()
                script = '''
const cp=require('node:child_process');let stopped=0,verified=0;
cp.execFile=(program,args,options,callback)=>{if(program==='systemctl'&&args.includes('stop')){stopped++;callback(null,'','');return {};}if(program==='ps'){verified++;callback(null,'Sl\\n','');return {};}throw new Error('unexpected call');};
require(process.argv[1]).migrate(process.argv[2],process.argv[3],'--quiesce-gateway').then(()=>process.exitCode=2).catch(()=>{console.log(JSON.stringify({stopped,verified}));process.exitCode=1;});
'''
                result = subprocess.run(['node', '-e', script, str(SOURCE), str(legacy), str(self.storage)],
                                        env=self.env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(json.loads(result.stdout), {'stopped': 1, 'verified': 1})
                self.assertTrue(tracking.exists())
                self.assertEqual(self.live.read_bytes(), self.blob('preserved-shared'))

    def test_tracker_only_shutdown_cannot_hide_a_recreated_legacy_login(self):
        unit = self.home/'.config/systemd/user/openclaw-gateway.service'
        unit.parent.mkdir(parents=True)
        unit.touch()
        legacy = self.home/'legacy'
        legacy.mkdir()
        tracking = legacy/'.credentials-seed.sha256'
        tracking.write_text('a'*64+'\n')
        self.live.write_bytes(self.blob('preserved-shared'))
        script = '''
const fs=require('node:fs'),cp=require('node:child_process'),path=require('node:path');
cp.execFile=(program,args,options,callback)=>{if(program==='systemctl'&&args.includes('stop')){fs.writeFileSync(path.join(process.argv[2],'.credentials.json'),process.argv[4]);callback(null,'','');return {};}if(program==='ps'){const e=new Error('no writers');e.code=1;callback(e,'','');return {};}throw new Error('unexpected call');};
require(process.argv[1]).migrate(process.argv[2],process.argv[3],'--quiesce-gateway').catch(()=>process.exitCode=1);
'''
        result = subprocess.run(['node', '-e', script, str(SOURCE), str(legacy), str(self.storage), self.blob('shutdown-legacy').decode()],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue(tracking.exists())
        self.assertEqual((legacy/'.credentials.json').read_bytes(), self.blob('shutdown-legacy'))
        self.assertEqual(self.live.read_bytes(), self.blob('preserved-shared'))
        self.assertFalse((self.storage/'.migration.json').exists())

    def test_shutdown_created_shared_login_is_never_overwritten(self):
        unit = self.home/'.config/systemd/user/openclaw-gateway.service'
        unit.parent.mkdir(parents=True)
        unit.touch()
        legacy = self.home/'legacy'
        legacy.mkdir()
        old = legacy/'.credentials.json'
        old.write_bytes(self.blob('original'))
        script = '''
const fs=require('node:fs'),cp=require('node:child_process'),path=require('node:path');
cp.execFile=(program,args,options,callback)=>{if(program==='systemctl'&&args.includes('stop')){fs.writeFileSync(path.join(process.argv[3],'.credentials.json'),process.argv[4]);callback(null,'','');return {};}if(program==='ps'){const e=new Error('no writers');e.code=1;callback(e,'','');return {};}throw new Error('unexpected service call');};
require(process.argv[1]).migrate(process.argv[2],process.argv[3],'--quiesce-gateway').catch(()=>process.exitCode=1);
'''
        result = subprocess.run(['node', '-e', script, str(SOURCE), str(legacy), str(self.storage), self.blob('shutdown-shared').decode()],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(old.read_bytes(), self.blob('original'))
        self.assertEqual(self.live.read_bytes(), self.blob('shutdown-shared'))
        self.assertFalse((self.storage/'.migration.json').exists())

    def test_missing_rotation_source_cannot_acknowledge_unapplied_replacement(self):
        self.source.write_bytes(self.blob('initial'))
        self.run_seed()
        previous = self.version.read_bytes()
        self.source.write_bytes(self.blob('replacement'))
        receipt = self.interrupted_rotation(self.live)
        Path(receipt['replacement']).unlink()
        result = subprocess.run(['node', str(SOURCE), str(self.source), str(self.live), str(self.version)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertIn('ambiguous', result.stderr)
        self.assertEqual(self.live.read_bytes(), self.blob('initial'))
        self.assertEqual(self.version.read_bytes(), previous)
        self.assertTrue((self.storage/'.rotation.json').exists())

    def test_pending_migration_seeding_retains_forced_rotation_intent(self):
        legacy = self.home/'legacy'
        legacy.mkdir()
        self.source.write_bytes(self.blob('configured'))
        self.run_seed()
        self.atomic_refresh('preserved-before-force')
        (self.storage/'.migration.json').write_text(json.dumps({'legacy': str(legacy), 'directory': str(self.storage), 'phase': 'moved'}))
        script = '''
const fs=require('node:fs/promises'),rename=fs.rename;
fs.rename=async(a,b)=>{await rename(a,b);if(b.endsWith('/.migration.json'))throw Object.assign(new Error('seeding interrupted'),{code:'EIO'});};
require(process.argv[1]).seed(...process.argv.slice(2),true).catch(()=>process.exitCode=1);
'''
        result = subprocess.run(['node', '-e', script, str(SOURCE), str(self.source), str(self.live), str(self.version)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        receipt = json.loads((self.storage/'.migration.json').read_text())
        self.assertEqual(receipt['operation'], 'replace')
        self.assertFalse(receipt['committed'])
        self.assertEqual(self.run_seed(), 'ROTATED')
        self.assertEqual(self.live.read_bytes(), self.source.read_bytes())
        self.assertFalse((self.storage/'.migration.json').exists())

    def test_completed_pending_force_is_not_repeated_after_recovery(self):
        self.source.write_bytes(self.blob('initial'))
        self.run_seed()
        self.atomic_refresh('before-force')
        receipt = self.interrupted_rotation(self.version, rotate=True)
        self.assertEqual(receipt['phase'], 'committed')
        self.atomic_refresh('after-force')
        self.assertEqual(self.run_seed(rotate=True), 'RECOVERED')
        self.assertEqual(self.live.read_bytes(), self.blob('after-force'))

    def test_prepared_migration_refuses_unrelated_tracking_inodes(self):
        legacy = self.home/'legacy'
        legacy.mkdir()
        (legacy/'.credentials.json').write_bytes(self.blob('original'))
        old_version = legacy/'.credentials-seed.sha256'
        old_version.write_text('a'*64+'\n')
        self.version.write_text('b'*64+'\n')
        (self.storage/'.migration.json').write_text(json.dumps({'legacy': str(legacy), 'directory': str(self.storage),
                                                               'phase': 'moving', 'versionHash': 'a'*64}))
        result = subprocess.run(['node', str(SOURCE), 'migrate', str(legacy), str(self.storage)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertTrue((legacy/'.credentials.json').exists())
        self.assertFalse(self.live.exists())
        self.assertEqual(old_version.read_text(), 'a'*64+'\n')
        self.assertEqual(self.version.read_text(), 'b'*64+'\n')

    def test_validation_persists_empty_auth_tree_without_changing_the_pointer(self):
        legacy = self.home/'legacy'
        legacy.mkdir()
        (legacy/'.credentials.json').write_bytes(self.blob('legacy'))
        events = self.home/'validation-sync-events'
        preload = self.home/'validation-preload.cjs'
        preload.write_text('''
const fs=require('node:fs/promises'),sync=require('node:fs'),open=fs.open,events=[];
fs.open=async(p,...args)=>{const h=await open(p,...args),original=h.sync.bind(h);h.sync=async()=>{await original();events.push(p);};return h;};
process.on('exit',()=>sync.writeFileSync(process.env.FIXTURE_SYNC_EVENTS,JSON.stringify(events)));
''')
        result = subprocess.run(['node', '--require', str(preload), str(SOURCE), 'validate-storage', str(legacy), str(self.storage)],
                                env={**self.env, 'FIXTURE_SYNC_EVENTS': str(events)}, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'ENV_VALID')
        synced = json.loads(events.read_text())
        for directory in [legacy, self.storage, self.storage.parent, self.home]:
            self.assertIn(str(directory), synced)
        self.assertFalse((self.home/'.config/openclaw/claude-auth.env').exists())
        self.assertFalse(self.live.exists())

    def test_migration_persists_the_last_shutdown_write(self):
        unit = self.home/'.config/systemd/user/openclaw-gateway.service'
        unit.parent.mkdir(parents=True)
        unit.touch()
        legacy = self.home/'legacy'
        legacy.mkdir()
        old = legacy/'.credentials.json'
        old.write_bytes(self.blob('before-stop'))
        script = '''
const fs=require('node:fs/promises'),sync=require('node:fs'),cp=require('node:child_process'),path=require('node:path');
const old=path.join(process.argv[2],'.credentials.json'),open=fs.open,rename=fs.rename;let stopped=false,lateSynced=false,unitSynced=false;
cp.execFile=(program,args,options,callback)=>{if(program==='systemctl'&&args.includes('stop')){if(!unitSynced)throw new Error('volatile gateway unit');sync.writeFileSync(old,process.argv[4]);stopped=true;callback(null,'','');return {};}if(program==='ps'){const e=new Error('no writers');e.code=1;callback(e,'','');return {};}throw new Error('unexpected service call');};
fs.open=async(p,...args)=>{const h=await open(p,...args),sync=h.sync.bind(h);h.sync=async()=>{await sync();if(p===old&&stopped)lateSynced=true;if(p.endsWith('/openclaw-gateway.service'))unitSynced=true;};return h;};
fs.rename=async(a,b)=>{if(a===old&&!lateSynced)throw new Error('last shutdown write was not persisted');return rename(a,b);};
require(process.argv[1]).migrate(process.argv[2],process.argv[3],'--quiesce-gateway').then(status=>console.log(JSON.stringify({status,lateSynced,unitSynced}))).catch(()=>process.exitCode=1);
'''
        result = subprocess.run(['node', '-e', script, str(SOURCE), str(legacy), str(self.storage), self.blob('shutdown-refresh').decode()],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'status': 'MIGRATED', 'lateSynced': True, 'unitSynced': True})
        self.assertEqual(self.live.read_bytes(), self.blob('shutdown-refresh'))

    def test_adoption_syncs_live_bytes_before_tracking(self):
        self.source.write_bytes(self.blob('configured'))
        self.live.write_bytes(self.blob('preserved-refresh'))
        script = '''
const fs=require('node:fs/promises'),open=fs.open,rename=fs.rename;let synced=false;
fs.open=async(p,...args)=>{const h=await open(p,...args),sync=h.sync.bind(h);h.sync=async()=>{await sync();if(p===process.argv[3])synced=true;};return h;};
fs.rename=async(a,b)=>{if(b===process.argv[4]&&!synced)throw new Error('tracking before live data durability');return rename(a,b);};
require(process.argv[1]).seed(...process.argv.slice(2,5),false).then(status=>console.log(JSON.stringify({status,synced}))).catch(()=>process.exitCode=1);
'''
        result = subprocess.run(['node', '-e', script, str(SOURCE), str(self.source), str(self.live), str(self.version)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'status': 'ADOPTED', 'synced': True})
        self.assertEqual(self.live.read_bytes(), self.blob('preserved-refresh'))

    def test_recovery_assets_and_creation_parents_are_persisted(self):
        asset = self.home/'.config/systemd/user/startup.service'
        asset.parent.mkdir(parents=True)
        asset.write_text('[Service]\n')
        events = self.home/'sync-events'
        preload = self.home/'sync-preload.cjs'
        preload.write_text('''
const fs=require('node:fs/promises'),sync=require('node:fs'),open=fs.open,events=[];
fs.open=async(p,...args)=>{const h=await open(p,...args),original=h.sync.bind(h);h.sync=async()=>{await original();events.push(p);};return h;};
process.on('exit',()=>sync.writeFileSync(process.env.FIXTURE_SYNC_EVENTS,JSON.stringify(events)));
''')
        result = subprocess.run(['node', '--require', str(preload), str(SOURCE), 'persist-assets', str(asset)],
                                env={**self.env, 'FIXTURE_SYNC_EVENTS': str(events)}, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'ASSETS_PERSISTED')
        synced = json.loads(events.read_text())
        self.assertEqual(synced, [str(asset), str(asset.parent), str(asset.parent.parent), str(self.home/'.config'), str(self.home)])

    def test_rotation_sync_order_and_replayed_completed_receipt_preserve_refresh(self):
        self.source.write_bytes(self.blob('new'))
        saved = self.home/'saved-receipt'
        script = '''
const fs=require('node:fs/promises'),path=require('node:path');
const open=fs.open,rename=fs.rename,unlink=fs.unlink,rmdir=fs.rmdir;
let stage,renamed=false,sourceSynced=false,destinationSynced=false,deleted=false,deletionSynced=false;
fs.open=async(p,...args)=>{const h=await open(p,...args),sync=h.sync.bind(h);h.sync=async()=>{await sync();if(renamed&&p===stage)sourceSynced=true;if(renamed&&p===path.dirname(process.argv[3]))destinationSynced=true;if(deleted&&p===path.dirname(process.argv[3]))deletionSynced=true;};return h;};
fs.rename=async(a,b)=>{if(b===process.argv[4]&&(!sourceSynced||!destinationSynced))throw new Error('tracking preceded source/destination durability');await rename(a,b);if(b===process.argv[3]){stage=path.dirname(a);renamed=true;}};
fs.unlink=async p=>{if(p.endsWith('/.rotation.json')){await fs.writeFile(process.argv[5],await fs.readFile(p));await unlink(p);deleted=true;}else await unlink(p);};
fs.rmdir=async p=>{if(p===stage&&!deletionSynced)throw new Error('stage removed before receipt deletion durability');return rmdir(p);};
require(process.argv[1]).seed(...process.argv.slice(2,5),false).then(()=>console.log(JSON.stringify({sourceSynced,destinationSynced,deletionSynced}))).catch(()=>process.exitCode=1);
'''
        result = subprocess.run(['node', '-e', script, str(SOURCE), str(self.source), str(self.live), str(self.version), str(saved)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'sourceSynced': True, 'destinationSynced': True, 'deletionSynced': True})
        receipt = json.loads(saved.read_text())
        self.assertFalse(Path(receipt['replacement']).parent.exists())
        self.live.write_bytes(self.blob('later-sdk-refresh'))
        (self.storage/'.rotation.json').write_text(saved.read_text())
        self.assertEqual(self.run_seed(), 'RECOVERED')
        self.assertEqual(self.live.read_bytes(), self.blob('later-sdk-refresh'))
        self.assertEqual(self.run_seed(), 'PRESERVED')

    def test_migration_syncs_both_rename_parents_before_completion(self):
        legacy = self.home/'legacy'
        legacy.mkdir()
        (legacy/'.credentials.json').write_bytes(self.blob('preserved'))
        (legacy/'.credentials-seed.sha256').write_text('a'*64+'\n')
        script = '''
const fs=require('node:fs/promises'),path=require('node:path'),events=[];
const open=fs.open,rename=fs.rename;
fs.open=async(p,...args)=>{const h=await open(p,...args),sync=h.sync.bind(h);h.sync=async()=>{await sync();if([process.argv[2],process.argv[3],path.dirname(process.argv[3]),path.join(process.argv[2],'.credentials.json'),path.join(process.argv[2],'.credentials-seed.sha256')].includes(p))events.push('sync:'+p);};return h;};
fs.rename=async(a,b)=>{await rename(a,b);if(a.startsWith(process.argv[2]+'/'))events.push('rename:'+path.basename(a));};
require(process.argv[1]).migrate(process.argv[2],process.argv[3]).then(()=>console.log(JSON.stringify(events))).catch(()=>process.exitCode=1);
'''
        result = subprocess.run(['node', '-e', script, str(SOURCE), str(legacy), str(self.storage)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        events = json.loads(result.stdout)
        first_move = events.index('rename:.credentials.json')
        self.assertIn('sync:'+str(self.storage.parent), events[:first_move])
        self.assertIn('sync:'+str(legacy/'.credentials.json'), events[:first_move])
        tracking_move = events.index('rename:.credentials-seed.sha256')
        self.assertIn('sync:'+str(legacy/'.credentials-seed.sha256'), events[:tracking_move])
        for name in ['.credentials.json', '.credentials-seed.sha256']:
            index = events.index('rename:'+name)
            after = events[index+1:]
            self.assertLess(after.index('sync:'+str(self.storage)), after.index('sync:'+str(legacy)))
        self.assertEqual(self.live.read_bytes(), self.blob('preserved'))

    def test_recovery_unit_probe_error_cannot_skip_independent_start(self):
        legacy = self.home / 'legacy'
        legacy.mkdir()
        self.live.write_bytes(self.blob('preserved'))
        control = self.home/'.config/openclaw'
        control.mkdir(parents=True)
        (control/'claude-auth.env').write_text('CLAUDE_SECURESTORAGE_CONFIG_DIR=' + str(self.storage) + '\n')
        binary = self.home/'bin'
        binary.mkdir(exist_ok=True)
        systemctl = binary/'systemctl'
        systemctl.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$HOME/systemctl-calls"\nexit 0\n')
        systemctl.chmod(0o700)
        script = '''
const fs=require('node:fs/promises'),stat=fs.lstat;
fs.lstat=async p=>{if(p.endsWith('/openclaw-gateway.service'))throw Object.assign(new Error('synthetic probe EIO'),{code:'EIO'});return stat(p);};
require(process.argv[1]).recover(process.argv[2],process.argv[3]).then(()=>process.exitCode=2).catch(()=>console.log('failed-after-start'));
'''
        result = subprocess.run(['node', '-e', script, str(SOURCE), str(legacy), str(self.storage)],
                                env={**self.env, 'PATH': str(binary)+os.pathsep+os.environ['PATH']},
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'failed-after-start')
        calls = (self.home/'systemctl-calls').read_text()
        self.assertIn('daemon-reload', calls)
        self.assertIn('--user start openclaw-gateway', calls)
        self.assertEqual(self.live.read_bytes(), self.blob('preserved'))

    def test_migration_never_quiesces_after_known_lock_loss(self):
        legacy = self.home / 'legacy'
        legacy.mkdir()
        old = legacy / '.credentials.json'
        old.write_bytes(self.blob('preserved'))
        unit = self.home / '.config/systemd/user/openclaw-gateway.service'
        unit.parent.mkdir(parents=True)
        unit.write_text('[Unit]\n')
        binary = self.home / 'bin'
        binary.mkdir(exist_ok=True)
        systemctl = binary / 'systemctl'
        systemctl.write_text('#!/bin/sh\ntouch "$HOME/stopped-after-loss"\n')
        systemctl.chmod(0o700)
        script = '''
const fs=require('node:fs/promises');const lockfile=require(process.argv[4]+'/proper-lockfile'),lock=lockfile.lock;
const callbacks=[];lockfile.lock=async(...args)=>{callbacks.push(args[1].onCompromised);return lock(...args);};
const read=fs.readFile;fs.readFile=async(p,...args)=>{const data=await read(p,...args);if(p.endsWith('/.credentials.json'))for(const cb of callbacks)cb(new Error('synthetic ownership loss'));return data;};
require(process.argv[1]).migrate(process.argv[2],process.argv[3],'--quiesce-gateway').then(()=>process.exitCode=2).catch(()=>console.log('refused'));
'''
        result = subprocess.run(['node', '-e', script, str(SOURCE), str(legacy), str(self.storage), str(MODULES)],
                                env={**self.env, 'PATH': str(binary) + os.pathsep + os.environ['PATH']},
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'refused')
        self.assertFalse((self.home/'stopped-after-loss').exists())
        self.assertEqual(old.read_bytes(), self.blob('preserved'))
        self.assertFalse(self.live.exists())
        self.assertFalse((self.storage/'.migration.json').exists())

    def test_migration_metadata_failure_after_lock_loss_never_compensates(self):
        legacy = self.home / 'legacy'
        legacy.mkdir()
        old = legacy / '.credentials.json'
        old.write_bytes(self.blob('preserved'))
        (legacy / '.credentials-seed.sha256').write_text('a' * 64 + '\n')
        script = '''
const fs=require('node:fs/promises'),path=require('node:path');
const lockfile=require(process.argv[4]+'/proper-lockfile'),lock=lockfile.lock;
const callbacks=[];lockfile.lock=async(...args)=>{callbacks.push(args[1].onCompromised);return lock(...args);};
const rename=fs.rename;let lost=false,writesAfterLoss=0;
fs.rename=async(a,b)=>{
 if(a===path.join(process.argv[2],'.credentials-seed.sha256')){
  lost=true;for(const cb of callbacks)cb(new Error('synthetic ownership loss'));
  throw new Error('synthetic metadata EIO');
 }
 if(lost)writesAfterLoss++;
 return rename(a,b);
};
require(process.argv[1]).migrate(process.argv[2],process.argv[3]).then(()=>process.exitCode=2).catch(()=>console.log(JSON.stringify({lost,writesAfterLoss})));
'''
        result = subprocess.run(['node', '-e', script, str(SOURCE), str(legacy), str(self.storage), str(MODULES)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'lost': True, 'writesAfterLoss': 0})
        self.assertFalse(old.exists())
        self.assertEqual(self.live.read_bytes(), self.blob('preserved'))
        self.assertTrue((self.storage / '.migration.json').exists())
        retry = subprocess.run(['node', str(SOURCE), 'migrate', str(legacy), str(self.storage)],
                               env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(retry.stdout.strip(), 'MIGRATION_RESUMED')
        self.assertEqual(self.live.read_bytes(), self.blob('preserved'))

    def test_python_seed_fingerprint_survives_helper_upgrade(self):
        configured = {'claudeAiOauth': {'accessToken': 'synthetic-seed', 'refreshToken': 'synthetic-refresh'},
                      'label': 'Grüße𐀀', 'numbers': [1.0, 1e-7, 1e20]}
        self.source.write_text(json.dumps(configured, ensure_ascii=False))
        previous = hashlib.sha256(json.dumps(configured, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.version.write_text(previous + '\n')
        self.live.write_bytes(self.blob('refreshed'))
        self.assertEqual(self.run_seed(), 'PRESERVED')
        self.assertEqual(self.live.read_bytes(), self.blob('refreshed'))

    def test_cleanup_and_release_failures_preserve_partial_replacement_status(self):
        for boundary in ['cleanup', 'release']:
            with self.subTest(boundary=boundary):
                self.source.write_bytes(self.blob('new', refresh=False))
                self.live.write_bytes(self.blob('old', refresh=False))
                self.version.unlink(missing_ok=True)
                script = '''
const fs=require('node:fs/promises');
if(process.argv[5]==='cleanup'){
 const rename=fs.rename,remove=fs.rm;let committed=false;
 fs.rename=async(a,b)=>{await rename(a,b);if(b===process.argv[3])committed=true;};
 fs.rm=async(...args)=>{if(committed)throw new Error('synthetic cleanup EIO');return remove(...args);};
}
else {
 const lockfile=require(process.argv[6]+'/proper-lockfile'),lock=lockfile.lock;
 lockfile.lock=async(...args)=>{const release=await lock(...args);return async()=>{await release();throw new Error('synthetic release EIO');};};
}
require(process.argv[1]).seed(...process.argv.slice(2,5),false).then(()=>process.exitCode=2).catch(e=>{
 if(e.constructor.name==='PartialRotation')console.log('partial');else process.exitCode=3;
});
'''
                result = subprocess.run(['node', '-e', script, str(SOURCE), str(self.source), str(self.live), str(self.version),
                                         boundary, str(MODULES)], env=self.env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), 'partial')
                self.assertEqual(self.live.read_bytes(), self.source.read_bytes())
                self.assertFalse((self.storage / '.oauth_refresh.lock').exists())
                self.assertFalse(Path(str(self.storage) + '.lock').exists())
                self.assertIn(self.run_seed(), ['PRESERVED', 'RECOVERED'])
                self.assertFalse((self.storage / '.rotation.json').exists())

    def test_uncommitted_rotation_receipt_failure_cleans_its_owned_candidate(self):
        self.source.write_bytes(self.blob('old'))
        self.run_seed()
        self.source.write_bytes(self.blob('new'))
        script = '''
const fs=require('node:fs/promises'),rename=fs.rename;
fs.rename=async(a,b)=>{if(b.endsWith('/.rotation.json'))throw new Error('synthetic receipt EIO');return rename(a,b);};
require(process.argv[1]).seed(...process.argv.slice(2),false).then(()=>process.exitCode=2).catch(()=>console.log('failed'));
'''
        result = subprocess.run(['node', '-e', script, str(SOURCE), str(self.source), str(self.live), str(self.version)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'failed')
        self.assertEqual(self.live.read_bytes(), self.blob('old'))
        self.assertFalse((self.storage / '.rotation.json').exists())
        self.assertFalse(list(self.storage.glob('.claude-seed-*/value')))

    def test_post_rename_eio_still_requests_restart_and_preserves_later_refresh(self):
        self.source.write_bytes(self.blob('old'))
        self.run_seed()
        self.source.write_bytes(self.blob('new'))
        script = '''
const fs=require('node:fs/promises'),rename=fs.rename;
fs.rename=async(a,b)=>{await rename(a,b);if(b===process.argv[3])throw new Error('synthetic post-rename EIO');};
require(process.argv[1]).seed(...process.argv.slice(2),false).then(()=>process.exitCode=2).catch(e=>{
 if(e.constructor.name==='PartialRotation')console.log('partial');else process.exitCode=3;
});
'''
        result = subprocess.run(['node', '-e', script, str(SOURCE), str(self.source), str(self.live), str(self.version)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'partial')
        self.live.write_bytes(self.blob('new-refreshed'))
        ambiguous = subprocess.run(['node', str(SOURCE), str(self.source), str(self.live), str(self.version)],
                                   env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(ambiguous.returncode, 1)
        self.assertIn('ambiguous', ambiguous.stderr)
        self.assertEqual(self.run_seed(adopt=True), 'RECOVERED')
        self.assertEqual(self.live.read_bytes(), self.blob('new-refreshed'))

    def test_corrupted_staged_credentials_never_replace_the_current_login(self):
        self.source.write_bytes(self.blob('old'))
        self.run_seed()
        self.source.write_bytes(self.blob('new'))
        script = '''
const fs=require('node:fs/promises'),rename=fs.rename;
fs.rename=async(a,b)=>{await rename(a,b);if(b.endsWith('/.rotation.json'))process.exit(99);};
require(process.argv[1]).seed(...process.argv.slice(2),false).catch(()=>process.exitCode=3);
'''
        result = subprocess.run(['node', '-e', script, str(SOURCE), str(self.source), str(self.live), str(self.version)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 99)
        marker = json.loads((self.storage / '.rotation.json').read_text())
        Path(marker['replacement']).write_bytes(self.blob('corrupted'))
        result = subprocess.run(['node', str(SOURCE), str(self.source), str(self.live), str(self.version)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.live.read_bytes(), self.blob('old'))

    def test_concurrent_provisioners_serialize_the_same_source(self):
        self.source.write_bytes(self.blob('configured'))
        args = ['node', str(SOURCE), str(self.source), str(self.live), str(self.version)]
        processes = [subprocess.Popen(args, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        statuses = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stderr)
            statuses.append(stdout.strip())
        self.assertEqual(sorted(statuses), ['PRESERVED', 'SEEDED'])
        self.assertEqual(self.live.read_bytes(), self.source.read_bytes())

    def test_stale_sdk_locks_are_recovered(self):
        self.source.write_bytes(self.blob('configured'))
        for lock in [self.storage / '.oauth_refresh.lock', Path(str(self.storage) + '.lock')]:
            lock.mkdir()
            os.utime(lock, (time.time() - 80, time.time() - 80))
        self.assertEqual(self.run_seed(), 'SEEDED')
        self.assertFalse((self.storage / '.oauth_refresh.lock').exists())
        self.assertFalse(Path(str(self.storage) + '.lock').exists())

    def test_restart_capture_waits_for_the_writer_and_ack_preserves_a_new_generation(self):
        config = self.home / '.config'
        (config / 'systemd/user').mkdir(parents=True)
        (config / 'systemd/user/openclaw-gateway.service').write_text('[Service]\n')
        (config / 'openclaw').mkdir()
        self.source.write_bytes(self.blob('old'))
        self.run_seed()
        self.source.write_bytes(self.blob('new'))
        script = '''
const fs=require('node:fs/promises'),rename=fs.rename;
fs.rename=async(a,b)=>{if(b===process.argv[3]){console.log('paused');await new Promise(r=>setTimeout(r,3000));}return rename(a,b);};
require(process.argv[1]).seed(...process.argv.slice(2),false).then(console.log).catch(()=>process.exitCode=3);
'''
        writer = subprocess.Popen(['node', '-e', script, str(SOURCE), str(self.source), str(self.live), str(self.version)],
                                  env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(writer.stdout.readline().strip(), 'paused')
            started = time.monotonic()
            captured = subprocess.run(['node', str(SOURCE), 'restart-request', str(self.storage)],
                                      env=self.env, capture_output=True, text=True, timeout=15)
            self.assertEqual(captured.returncode, 0, captured.stderr)
            self.assertGreater(time.monotonic() - started, 2.5)
            self.assertEqual(self.live.read_bytes(), self.blob('new'))
            writer.communicate(timeout=5)
            self.assertEqual(writer.returncode, 0)
        finally:
            if writer.poll() is None:
                writer.terminate()
                writer.communicate(timeout=5)
        nonce = captured.stdout.strip()
        self.source.write_bytes(self.blob('newer'))
        self.assertEqual(self.run_seed(), 'ROTATED')
        acknowledged = subprocess.run(['node', str(SOURCE), 'ack-restart', nonce, str(self.storage)],
                                     env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(acknowledged.returncode, 0, acknowledged.stderr)
        self.assertEqual(acknowledged.stdout.strip(), 'PRESERVED')
        self.assertTrue((config / 'openclaw/claude-auth-restart').exists())
        self.assertNotEqual((config / 'openclaw/claude-auth-restart').read_text().strip(), nonce)

    def test_live_sdk_lock_timeout_never_changes_credentials(self):
        self.source.write_bytes(self.blob('configured'))
        self.live.write_bytes(self.blob('current-live'))
        script = '''
const lockfile=require(process.argv[1]+'/proper-lockfile');
(async()=>{
 const release=await lockfile.lock(process.argv[2],{realpath:false,lockfilePath:process.argv[2]+'/.oauth_refresh.lock',stale:60000,update:5000});
 console.log('locked');await new Promise(r=>setTimeout(r,130000));await release();
})();
'''
        holder = subprocess.Popen(['node', '-e', script, str(MODULES), str(self.storage)], env=self.env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), 'locked')
            result = subprocess.run(['node', str(SOURCE), str(self.source), str(self.live), str(self.version)],
                                    env=self.env, capture_output=True, text=True, timeout=105)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('refresh locks remained busy', result.stderr)
            self.assertEqual(self.live.read_bytes(), self.blob('current-live'))
            self.assertFalse(self.version.exists())
        finally:
            holder.terminate()
            holder.communicate(timeout=5)

    def test_rotation_crash_recovery_preserves_a_later_sdk_refresh(self):
        self.source.write_bytes(self.blob('old'))
        self.assertEqual(self.run_seed(), 'SEEDED')
        self.source.write_bytes(self.blob('new'))
        script = '''
const fs=require('node:fs/promises'),rename=fs.rename;
fs.rename=async(a,b)=>{await rename(a,b);if(b===process.argv[3])process.exit(99);};
require(process.argv[1]).seed(...process.argv.slice(2),false).catch(()=>process.exitCode=3);
'''
        result = subprocess.run(['node', '-e', script, str(SOURCE), str(self.source), str(self.live), str(self.version)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 99)
        self.assertTrue((self.storage / '.rotation.json').exists())
        refreshed = self.home / 'refreshed'
        refreshed.write_bytes(self.blob('new-refreshed'))
        refreshed.replace(self.live)
        ambiguous = subprocess.run(['node', str(SOURCE), str(self.source), str(self.live), str(self.version)],
                                   env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(ambiguous.returncode, 1)
        self.assertEqual(self.live.read_bytes(), self.blob('new-refreshed'))
        self.assertEqual(self.run_seed(adopt=True), 'RECOVERED')
        self.assertEqual(self.live.read_bytes(), self.blob('new-refreshed'))
        self.assertEqual(self.run_seed(), 'PRESERVED')

    def test_interrupted_migration_resumes_before_adopting_the_current_login(self):
        legacy = self.home / '.claude'
        legacy.mkdir()
        old = legacy / '.credentials.json'
        old.write_bytes(self.blob('current-live'))
        (legacy / '.credentials-seed.sha256').write_text('0' * 64 + '\n')
        self.source.write_bytes(self.blob('configured-older'))
        script = '''
const fs=require('node:fs/promises'),rename=fs.rename;
fs.rename=async(a,b)=>{await rename(a,b);if(b===process.argv[3]+'/.credentials.json')process.exit(99);};
require(process.argv[1]).migrate(...process.argv.slice(2)).catch(()=>process.exitCode=3);
'''
        result = subprocess.run(['node', '-e', script, str(SOURCE), str(legacy), str(self.storage)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 99)
        self.assertTrue((legacy / '.credentials-seed.sha256').exists())
        self.assertFalse(old.exists())
        result = subprocess.run(['node', str(SOURCE), 'migrate', str(legacy), str(self.storage)],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'MIGRATION_RESUMED')
        self.assertEqual(self.run_seed(), 'ADOPTED')
        self.assertEqual(self.live.read_bytes(), self.blob('current-live'))
        self.assertFalse((self.storage / '.migration.json').exists())
        self.assertEqual(self.run_seed(), 'PRESERVED')

    def test_migration_preserves_bytes_is_idempotent_and_refuses_two_logins(self):
        legacy = self.home / '.claude'
        legacy.mkdir()
        old = legacy / '.credentials.json'
        old.write_bytes(self.blob('live-refreshed'))
        def migrate():
            return subprocess.run(['node', str(SOURCE), 'migrate', str(legacy), str(self.storage)],
                                  env=self.env, capture_output=True, text=True, timeout=15)
        result = migrate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'MIGRATED')
        self.assertEqual(self.live.read_bytes(), self.blob('live-refreshed'))
        self.assertFalse(old.exists())
        self.assertEqual(migrate().stdout.strip(), 'MIGRATION_RESUMED')
        self.source.write_bytes(self.blob('configured'))
        self.assertEqual(self.run_seed(), 'ADOPTED')
        self.assertEqual(self.live.read_bytes(), self.blob('live-refreshed'))
        self.assertEqual(migrate().stdout.strip(), 'ALREADY_MIGRATED')
        old.write_bytes(self.blob('other'))
        result = migrate()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(old.read_bytes(), self.blob('other'))
        self.assertEqual(self.live.read_bytes(), self.blob('live-refreshed'))


if __name__ == '__main__':
    unittest.main()
