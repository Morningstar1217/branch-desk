"""Push integration uses disposable local bare remotes, never business remotes."""
from pathlib import Path
import io
import json
import os
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from server import GIT, GuardError, Repository
import server
import test_server


class PushChecks(unittest.TestCase):
    def setUp(self):
        self.f = test_server.MergeChecks('test_fast_forward_stays_uncommitted_and_preserves_refs')
        self.f.cmd = lambda *args: subprocess.check_output([GIT,'-C',str(self.f.path),*args],stderr=subprocess.STDOUT,env={**os.environ,'GIT_TRACE2_EVENT':'0'}).decode().strip()
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.repo,self.path = self.f.repo,self.f.target
        self.remote = self.f.local_origin()
        self.f.cmd('fetch','origin')
        self.f.cmd('branch','--set-upstream-to=origin/target','target')
        self.repo.merge(self.f.plan()['plan'])
        self.head = self.repo.commit_merge(self.repo.preview_commit(self.path)['plan'])['sha']

    def remote_refs(self):
        return subprocess.check_output([GIT,'--git-dir',str(self.remote),'show-ref'],text=True)

    def remote_advance(self):
        tree = self.f.cmd('rev-parse',self.f.base+'^{tree}')
        commit = self.f.cmd('commit-tree',tree,'-p',self.f.base,'-m','fix: remote advance')
        self.f.cmd('push','origin',commit+':refs/heads/target')
        return commit

    def test_reviewed_push_updates_only_same_branch_and_preserves_head_and_tags(self):
        subprocess.check_call([GIT,'--git-dir',str(self.remote),'update-ref','refs/heads/master',self.f.base])
        self.f.cmd('tag','-a','private-tag','-m','tag')
        self.f.cmd('config','push.followTags','true')
        before=self.remote_refs()
        plan=self.repo.preview_push(self.path)
        self.assertEqual(self.remote_refs(),before)
        self.assertEqual(plan['count'],2)  # The merge and its incoming commit.
        self.assertEqual(plan['ref'],'refs/heads/target')
        with patch('server.git',wraps=server.git) as commands, patch('server.subprocess.Popen',wraps=subprocess.Popen) as processes:
            result=self.repo.push(plan['plan'])
        command=next(call.args for call in commands.call_args_list if 'push' in call.args[1:])
        self.assertEqual(command[1],'push')
        process=next(call for call in processes.call_args_list if 'push' in call.args[0])
        self.assertEqual(process.kwargs['env']['GIT_TRACE2_EVENT'],'0')
        self.assertEqual(command[-2:],('origin','HEAD:refs/heads/target'))
        self.assertIn('--no-follow-tags',command)
        self.assertIn('--recurse-submodules=no',command)
        self.assertNotIn('--force',command)
        self.assertEqual(result['sha'],self.head)
        self.assertEqual(self.f.cmd('rev-parse','HEAD'),self.head)
        self.assertIn(self.head+' refs/heads/target',self.remote_refs())
        self.assertIn(self.f.base+' refs/heads/master',self.remote_refs())
        self.assertNotIn('refs/tags/',self.remote_refs())
        self.assertEqual(result['state']['upstream'],'refs/remotes/origin/target')
        with self.assertRaisesRegex(GuardError,'失效'):self.repo.push(plan['plan'])
        with self.assertRaisesRegex(GuardError,'无需推送'):self.repo.preview_push(self.path)

    def test_first_push_creates_same_name_and_sets_upstream(self):
        self.f.cmd('branch','--unset-upstream')
        subprocess.check_call([GIT,'--git-dir',str(self.remote),'update-ref','-d','refs/heads/target'])
        plan=self.repo.preview_push(self.path)
        self.assertTrue(plan['first'])
        result=self.repo.push(plan['plan'])
        self.assertEqual(result['state']['upstream'],'refs/remotes/origin/target')
        self.assertIn(self.head+' refs/heads/target',self.remote_refs())

    def test_remote_new_commits_block_preview_without_force(self):
        self.remote_advance();before=self.remote_refs()
        with self.assertRaisesRegex(GuardError,'尚未整合'):self.repo.preview_push(self.path)
        self.assertEqual(self.remote_refs(),before)

    def test_remote_change_after_preview_blocks_push(self):
        plan=self.repo.preview_push(self.path);self.remote_advance();before=self.remote_refs()
        with self.assertRaisesRegex(GuardError,'已变化'):self.repo.push(plan['plan'])
        self.assertEqual(self.remote_refs(),before)

    def test_local_head_and_config_changes_invalidate_preview(self):
        plan=self.repo.preview_push(self.path)
        self.f.cmd('config','user.name','changed')
        with self.assertRaisesRegex(GuardError,'已变化'):self.repo.push(plan['plan'])
        plan=self.repo.preview_push(self.path)
        (self.f.path/'local.txt').write_text('local')
        self.f.commit('fix: local change')
        with self.assertRaisesRegex(GuardError,'已变化'):self.repo.push(plan['plan'])

    def test_dirty_merge_master_detached_and_wrong_upstream_are_rejected(self):
        before=self.remote_refs()
        (self.f.path/'keep.txt').write_text('keep')
        with self.assertRaises(GuardError):self.repo.preview_push(self.path)
        (self.f.path/'keep.txt').unlink()
        self.f.cmd('branch','--set-upstream-to=origin/incoming','target')
        with self.assertRaisesRegex(GuardError,'同名'):self.repo.preview_push(self.path)
        self.f.cmd('checkout','-b','master')
        with self.assertRaisesRegex(GuardError,'master'):self.repo.preview_push(self.path)
        self.f.cmd('checkout','--detach')
        with self.assertRaisesRegex(GuardError,'分离'):self.repo.preview_push(self.path)
        self.assertEqual(self.remote_refs(),before)

    def test_mirror_unsafe_refspec_and_multiple_push_urls_are_rejected(self):
        for key,value in [('remote.origin.mirror','true'),('remote.origin.push','HEAD:refs/heads/master')]:
            self.f.cmd('config',key,value)
            with self.assertRaises(GuardError):self.repo.preview_push(self.path)
            self.f.cmd('config','--unset',key)
        self.f.cmd('config','--add','remote.origin.pushurl',str(self.remote))
        self.f.cmd('config','--add','remote.origin.pushurl',str(self.remote))
        with self.assertRaisesRegex(GuardError,'一个明确'):self.repo.preview_push(self.path)

    def test_pre_push_hooks_run_and_rejection_never_retries(self):
        hooks=Path(self.f.temp.name)/'push-hooks';hooks.mkdir()
        marker=hooks/'ran'
        script=hooks/'pre-push';script.write_text('#!/bin/sh\ntouch "'+str(marker)+'"\necho "fixture push rejected" >&2\nexit 1\n');script.chmod(0o755)
        self.f.cmd('config','core.hooksPath',str(hooks))
        plan=self.repo.preview_push(self.path);before=self.remote_refs()
        self.assertFalse(marker.exists())
        with self.assertRaisesRegex(GuardError,'fixture push rejected'):self.repo.push(plan['plan'])
        self.assertTrue(marker.exists());self.assertEqual(self.remote_refs(),before)
        with self.assertRaisesRegex(GuardError,'失效'):self.repo.push(plan['plan'])

    def test_expired_preview_and_disabled_hooks_are_rejected(self):
        plan=self.repo.preview_push(self.path);self.repo.push_plans[plan['plan']]['time']-=601
        with self.assertRaisesRegex(GuardError,'失效'):self.repo.push(plan['plan'])
        with patch.dict('os.environ',{'HUSKY':'0'}),self.assertRaisesRegex(GuardError,'hooks'):
            self.repo.preview_push(self.path)

    def test_invalid_new_commit_message_is_reported_without_rewriting_history(self):
        (self.f.path/'local.txt').write_text('local');self.f.commit('invalid subject')
        head=self.f.cmd('rev-parse','HEAD')
        with self.assertRaisesRegex(GuardError,'提交标题'):self.repo.preview_push(self.path)
        self.assertEqual(self.f.cmd('rev-parse','HEAD'),head)

    def test_post_push_verification_failure_does_not_repeat_push(self):
        plan=self.repo.preview_push(self.path)
        with patch.object(self.repo,'remote_tip',return_value=self.f.base),self.assertRaisesRegex(GuardError,'结果未确认'):
            self.repo.push(plan['plan'])
        self.assertIn(self.head+' refs/heads/target',self.remote_refs())
        with self.assertRaisesRegex(GuardError,'失效'):self.repo.push(plan['plan'])


class RequestChecks(unittest.TestCase):
    def test_push_routes_require_token(self):
        for route,data,method,args in [('push/preview',{'path':'/fixture'},'preview_push',('/fixture',)),('push',{'plan':'reviewed'},'push',('reviewed',))]:
            for token in ('wrong','test-token'):
                handler=test_server.RequestChecks().handler({'Host':'127.0.0.1:8123','X-Session-Token':token,'Content-Type':'application/json'})
                body=json.dumps(data).encode();handler.headers['Content-Length']=str(len(body));handler.path='/api/'+route;handler.rfile=io.BytesIO(body)
                operation=Mock(return_value={'fixture':True});handler.server.repo=SimpleNamespace(**{method:operation});handler.reply=Mock();handler.do_POST()
                if token=='wrong':operation.assert_not_called();self.assertEqual(handler.reply.call_args.args[0],400)
                else:operation.assert_called_once_with(*args);handler.reply.assert_called_once_with(200,{'fixture':True})

    def test_remote_url_credentials_are_not_shown(self):
        self.assertEqual(Repository.push_url_label('https://user:secret@example.invalid/repo.git?token=private'),'https://***@example.invalid/repo.git')


if __name__=='__main__':unittest.main()
