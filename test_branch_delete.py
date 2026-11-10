"""Local branch deletion checks in disposable repositories, never business refs."""
import io
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from server import GIT, GuardError, git
import test_server


class BranchDeleteChecks(unittest.TestCase):
    cmd = test_server.MergeChecks.cmd
    commit = test_server.MergeChecks.commit
    local_origin = test_server.MergeChecks.local_origin
    setUp = test_server.MergeChecks.setUp

    def merged(self):
        self.cmd('merge', '--ff-only', 'incoming')

    def preview(self):
        return self.repo.preview_branch_delete('refs/heads/incoming')

    def test_delete_local_only_retains_remote_refs_files_index_and_other_branches(self):
        self.merged()
        remote = self.local_origin()
        self.cmd('update-ref', 'refs/remotes/origin/incoming', self.incoming)
        self.cmd('branch', '--set-upstream-to=origin/incoming', 'incoming')
        remote_before = subprocess.check_output([GIT, '--git-dir', str(remote), 'show-ref'])
        (self.path / 'common.txt').write_text('staged user work')
        self.cmd('add', 'common.txt')
        (self.path / 'local.txt').write_text('untracked user work')
        (self.path / 'local.secret').write_text('ignored user work')
        before = self.repo.snapshot(self.target)
        plan = self.preview()
        self.assertEqual(plan['upstream'], 'refs/remotes/origin/incoming')
        self.repo.delete_branch(plan['plan'], True)
        self.assertEqual(self.repo.snapshot(self.target), before)
        self.assertEqual((self.path / 'local.secret').read_text(), 'ignored user work')
        self.assertNotIn('refs/heads/incoming', self.cmd('show-ref'))
        self.assertEqual(self.cmd('rev-parse', 'refs/remotes/origin/incoming'), self.incoming)
        self.assertEqual(self.cmd('rev-parse', 'target'), self.incoming)
        self.assertEqual(subprocess.check_output([GIT, '--git-dir', str(remote), 'show-ref']), remote_before)
        self.assertEqual(next(b for b in self.repo.branches() if b['branch'] == 'incoming')['remote'], 'origin')
        with self.assertRaisesRegex(GuardError, '失效'):
            self.repo.delete_branch(plan['plan'], True)

    def test_unmerged_master_checked_out_and_nonlocal_are_protected(self):
        before = self.cmd('show-ref')
        with self.assertRaisesRegex(GuardError, '未合入'):
            self.preview()
        with self.assertRaisesRegex(GuardError, '占用'):
            self.repo.preview_branch_delete('refs/heads/target')
        for ref in ('refs/remotes/origin/incoming', 'incoming', None, {}):
            with self.assertRaisesRegex(GuardError, '本地分支'):
                self.repo.preview_branch_delete(ref)
        self.assertEqual(self.cmd('show-ref'), before)
        self.cmd('branch', 'master', 'incoming')
        with self.assertRaisesRegex(GuardError, 'master'):
            self.repo.preview_branch_delete('refs/heads/master')

    def test_preview_rechecks_branch_tip_and_new_worktree_occupation(self):
        self.merged()
        plan = self.preview()
        self.cmd('update-ref', 'refs/heads/incoming', self.base)
        with self.assertRaisesRegex(GuardError, '已变化'):
            self.repo.delete_branch(plan['plan'], True)
        self.cmd('update-ref', 'refs/heads/incoming', self.incoming)
        plan = self.preview()
        wt = Path(self.temp.name) / 'new occupied wt'
        self.cmd('worktree', 'add', '--', str(wt), 'incoming')
        with self.assertRaisesRegex(GuardError, '占用'):
            self.repo.delete_branch(plan['plan'], True)
        self.assertTrue(wt.is_dir())
        self.assertEqual(self.cmd('rev-parse', 'incoming'), self.incoming)

    def test_explicit_confirmation_and_single_use_expiry(self):
        self.merged()
        for value in (None, False, 1, 'true', 'incoming'):
            plan = self.preview()
            with self.assertRaisesRegex(GuardError, '确认'):
                self.repo.delete_branch(plan['plan'], value)
            self.assertEqual(self.cmd('rev-parse', 'incoming'), self.incoming)
        plan = self.preview()
        self.repo.branch_delete_plans[plan['plan']]['time'] = 0
        for value in (plan['plan'], None, {}, 'unknown'):
            with self.assertRaisesRegex(GuardError, '失效'):
                self.repo.delete_branch(value, True)

    def test_wrong_upstream_and_changed_same_name_upstream_are_blocked(self):
        self.merged()
        self.local_origin()
        self.cmd('update-ref', 'refs/remotes/origin/target', self.incoming)
        self.cmd('update-ref', 'refs/remotes/origin/incoming', self.incoming)
        self.cmd('branch', '--set-upstream-to=origin/target', 'incoming')
        with self.assertRaisesRegex(GuardError, '上游不是同名'):
            self.preview()
        self.cmd('branch', '--set-upstream-to=origin/incoming', 'incoming')
        plan = self.preview()
        self.cmd('update-ref', 'refs/remotes/origin/incoming', self.base)
        with self.assertRaisesRegex(GuardError, '未合入'):
            self.repo.delete_branch(plan['plan'], True)
        self.assertEqual(self.cmd('rev-parse', 'incoming'), self.incoming)

    def test_running_jobs_and_root_git_operations_are_blocked(self):
        self.merged()
        with patch.object(self.repo.batch, 'guard', side_effect=ValueError('批量任务')):
            with self.assertRaisesRegex(ValueError, '批量'):
                self.preview()
        with patch.object(self.repo.resolver, 'guard', side_effect=ValueError('冲突任务')):
            with self.assertRaisesRegex(ValueError, '冲突'):
                self.preview()
        marker = self.path / '.git' / 'MERGE_HEAD'
        marker.write_text(self.base + '\n')
        with self.assertRaisesRegex(GuardError, 'Git 操作'):
            self.preview()


class ForceBranchDeleteChecks(unittest.TestCase):
    cmd = test_server.MergeChecks.cmd
    commit = test_server.MergeChecks.commit
    local_origin = test_server.MergeChecks.local_origin

    def setUp(self):
        original = subprocess.Popen
        def fixture_process(*args, **kwargs):
            kwargs['env'] = {**(kwargs.get('env') or os.environ), 'GIT_TRACE2_EVENT': '0'}
            return original(*args, **kwargs)
        process = patch('server.subprocess.Popen', fixture_process)
        process.start(); self.addCleanup(process.stop)
        test_server.MergeChecks.setUp(self)
        self.addCleanup(self.repo.resolver.close)

    def preview(self):
        return self.repo.preview_branch_delete('refs/heads/incoming', force=True)

    def test_force_preview_is_read_only_and_confirmation_deletes_only_local_ref(self):
        remote = self.local_origin()
        self.cmd('tag', 'keep-tag', 'incoming')
        (self.path / 'common.txt').write_text('staged work')
        self.cmd('add', 'common.txt')
        (self.path / 'untracked.txt').write_text('keep local work')
        before = self.repo.snapshot(self.target)
        refs = self.cmd('show-ref')
        index = self.cmd('ls-files', '--stage')
        remote_refs = subprocess.check_output([GIT, '--git-dir', str(remote), 'show-ref'])
        with self.assertRaisesRegex(GuardError, '未合入'):
            self.repo.preview_branch_delete('refs/heads/incoming')
        plan = self.preview()
        self.assertTrue(plan['force']); self.assertEqual(plan['count'], 1)
        self.assertIn('incoming change', plan['commits'])
        self.assertEqual(self.cmd('show-ref'), refs)
        with patch('server.git', wraps=git) as commands:
            self.repo.delete_branch(plan['plan'], True)
        self.assertTrue(any(call.args[1:3] == ('branch', '-D') for call in commands.call_args_list))
        self.assertEqual(self.repo.snapshot(self.target), before)
        self.assertEqual(self.cmd('ls-files', '--stage'), index)
        self.assertEqual((self.path / 'untracked.txt').read_text(), 'keep local work')
        self.assertNotIn('refs/heads/incoming', self.cmd('show-ref'))
        self.assertEqual(self.cmd('rev-parse', 'keep-tag'), self.incoming)
        self.assertEqual(subprocess.check_output([GIT, '--git-dir', str(remote), 'show-ref']), remote_refs)
        with self.assertRaisesRegex(GuardError, '失效'):
            self.repo.delete_branch(plan['plan'], True)

    def test_force_mode_and_confirmation_are_strict_and_expire(self):
        for force in (None, 0, 1, 'true', {}, []):
            with self.subTest(force=force), self.assertRaisesRegex(GuardError, '布尔'):
                self.repo.preview_branch_delete('refs/heads/incoming', force=force)
        for confirmation in (None, False, 1, 'true'):
            with self.subTest(confirmation=confirmation), self.assertRaisesRegex(GuardError, '确认'):
                self.repo.delete_branch(self.preview()['plan'], confirmation)
        plan = self.preview(); self.repo.branch_delete_plans[plan['plan']]['time'] -= 601
        with self.assertRaisesRegex(GuardError, '失效'):
            self.repo.delete_branch(plan['plan'], True)
        self.assertEqual(self.cmd('rev-parse', 'incoming'), self.incoming)

    def test_force_still_protects_master_current_nonlocal_and_missing_worktree(self):
        self.cmd('branch', 'master', 'incoming')
        for ref, reason in [('refs/heads/master', 'master'), ('refs/heads/target', '占用'),
                            ('refs/remotes/origin/incoming', '本地分支')]:
            with self.subTest(ref=ref), self.assertRaisesRegex(GuardError, reason):
                self.repo.preview_branch_delete(ref, force=True)
        wt = Path(self.temp.name) / 'occupied'
        self.cmd('worktree', 'add', '--', str(wt), 'incoming')
        wt.rename(wt.with_name('moved'))
        with self.assertRaisesRegex(GuardError, '占用'):
            self.preview()

    def test_tip_configuration_base_and_new_worktree_changes_invalidate_preview(self):
        plan = self.preview(); self.cmd('update-ref', 'refs/heads/incoming', self.base)
        with self.assertRaisesRegex(GuardError, '已变化'):
            self.repo.delete_branch(plan['plan'], True)
        self.cmd('update-ref', 'refs/heads/incoming', self.incoming)
        plan = self.preview(); self.cmd('config', 'user.name', 'changed fixture')
        with self.assertRaisesRegex(GuardError, '已变化'):
            self.repo.delete_branch(plan['plan'], True)
        plan = self.preview(); self.cmd('update-ref', 'refs/heads/target', self.incoming)
        with self.assertRaisesRegex(GuardError, '已变化'):
            self.repo.delete_branch(plan['plan'], True)
        plan = self.preview()
        self.cmd('worktree', 'add', '--', str(Path(self.temp.name) / 'new worktree'), 'incoming')
        with self.assertRaisesRegex(GuardError, '占用'):
            self.repo.delete_branch(plan['plan'], True)

    def test_force_protects_wrong_upstream_and_handles_missing_same_name_upstream(self):
        self.local_origin(); self.cmd('fetch', 'origin')
        self.cmd('branch', '--set-upstream-to=origin/target', 'incoming')
        with self.assertRaisesRegex(GuardError, '上游不是同名'):
            self.preview()
        self.cmd('branch', '--set-upstream-to=origin/incoming', 'incoming')
        self.cmd('update-ref', '-d', 'refs/remotes/origin/incoming')
        with self.assertRaisesRegex(GuardError, '上游引用不存在'):
            self.repo.preview_branch_delete('refs/heads/incoming')
        plan = self.preview()
        self.assertEqual(plan['base_ref'], 'target'); self.assertEqual(plan['base'], self.base)
        self.assertEqual(plan['count'], 1)
        self.repo.delete_branch(plan['plan'], True)

    def test_force_retains_running_job_and_git_operation_guards(self):
        with patch.object(self.repo.batch, 'guard', side_effect=ValueError('批量任务')):
            with self.assertRaisesRegex(ValueError, '批量'):
                self.preview()
        with patch.object(self.repo.resolver, 'guard', side_effect=ValueError('冲突任务')):
            with self.assertRaisesRegex(ValueError, '冲突'):
                self.preview()
        (self.path / '.git' / 'MERGE_HEAD').write_text(self.base + '\n')
        with self.assertRaisesRegex(GuardError, 'Git 操作'):
            self.preview()

    def test_force_commit_list_is_bounded_but_reports_total(self):
        tree = self.cmd('rev-parse', 'HEAD^{tree}'); tip = self.base
        for index in range(81):
            tip = self.cmd('commit-tree', tree, '-p', tip, '-m', 'fixture ' + str(index))
        self.cmd('update-ref', 'refs/heads/incoming', tip)
        plan = self.preview()
        self.assertEqual(plan['count'], 81); self.assertTrue(plan['truncated'])
        self.assertIn('fixture 80', plan['commits']); self.assertNotIn('fixture 0\n', plan['commits'])


class BranchDeleteRequestChecks(unittest.TestCase):
    def test_auth_jobs_and_only_reviewed_arguments_are_forwarded(self):
        for preview in (True, False):
            for token, running in [('wrong', False), ('test-token', True), ('test-token', False)]:
                handler = test_server.RequestChecks.handler(self, {'Host': '127.0.0.1:8123', 'X-Session-Token': token, 'Content-Type': 'application/json'})
                handler.path = '/api/branch/delete' + ('/preview' if preview else '')
                body = json.dumps({'ref': 'refs/heads/incoming', 'plan': 'reviewed', 'confirmation': True, 'force': True}).encode()
                handler.headers['Content-Length'] = str(len(body));handler.rfile = io.BytesIO(body)
                handler.server.repo = Mock()
                handler.server.development = SimpleNamespace(status=lambda: {'id': {'status': 'running' if running else 'ready'}})
                replies=[];handler.reply=lambda *args: replies.append(args)
                handler.do_POST()
                method = handler.server.repo.preview_branch_delete if preview else handler.server.repo.delete_branch
                if token == 'test-token' and not running:
                    self.assertEqual(replies[0][0], 200)
                    if preview:method.assert_called_once_with('refs/heads/incoming', force=True)
                    else:method.assert_called_once_with('reviewed', confirmation=True)
                else:
                    self.assertEqual(replies[0][0], 400);method.assert_not_called()
