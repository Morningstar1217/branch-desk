"""Fast-forward update checks use disposable repositories and local bare origins."""
import io
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import server
from server import GIT, GuardError
import test_server


class BranchUpdateChecks(unittest.TestCase):
    commit = test_server.MergeChecks.commit
    local_origin = test_server.MergeChecks.local_origin

    def cmd(self, *args):
        return subprocess.check_output([GIT, '-C', str(self.path), *args], stderr=subprocess.STDOUT,
                                       env={**os.environ, 'GIT_TRACE2_EVENT': '0'}).decode().strip()

    def setUp(self):
        test_server.MergeChecks.setUp(self)
        self.remote = self.local_origin()
        self.cmd('update-ref', 'refs/remotes/origin/target', self.base)
        self.cmd('branch', '--set-upstream-to=origin/target', 'target')
        self.remote_command('update-ref', 'refs/heads/target', self.incoming)

    def remote_command(self, *args):
        return subprocess.check_output([GIT, '--git-dir', str(self.remote), *args], stderr=subprocess.STDOUT,
                                       env={**os.environ, 'GIT_TRACE2_EVENT': '0'}).decode().strip()

    def update(self, **values):
        return self.repo.update_branch(values.get('path', self.target), values.get('branch', 'target'),
                                       values.get('head', self.base), values.get('confirmed', True))

    def test_fast_forward_only_updates_selected_branch_and_fetches_one_tracking_ref(self):
        self.cmd('branch', 'master', self.base)
        self.cmd('tag', 'private-tag', self.base)
        self.cmd('config', 'remote.origin.fetch', '+refs/heads/*:refs/heads/*')
        self.cmd('config', 'fetch.prune', 'true')
        self.cmd('config', 'fetch.pruneTags', 'true')
        self.cmd('config', 'remote.origin.pruneTags', 'true')
        self.cmd('config', 'remote.origin.tagOpt', '--tags')
        self.cmd('branch', '--unset-upstream', 'target')
        self.cmd('update-ref', 'refs/remotes/origin/keep', self.base)
        before_remote = self.remote_command('show-ref')
        before_config = self.cmd('config', '--null', '--list')
        self.repo.plans['old'] = {}
        result = self.update()
        self.assertEqual((result['status'], result['count'], result['state']['head']), ('updated', 1, self.incoming))
        self.assertEqual(self.cmd('rev-parse', 'master'), self.base)
        self.assertEqual(self.cmd('rev-parse', 'incoming'), self.incoming)
        self.assertEqual(self.cmd('rev-parse', 'private-tag'), self.base)
        self.assertEqual(self.cmd('rev-parse', 'refs/remotes/origin/keep'), self.base)
        self.assertEqual(self.cmd('rev-parse', 'refs/remotes/origin/target'), self.incoming)
        self.assertNotIn('refs/remotes/origin/incoming', self.cmd('show-ref'))
        self.assertEqual(self.remote_command('show-ref'), before_remote)
        self.assertEqual(self.cmd('config', '--null', '--list'), before_config)
        self.assertEqual(result['state']['upstream'], '')
        self.assertEqual(result['state']['operations'], [])
        self.assertEqual(result['state']['status'], '')
        self.assertFalse(self.repo.plans)
        self.assertEqual((self.path / 'incoming.txt').read_text(), 'incoming\n')

    def test_equal_and_local_ahead_keep_local_commits_without_pushing(self):
        self.remote_command('update-ref', 'refs/heads/target', self.base)
        self.assertEqual(self.update()['status'], 'unchanged')
        self.cmd('merge', '--ff-only', 'incoming')
        result = self.update(head=self.incoming)
        self.assertEqual((result['status'], result['count']), ('ahead', 1))
        self.assertEqual(self.cmd('rev-parse', 'HEAD'), self.incoming)
        self.assertEqual(self.remote_command('rev-parse', 'target'), self.base)

    def test_registered_worktree_updates_without_changing_other_worktree_edits(self):
        self.cmd('checkout', '-b', 'other-local-branch')
        directory = Path(self.temp.name) / 'selected worktree 中文'
        self.cmd('worktree', 'add', '--', str(directory), 'target')
        (self.path / 'keep.txt').write_text('other worktree edits')
        before = self.repo.snapshot(self.target)
        result = self.update(path=str(directory))
        self.assertEqual((result['status'], result['state']['head']), ('updated', self.incoming))
        self.assertEqual(self.repo.snapshot(self.target), before)
        self.assertEqual((self.path / 'keep.txt').read_text(), 'other worktree edits')

    def test_divergence_preserves_both_histories_and_worktree(self):
        (self.path / 'local.txt').write_text('local history')
        self.commit('local change')
        before = self.repo.snapshot(self.target)
        with self.assertRaisesRegex(GuardError, '分叉'):
            self.update(head=before['head'])
        self.assertEqual(self.repo.snapshot(self.target), before)
        self.assertEqual(self.remote_command('rev-parse', 'target'), self.incoming)

    def test_confirmation_expected_branch_head_and_registered_path_are_required(self):
        for values in ({'confirmed': False}, {'confirmed': 1}, {'branch': 'incoming'}, {'head': self.incoming},
                       {'path': str(self.path.parent)}):
            with self.subTest(values=values), self.assertRaises(GuardError):
                self.update(**values)
        self.assertEqual(self.cmd('rev-parse', 'HEAD'), self.base)

    def test_detached_dirty_git_operation_and_wrong_upstreams_are_blocked(self):
        for stage in (False, True):
            (self.path / 'keep.txt').write_text('user work')
            if stage:
                self.cmd('add', 'keep.txt')
            with self.assertRaisesRegex(GuardError, '暂存'):
                self.update()
        self.cmd('reset', '--', 'keep.txt')
        (self.path / 'keep.txt').unlink()
        self.cmd('update-ref', 'refs/remotes/origin/incoming', self.incoming)
        self.cmd('branch', '--set-upstream-to=origin/incoming', 'target')
        with self.assertRaisesRegex(GuardError, '上游'):
            self.update()
        self.cmd('remote', 'add', 'other', str(self.remote))
        self.cmd('update-ref', 'refs/remotes/other/target', self.base)
        self.cmd('branch', '--set-upstream-to=other/target', 'target')
        with self.assertRaisesRegex(GuardError, 'origin'):
            self.update()
        self.cmd('branch', '--set-upstream-to=origin/target', 'target')
        admin = Path(self.cmd('rev-parse', '--absolute-git-dir'))
        (admin / 'CHERRY_PICK_HEAD').write_text(self.incoming + '\n')
        with self.assertRaisesRegex(GuardError, 'Git 操作'):
            self.update()
        (admin / 'CHERRY_PICK_HEAD').unlink()
        self.cmd('checkout', '-b', 'master')
        state = self.repo.snapshot(self.target)
        self.assertEqual(state['blocked'], '')
        self.assertEqual(state['update_blocked'], '')
        self.cmd('checkout', '--detach')
        with self.assertRaisesRegex(GuardError, '分离'):
            self.update()

    def test_master_fast_forward_allows_local_review_and_keeps_push_blocked(self):
        self.cmd('checkout', '-b', 'master')
        self.cmd('update-ref', 'refs/remotes/origin/master', self.base)
        self.cmd('branch', '--set-upstream-to=origin/master', 'master')
        self.remote_command('update-ref', 'refs/heads/master', self.incoming)
        before_remote = self.remote_command('show-ref')
        before_config = self.cmd('config', '--null', '--list')
        result = self.update(branch='master')
        self.assertEqual((result['status'], result['count'], result['state']['head']), ('updated', 1, self.incoming))
        self.assertEqual(self.cmd('rev-parse', 'master'), self.incoming)
        self.assertEqual(self.cmd('rev-parse', 'target'), self.base)
        self.assertEqual(self.cmd('rev-parse', 'incoming'), self.incoming)
        self.assertEqual(self.remote_command('show-ref'), before_remote)
        self.assertEqual(self.cmd('config', '--null', '--list'), before_config)
        self.assertEqual(result['state']['update_blocked'], '')
        self.assertEqual(result['state']['blocked'], '')
        self.assertEqual(result['state']['status'], '')
        self.assertEqual(result['state']['operations'], [])
        self.assertEqual((self.path / 'incoming.txt').read_text(), 'incoming\n')
        plan = self.repo.preview(self.target, 'refs/heads/target')
        self.assertEqual(self.repo.merge(plan['plan'])['kind'], 'unchanged')
        self.assertEqual(self.repo.prepare_target('refs/heads/master')['branch'], 'master')
        with self.assertRaisesRegex(GuardError, '暂存区没有可提交'):
            self.repo.preview_commit(self.target, 'fix: local master commit', mode='worktree')
        with self.assertRaisesRegex(GuardError, 'master'):
            self.repo.preview_push(self.target)
        self.assertEqual(self.remote_command('show-ref'), before_remote)

    def test_master_dirty_wrong_upstream_git_operation_and_divergence_remain_blocked(self):
        self.cmd('checkout', '-b', 'master')
        self.cmd('update-ref', 'refs/remotes/origin/master', self.base)
        self.cmd('branch', '--set-upstream-to=origin/master', 'master')
        self.remote_command('update-ref', 'refs/heads/master', self.incoming)
        before_remote = self.remote_command('show-ref')
        (self.path / 'keep.txt').write_text('master user work')
        with self.assertRaisesRegex(GuardError, '暂存'):
            self.update(branch='master')
        self.assertEqual((self.path / 'keep.txt').read_text(), 'master user work')
        (self.path / 'keep.txt').unlink()
        self.cmd('branch', '--set-upstream-to=origin/target', 'master')
        with self.assertRaisesRegex(GuardError, '上游'):
            self.update(branch='master')
        self.cmd('branch', '--set-upstream-to=origin/master', 'master')
        admin = Path(self.cmd('rev-parse', '--absolute-git-dir'))
        (admin / 'CHERRY_PICK_HEAD').write_text(self.incoming + '\n')
        with self.assertRaisesRegex(GuardError, 'Git 操作'):
            self.update(branch='master')
        (admin / 'CHERRY_PICK_HEAD').unlink()
        (self.path / 'common.txt').write_text('local master commit')
        self.commit('local master change')
        head = self.cmd('rev-parse', 'HEAD')
        with self.assertRaisesRegex(GuardError, '分叉'):
            self.update(branch='master', head=head)
        self.assertEqual(self.cmd('rev-parse', 'HEAD'), head)
        self.assertEqual(self.remote_command('show-ref'), before_remote)

    def test_hidden_index_and_git_lock_block_update(self):
        self.cmd('update-index', '--assume-unchanged', 'common.txt')
        (self.path / 'common.txt').write_text('hidden user edits')
        with self.assertRaisesRegex(GuardError, 'assume-unchanged'):
            self.update()
        self.assertEqual((self.path / 'common.txt').read_text(), 'hidden user edits')
        self.cmd('update-index', '--no-assume-unchanged', 'common.txt')
        (self.path / 'common.txt').write_text('base\n')
        admin = Path(self.cmd('rev-parse', '--absolute-git-dir'))
        (admin / 'index.lock').touch()
        with self.assertRaisesRegex(GuardError, '锁'):
            self.update()

    def test_ignored_file_and_directory_collisions_are_preserved(self):
        for directory in (False, True):
            with self.subTest(directory=directory):
                self.cmd('checkout', 'incoming')
                collision = self.path / ('folder.secret' if directory else 'config.secret')
                collision.write_text('remote content')
                self.cmd('add', '-f', collision.name)
                self.cmd('commit', '-m', 'track ignored path')
                sha = self.cmd('rev-parse', 'HEAD')
                self.cmd('checkout', 'target')
                self.cmd('push', 'origin', sha + ':refs/heads/target')
                if directory:
                    collision.mkdir()
                    (collision / 'keep').write_text('user ignored content')
                else:
                    collision.write_text('user ignored content')
                with self.assertRaisesRegex(GuardError, '忽略路径'):
                    self.update()
                self.assertEqual(self.cmd('rev-parse', 'HEAD'), self.base)
                self.assertEqual((collision / 'keep' if directory else collision).read_text(), 'user ignored content')
                if directory:
                    (collision / 'keep').unlink()
                    collision.rmdir()
                else:
                    collision.unlink()

    def test_remote_absence_and_unsafe_push_config_do_not_change_local_head(self):
        self.remote_command('update-ref', '-d', 'refs/heads/target')
        with self.assertRaises(GuardError):
            self.update()
        self.remote_command('update-ref', 'refs/heads/target', self.incoming)
        for key, value in (('remote.origin.push', 'HEAD:refs/heads/master'), ('remote.origin.mirror', 'true')):
            self.cmd('config', key, value)
            with self.assertRaisesRegex(GuardError, '推送引用'):
                self.update()
            self.cmd('config', '--unset', key)
        self.assertEqual(self.cmd('rev-parse', 'HEAD'), self.base)

    def test_symbolic_tracking_ref_cannot_redirect_fetch_into_local_master(self):
        self.cmd('branch', 'master', self.base)
        self.cmd('symbolic-ref', 'refs/remotes/origin/target', 'refs/heads/master')
        with self.assertRaisesRegex(GuardError, '符号引用'):
            self.update()
        self.assertEqual(self.cmd('rev-parse', 'master'), self.base)
        self.assertEqual(self.cmd('rev-parse', 'HEAD'), self.base)

    def test_changes_during_fetch_and_running_tasks_stop_before_merge(self):
        actual = server.git

        def changed_config(path, *args, **kwargs):
            result = actual(path, *args, **kwargs)
            if 'fetch' in args:
                self.cmd('config', 'user.name', 'changed by another client')
            return result

        with patch('server.git', side_effect=changed_config), self.assertRaisesRegex(GuardError, '已变化'):
            self.update()
        self.assertEqual(self.cmd('rev-parse', 'HEAD'), self.base)

        def new_edits(path, *args, **kwargs):
            result = actual(path, *args, **kwargs)
            if 'fetch' in args:
                (self.path / 'new-user-work.txt').write_text('preserve new edits')
            return result

        with patch('server.git', side_effect=new_edits), self.assertRaisesRegex(GuardError, '已变化'):
            self.update()
        self.assertEqual((self.path / 'new-user-work.txt').read_text(), 'preserve new edits')
        self.assertEqual(self.cmd('rev-parse', 'HEAD'), self.base)
        with patch.object(self.repo.batch, 'guard', side_effect=ValueError('批量任务')), self.assertRaisesRegex(ValueError, '批量任务'):
            self.update()
        with patch.object(self.repo.resolver, 'guard', side_effect=GuardError('Codex 处理中')), self.assertRaisesRegex(GuardError, 'Codex'):
            self.update()


class RequestChecks(unittest.TestCase):
    def test_token_confirmation_payload_and_development_guard_without_http_server(self):
        for token, running in (('wrong', False), ('test-token', True), ('test-token', False)):
            handler = test_server.RequestChecks().handler({'Host': '127.0.0.1:8123', 'X-Session-Token': token,
                                                          'Content-Type': 'application/json'})
            body = json.dumps(dict(path='/fixture', branch='target', head='abc', confirmed=True)).encode()
            handler.headers['Content-Length'] = str(len(body))
            handler.path = '/api/branch/update'
            handler.rfile = io.BytesIO(body)
            operation = Mock(return_value={'status': 'updated'})
            handler.server.repo = SimpleNamespace(update_branch=operation)
            handler.server.services = SimpleNamespace()
            handler.server.development = SimpleNamespace(status=lambda: {'fixture': {'status': 'running'}} if running else {})
            handler.reply = Mock()
            handler.do_POST()
            if token == 'test-token' and not running:
                operation.assert_called_once_with('/fixture', 'target', 'abc', True)
                handler.reply.assert_called_once_with(200, {'status': 'updated'})
            else:
                operation.assert_not_called()
                self.assertEqual(handler.reply.call_args.args[0], 400)


if __name__ == '__main__':
    unittest.main()
