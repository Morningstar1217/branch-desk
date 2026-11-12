"""Merged-branch cleanup uses disposable Git repositories, never business branches."""
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from server import GuardError, git
import test_server


class MergedCleanupChecks(unittest.TestCase):
    cmd = test_server.MergeChecks.cmd
    commit = test_server.MergeChecks.commit
    local_origin = test_server.MergeChecks.local_origin

    def setUp(self):
        test_server.MergeChecks.setUp(self)
        self.local_origin()
        self.cmd('update-ref', 'refs/remotes/origin/master', self.incoming)
        self.cmd('update-ref', 'refs/remotes/origin/incoming', self.incoming)
        self.cmd('branch', '--set-upstream-to=origin/incoming', 'incoming')
        pinned = patch('server.read_pinned', return_value=[])
        processes = patch.object(self.repo, 'check_worktree_processes')
        self.pinned = pinned.start()
        self.processes = processes.start()
        self.addCleanup(pinned.stop)
        self.addCleanup(processes.stop)
        self.ref = 'refs/heads/incoming'

    def worktree(self):
        path = Path(self.temp.name) / 'worktree with spaces 中文'
        self.cmd('worktree', 'add', '--', str(path), 'incoming')
        return path

    def preview(self, discard=False):
        return self.repo.preview_merged_cleanup(self.ref, discard)

    def test_read_only_list_and_cleanup_preserve_remote_refs_and_launch_changes(self):
        wt = self.worktree()
        (self.path / 'common.txt').write_text('keep staged work')
        self.cmd('add', 'common.txt')
        (self.path / 'new.txt').write_text('keep untracked work')
        before = self.repo.snapshot(self.target)
        refs = self.cmd('show-ref')
        listing = self.repo.merged_cleanup_list()
        self.assertEqual(next(row for row in listing['rows'] if row['ref'] == self.ref)['reason'], '')
        self.assertEqual(self.cmd('show-ref'), refs)
        plan = self.preview()
        self.assertTrue(wt.is_dir())
        self.assertEqual(plan['base'], self.incoming)
        remote_refs = git(self.path, 'for-each-ref', 'refs/remotes/')[1]
        result = self.repo.cleanup_merged_branch(plan['plan'], True)
        self.assertEqual((result['status'], result['released'], result['deleted']), ('succeeded', True, True))
        self.assertFalse(wt.exists())
        self.assertFalse(any(ref['ref'] == self.ref for ref in self.repo.refs()))
        self.assertEqual(git(self.path, 'for-each-ref', 'refs/remotes/')[1], remote_refs)
        self.assertEqual(self.repo.snapshot(self.target), before)

    def test_pushed_but_unmerged_is_retained_and_baseline_is_required(self):
        self.cmd('update-ref', 'refs/remotes/origin/master', self.base)
        with self.assertRaisesRegex(GuardError, '尚未完整合入'):
            self.preview()
        self.cmd('update-ref', '-d', 'refs/remotes/origin/master')
        with self.assertRaisesRegex(GuardError, '缺少 origin/master'):
            self.repo.merged_cleanup_list()

    def test_wrong_missing_or_unmerged_upstream_stops_before_releasing(self):
        wt = self.worktree()
        self.cmd('update-ref', 'refs/remotes/origin/target', self.incoming)
        self.cmd('branch', '--set-upstream-to=origin/target', 'incoming')
        with self.assertRaisesRegex(GuardError, '上游不是同名'):
            self.preview()
        self.cmd('branch', '--set-upstream-to=origin/incoming', 'incoming')
        self.cmd('update-ref', 'refs/remotes/origin/incoming', self.base)
        with self.assertRaisesRegex(GuardError, '普通删除'):
            self.preview()
        self.cmd('update-ref', '-d', 'refs/remotes/origin/incoming')
        with self.assertRaises(GuardError):
            self.preview()
        self.cmd('branch', '--unset-upstream', 'incoming')
        with self.assertRaisesRegex(GuardError, '普通删除'):
            self.preview()
        self.assertTrue(wt.exists())

    def test_environment_sentry_pinned_and_launch_branches_are_protected(self):
        for branch in ('master', 'main', 'pre', 'test', 'M-sentry错误修复', 'feature/SENTRY-log'):
            self.cmd('branch', branch, 'incoming')
            with self.subTest(branch=branch), self.assertRaisesRegex(GuardError, '环境分支和 Sentry'):
                self.repo.preview_merged_cleanup('refs/heads/' + branch, True)
        self.pinned.return_value = [self.ref]
        with self.assertRaisesRegex(GuardError, '常用分支'):
            self.preview(True)
        self.pinned.return_value = []
        with self.assertRaisesRegex(GuardError, '启动工作区'):
            self.repo.preview_merged_cleanup('refs/heads/target')

    def test_dirty_locked_hidden_and_occupied_worktrees_never_release(self):
        wt = self.worktree()
        (wt / 'common.txt').write_text('uncommitted')
        with self.assertRaisesRegex(GuardError, '未提交'):
            self.preview(True)
        git(wt, 'checkout', '--', 'common.txt')
        git(wt, 'update-index', '--assume-unchanged', 'common.txt')
        with self.assertRaisesRegex(GuardError, '隐藏改动'):
            self.preview(True)
        git(wt, 'update-index', '--no-assume-unchanged', 'common.txt')
        self.cmd('worktree', 'lock', str(wt))
        with self.assertRaisesRegex(GuardError, '锁定'):
            self.preview(True)
        self.cmd('worktree', 'unlock', str(wt))
        self.processes.side_effect = GuardError('工作区仍有进程占用')
        with self.assertRaisesRegex(GuardError, '占用'):
            self.preview(True)
        self.assertTrue(wt.is_dir())
        self.assertEqual(self.cmd('rev-parse', self.ref), self.incoming)

    def test_changed_base_tip_pins_and_tracking_invalidate_preview(self):
        for change in ('base', 'tip', 'pins', 'tracking'):
            with self.subTest(change=change):
                plan = self.preview()
                if change == 'base':
                    self.cmd('update-ref', 'refs/remotes/origin/master', self.base)
                elif change == 'tip':
                    self.cmd('update-ref', self.ref, self.base)
                elif change == 'pins':
                    self.pinned.return_value = [self.ref]
                else:
                    self.cmd('branch', '--unset-upstream', 'incoming')
                with self.assertRaises(GuardError):
                    self.repo.cleanup_merged_branch(plan['plan'], True)
                self.cmd('update-ref', 'refs/remotes/origin/master', self.incoming)
                self.cmd('update-ref', self.ref, self.incoming)
                self.cmd('branch', '--set-upstream-to=origin/incoming', 'incoming')
                self.pinned.return_value = []

    def test_confirmation_expiry_single_use_and_strict_options(self):
        for value in (None, False, 1, 'true'):
            plan = self.preview()
            with self.assertRaisesRegex(GuardError, '确认'):
                self.repo.cleanup_merged_branch(plan['plan'], value)
            with self.assertRaisesRegex(GuardError, '失效'):
                self.repo.cleanup_merged_branch(plan['plan'], True)
        plan = self.preview()
        self.repo.merged_cleanup_plans[plan['plan']]['time'] = 0
        with self.assertRaisesRegex(GuardError, '失效'):
            self.repo.cleanup_merged_branch(plan['plan'], True)
        for value in (1, None, 'true', {}):
            with self.assertRaisesRegex(GuardError, '布尔值'):
                self.preview(value)
        with self.assertRaisesRegex(GuardError, '本地分支'):
            self.repo.preview_merged_cleanup('refs/remotes/origin/incoming')

    def test_ignored_inventory_requires_second_confirmation_and_keeps_link_target(self):
        wt = self.worktree()
        outside = Path(self.temp.name) / 'outside.secret'
        outside.write_text('external content')
        ignored = wt / 'local.secret'
        kind = '链接'
        try:
            ignored.symlink_to(outside)
        except OSError as error:
            if getattr(error, 'winerror', None) != 1314:
                raise
            # Keep testing the second confirmation on Windows without symlink privileges.
            ignored.write_text('local content')
            kind = '文件'
        with self.assertRaisesRegex(GuardError, '忽略文件'):
            self.preview()
        plan = self.preview(True)
        self.assertEqual(plan['ignored'], [{'path': ignored.name, 'kind': kind, 'count': 1}])
        with self.assertRaisesRegex(GuardError, '二次确认'):
            self.repo.cleanup_merged_branch(plan['plan'], True, False)
        self.assertTrue(wt.exists())
        result = self.repo.cleanup_merged_branch(self.preview(True)['plan'], True, True)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(outside.read_text(), 'external content')

    def test_ignored_changes_after_preview_and_nested_git_are_blocked(self):
        wt = self.worktree()
        ignored = wt / 'local.secret'
        ignored.write_text('before')
        plan = self.preview(True)
        ignored.write_text('changed after review')
        with self.assertRaisesRegex(GuardError, '已变化'):
            self.repo.cleanup_merged_branch(plan['plan'], True, True)
        ignored.unlink()
        ignored.mkdir()
        (ignored / '.git').mkdir()
        with self.assertRaisesRegex(GuardError, '嵌套 Git'):
            self.preview(True)
        self.assertTrue(wt.exists())

    def test_sequential_plans_survive_prior_branch_tracking_removal(self):
        self.cmd('branch', 'another', 'incoming')
        self.cmd('update-ref', 'refs/remotes/origin/another', self.incoming)
        self.cmd('branch', '--set-upstream-to=origin/another', 'another')
        one = self.preview()
        two = self.repo.preview_merged_cleanup('refs/heads/another')
        for plan in (one, two):
            result = self.repo.cleanup_merged_branch(plan['plan'], True)
            self.assertEqual(result['status'], 'succeeded', result)

    def test_failed_deletion_reports_completed_release_and_retains_branch(self):
        wt = self.worktree()
        plan = self.preview()
        with patch.object(self.repo, 'delete_branch', side_effect=GuardError('fixture: branch locked')):
            result = self.repo.cleanup_merged_branch(plan['plan'], True)
        self.assertEqual((result['status'], result['released'], result['deleted']), ('failed', True, False))
        self.assertFalse(wt.exists())
        self.assertEqual(self.cmd('rev-parse', self.ref), self.incoming)


class MergedCleanupRequestChecks(unittest.TestCase):
    def test_session_and_running_jobs_guard_all_routes_and_confirmation_is_forwarded(self):
        for route, method, args in (
            ('list', 'merged_cleanup_list', (True,)),
            ('preview', 'preview_merged_cleanup', ('refs/heads/incoming', True)),
            ('delete', 'cleanup_merged_branch', ('reviewed', True, True)),
        ):
            for token, running in [('wrong', False), ('test-token', True), ('test-token', False)]:
                handler = test_server.RequestChecks.handler(self, {'Host': '127.0.0.1:8123', 'X-Session-Token': token, 'Content-Type': 'application/json'})
                handler.path = '/api/branch/merged/' + route
                body = json.dumps(dict(ref='refs/heads/incoming', plan='reviewed', discard_ignored=True,
                                       confirmation=True, discard_confirmation=True)).encode()
                handler.headers['Content-Length'] = str(len(body))
                handler.rfile = io.BytesIO(body)
                handler.server.repo = Mock()
                handler.server.development = SimpleNamespace(status=lambda: {'id': {'status': 'running' if running else 'ready'}})
                replies = []
                handler.reply = lambda *args: replies.append(args)
                handler.do_POST()
                called = getattr(handler.server.repo, method)
                if token == 'test-token' and not running:
                    self.assertEqual(replies[0][0], 200)
                    called.assert_called_once_with(*args)
                else:
                    self.assertEqual(replies[0][0], 400)
                    called.assert_not_called()
