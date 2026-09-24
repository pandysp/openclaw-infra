#!/usr/bin/env python3
"""Run the actual sync script against disposable local Git repositories."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = Path(os.environ.get('WORKSPACE_SYNC_SCRIPT', ROOT/'ansible/roles/workspace/files/workspace-sync.sh'))
GIT = shutil.which('git')


class WorkspaceSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='workspace-sync-')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.env={'PATH':os.environ['PATH'],'HOME':str(self.root),'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null'}
        self.remote=self.root/'remote.git'
        self.local=self.root/'workspace'
        self.git(self.root,'init','--bare','-b','main',str(self.remote))
        self.local.mkdir()
        self.script=SCRIPT
        self.env.update({'WORKSPACE_PATH':str(self.local),'WORKSPACE_AGENT_ID':'test',
                         'WORKSPACE_REPOSITORY':str(self.remote),'WORKSPACE_INITIALIZE':'1',
                         'WORKSPACE_EXCLUDES':str(ROOT/'ansible/roles/workspace/files/gitignore-workspace')})

    def git(self,cwd,*args,check=True):
        return subprocess.run([GIT,'-C',str(cwd),*args],env=self.env,capture_output=True,text=True,check=check)

    def init(self,path):
        path.mkdir(exist_ok=True)
        self.git(path,'init','-b','main')
        self.git(path,'config','user.name','Fixture')
        self.git(path,'config','user.email','fixture@example.invalid')
        self.git(path,'remote','add','origin',str(self.remote))

    def seed(self):
        seed=self.root/'seed'
        self.init(seed)
        (seed/'note.md').write_text('remote original\n')
        self.git(seed,'add','.')
        self.git(seed,'commit','-m','seed')
        self.git(seed,'push','origin','main')
        return seed

    def restored(self):
        seed=self.seed()
        self.git(self.local,'clone',str(self.remote),'.')
        self.git(self.local,'config','user.name','Fixture')
        self.git(self.local,'config','user.email','fixture@example.invalid')
        return seed

    def sync(self):
        return subprocess.run(['bash',str(self.script)],env=self.env,capture_output=True,text=True,timeout=15)

    def fail_git_operation(self,operation):
        bin=self.root/'bin';bin.mkdir()
        wrapper=bin/'git'
        wrapper.write_text('#!/bin/sh\nif [ "$1" = '+operation+' ]; then echo "Injected Git failure" >&2; exit 70; fi\nexec '+GIT+' "$@"\n')
        wrapper.chmod(0o700)
        self.env['PATH']=str(bin)+':'+self.env['PATH']

    def test_empty_remote_accepts_first_push_and_repeat_with_bootstrap(self):
        result=self.sync()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.env['WORKSPACE_INITIALIZE']='0'
        (self.local/'BOOTSTRAP.md').write_text('new workspace\n')
        for _ in range(2):
            result=self.sync()
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.git(self.remote,'show','main:BOOTSTRAP.md').stdout,'new workspace\n')

    def test_nonempty_unborn_workspace_refuses_remote_restoration(self):
        self.seed(); self.init(self.local)
        (self.local/'note.md').write_text('valuable local note\n')
        result=self.sync()
        self.assertNotEqual(result.returncode,0)
        self.assertEqual((self.local/'note.md').read_text(),'valuable local note\n')
        self.assertNotEqual(self.git(self.local,'rev-parse','--verify','HEAD',check=False).returncode,0)

    def test_empty_workspace_clones_before_first_commit(self):
        self.seed()
        shutil.rmtree(self.local)
        self.local.mkdir()
        result=self.sync()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual((self.local/'note.md').read_text(),'remote original\n')
        self.assertEqual(self.git(self.local,'rev-list','--max-parents=0','HEAD').stdout,self.git(self.remote,'rev-list','--max-parents=0','main').stdout)

    def test_uninitialized_nonempty_directory_is_never_overwritten(self):
        self.seed()
        (self.local/'note.md').write_text('existing files\n')
        self.assertNotEqual(self.sync().returncode,0)
        self.assertEqual((self.local/'note.md').read_text(),'existing files\n')
        self.assertFalse((self.local/'.git').exists())

    def test_auth_or_transport_failure_is_not_empty_remote(self):
        self.init(self.local)
        self.env['WORKSPACE_REPOSITORY']=str(self.root/'missing.git')
        self.git(self.local,'remote','set-url','origin',self.env['WORKSPACE_REPOSITORY'])
        (self.local/'note.md').write_text('preserve me\n')
        self.assertNotEqual(self.sync().returncode,0)
        self.assertNotEqual(self.git(self.local,'rev-parse','--verify','HEAD',check=False).returncode,0)
        self.assertEqual((self.local/'note.md').read_text(),'preserve me\n')

    def test_conflict_preserves_both_histories_without_force_push(self):
        seed=self.restored()
        (self.local/'note.md').write_text('local edit\n')
        (seed/'note.md').write_text('remote edit\n')
        self.git(seed,'commit','-am','remote edit');self.git(seed,'push','origin','main')
        remote_head=self.git(self.remote,'rev-parse','main').stdout
        self.assertNotEqual(self.sync().returncode,0)
        self.assertEqual(self.git(self.remote,'rev-parse','main').stdout,remote_head)
        self.assertEqual((self.local/'note.md').read_text(),'local edit\n')
        self.assertEqual(self.git(self.local,'show','HEAD:note.md').stdout,'local edit\n')
        self.assertFalse((self.local/'.git/MERGE_HEAD').exists())

    def test_bootstrap_filename_does_not_trigger_restoration(self):
        self.seed(); self.assertEqual(self.sync().returncode,0)
        (self.local/'BOOTSTRAP.md').write_text('ordinary content\n')
        (self.local/'note.md').write_text('valuable edit\n')
        result=self.sync()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual((self.local/'note.md').read_text(),'valuable edit\n')
        self.assertEqual(self.git(self.remote,'show','main:note.md').stdout,'valuable edit\n')

    def test_remote_changes_merge_without_losing_local_notes(self):
        seed=self.seed(); self.assertEqual(self.sync().returncode,0)
        (self.local/'local.md').write_text('local addition\n')
        (seed/'remote.md').write_text('remote addition\n')
        self.git(seed,'add','.');self.git(seed,'commit','-m','remote addition');self.git(seed,'push','origin','main')
        result=self.sync()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        for file in ['local.md','remote.md']:
            self.assertTrue((self.local/file).exists())
            self.assertEqual(self.git(self.remote,'show','main:'+file).returncode,0)
        before=self.git(self.remote,'rev-parse','main').stdout
        self.assertEqual(self.sync().returncode,0)
        self.assertEqual(self.git(self.remote,'rev-parse','main').stdout,before)

    def test_backup_excludes_survive_remote_gitignore(self):
        seed=self.seed();(seed/'.gitignore').write_text('unrelated.tmp\n')
        self.git(seed,'add','.');self.git(seed,'commit','-m','ignore');self.git(seed,'push','origin','main')
        self.assertEqual(self.sync().returncode,0)
        (self.local/'.env').write_text('fixture-private-value\n')
        (self.local/'visible.md').write_text('public\n')
        self.assertEqual(self.sync().returncode,0)
        self.assertNotEqual(self.git(self.remote,'show','main:.env',check=False).returncode,0)
        self.assertEqual((self.local/'.gitignore').read_text(),'unrelated.tmp\n')

    def test_failed_clone_cannot_claim_success(self):
        self.seed(); self.fail_git_operation('clone')
        self.assertNotEqual(self.sync().returncode,0)

    def test_failed_enumeration_stops_before_commit_and_push(self):
        self.restored(); before=self.git(self.remote,'rev-parse','main').stdout
        (self.local/'note.md').write_text('new\n')
        self.fail_git_operation('ls-files')
        self.assertNotEqual(self.sync().returncode,0)
        self.assertEqual(self.git(self.remote,'rev-parse','main').stdout,before)

    def test_failed_fetch_does_not_commit_or_push(self):
        self.restored(); before=self.git(self.remote,'rev-parse','main').stdout
        (self.local/'note.md').write_text('new\n')
        self.fail_git_operation('fetch')
        self.assertNotEqual(self.sync().returncode,0)
        self.assertEqual(self.git(self.remote,'rev-parse','main').stdout,before)
        self.assertEqual(self.git(self.local,'rev-parse','HEAD').stdout,before)

    def test_failed_diff_does_not_commit_or_push(self):
        self.restored(); before=self.git(self.remote,'rev-parse','main').stdout
        (self.local/'note.md').write_text('new\n')
        self.fail_git_operation('diff')
        self.assertNotEqual(self.sync().returncode,0)
        self.assertEqual(self.git(self.remote,'rev-parse','main').stdout,before)

    def test_recurring_sync_cannot_restore_even_an_empty_workspace(self):
        self.seed(); self.env['WORKSPACE_INITIALIZE']='0'
        result=self.sync()
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(list(self.local.iterdir()),[])


if __name__=='__main__':unittest.main()
