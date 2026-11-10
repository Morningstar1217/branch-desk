"""Release only temporary worktrees; never delete a branch or contact a remote."""
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from server import GuardError, Repository, git
import test_server


class ReleaseChecks(unittest.TestCase):
    cmd = test_server.MergeChecks.cmd
    commit = test_server.MergeChecks.commit

    def setUp(self):
        test_server.MergeChecks.setUp(self)
        self.wt = Path(self.temp.name) / '附加 worktree'
        self.cmd('worktree', 'add', '--', str(self.wt), 'incoming')
        self.wt = self.wt.resolve()
        self.process_check = patch.object(self.repo, 'check_worktree_processes')
        self.process_check.start()
        self.addCleanup(self.process_check.stop)

    def preview(self):
        return self.repo.preview_release(str(self.wt))

    def test_release_preserves_unique_commits_and_all_refs_and_allows_recreate(self):
        self.cmd('update-ref', 'refs/remotes/origin/incoming', self.base)
        refs = self.cmd('show-ref')
        plan = self.preview()
        self.assertEqual(plan['head'], self.incoming)
        self.repo.release_worktree(plan['plan'])
        self.assertFalse(self.wt.exists())
        self.assertEqual(self.cmd('show-ref'), refs)
        self.assertEqual(self.cmd('rev-parse', 'incoming'), self.incoming)
        self.assertEqual(self.cmd('status', '--porcelain'), '')
        self.assertEqual(next(b for b in self.repo.branches() if b['branch'] == 'incoming')['path'], '')
        state = self.repo.prepare_target('refs/heads/incoming')
        self.assertEqual(state['head'], self.incoming)
        with self.assertRaisesRegex(GuardError, '失效'):
            self.repo.release_worktree(plan['plan'])

    def test_dirty_staged_untracked_and_ignored_files_are_preserved(self):
        for name, stage in [('common.txt', False), ('common.txt', True), ('new.txt', False), ('keep.secret', False)]:
            with self.subTest(name=name, stage=stage):
                file = self.wt / name
                original = file.read_bytes() if file.exists() else None
                file.write_text('preserve me')
                if stage:
                    git(self.wt, 'add', '--', name)
                with self.assertRaises(GuardError):
                    self.preview()
                self.assertEqual(file.read_text(), 'preserve me')
                if stage:
                    git(self.wt, 'reset', '--', name)
                if original is None:
                    file.unlink()
                else:
                    file.write_bytes(original)

    def test_confirmation_rechecks_dirty_ignored_head_and_lock(self):
        for change in ('dirty', 'ignored', 'head', 'lock'):
            with self.subTest(change=change):
                plan = self.preview()
                if change == 'lock':
                    self.cmd('worktree', 'lock', str(self.wt))
                elif change == 'head':
                    git(self.wt, 'commit', '--allow-empty', '-m', 'changed')
                else:
                    (self.wt / ('keep.secret' if change == 'ignored' else 'new.txt')).write_text('keep')
                with self.assertRaises(GuardError):
                    self.repo.release_worktree(plan['plan'])
                self.assertTrue(self.wt.is_dir())
                if change == 'lock':
                    self.cmd('worktree', 'unlock', str(self.wt))
                elif change != 'head':
                    (self.wt / ('keep.secret' if change == 'ignored' else 'new.txt')).unlink()

    def test_main_startup_detached_and_master_are_blocked(self):
        with self.assertRaisesRegex(GuardError, '主工作区'):
            self.repo.preview_release(self.target)
        with self.assertRaisesRegex(GuardError, '启动目录'):
            Repository(self.wt).preview_release(str(self.wt))
        git(self.wt, 'checkout', '--detach')
        with self.assertRaisesRegex(GuardError, '分离 HEAD'):
            self.preview()
        git(self.wt, 'checkout', '-b', 'master')
        with self.assertRaisesRegex(GuardError, 'master'):
            self.preview()

    def test_hidden_index_changes_are_blocked(self):
        git(self.wt, 'update-index', '--assume-unchanged', 'common.txt')
        (self.wt / 'common.txt').write_text('hidden edits')
        with self.assertRaisesRegex(GuardError, '隐藏改动'):
            self.preview()

    def test_active_jobs_and_submodule_and_unregistered_are_blocked(self):
        with patch.object(self.repo.batch, 'guard', side_effect=ValueError('批量任务运行中')):
            with self.assertRaisesRegex(ValueError, '批量'):
                self.preview()
        with patch.object(self.repo.resolver, 'guard', side_effect=ValueError('冲突处理中')):
            with self.assertRaisesRegex(ValueError, '冲突'):
                self.preview()
        with self.assertRaisesRegex(GuardError, '登记'):
            self.repo.preview_release(str(self.wt.parent))
        (self.wt / 'sub').mkdir()
        git(self.wt, 'update-index', '--add', '--cacheinfo', '160000,' + self.base + ',sub')
        git(self.wt, 'commit', '-m', 'submodule fixture')
        with self.assertRaisesRegex(GuardError, '子模块'):
            self.preview()

    def test_expired_or_malformed_plan_does_not_release(self):
        plan = self.preview()
        self.repo.release_plans[plan['plan']]['time'] = 0
        for value in (plan['plan'], None, {}, 'unknown'):
            with self.assertRaisesRegex(GuardError, '失效'):
                self.repo.release_worktree(value)
        self.assertTrue(self.wt.is_dir())

    def test_generated_husky_runtime_only_is_allowed_custom_files_block(self):
        package = self.path / 'node_modules' / 'husky'
        package.mkdir(parents=True)
        (package / 'package.json').write_text('{"version":"8.0.3"}')
        (package / 'husky.sh').write_text('# standard runtime fixture\n')
        hooks = self.wt / '.husky'
        runtime = hooks / '_'
        runtime.mkdir(parents=True)
        (hooks / 'pre-commit').write_text('# tracked hook\n')
        git(self.wt, 'add', '.husky/pre-commit')
        git(self.wt, 'commit', '-m', 'hook fixture')
        (runtime / '.gitignore').write_text('*\n')
        (runtime / 'husky.sh').write_bytes((package / 'husky.sh').read_bytes())
        plan = self.preview()
        self.assertEqual(len(plan['generated']), 2)
        (runtime / 'husky.sh').write_text('custom runtime')
        with self.assertRaisesRegex(GuardError, '忽略文件'):
            self.repo.release_worktree(plan['plan'])
        (runtime / 'husky.sh').write_bytes((package / 'husky.sh').read_bytes())
        (runtime / 'local.txt').write_text('keep')
        with self.assertRaisesRegex(GuardError, '忽略文件'):
            self.preview()
        (runtime / 'local.txt').unlink()
        plan = self.preview()
        self.repo.release_worktree(plan['plan'])
        self.assertFalse(self.wt.exists())
        self.assertTrue((package / 'husky.sh').is_file())

    def test_lsof_blocks_process_but_not_sibling_paths_and_errors_fail_closed(self):
        self.process_check.stop()
        for output, blocked in [('p123\nn' + str(self.wt / 'src') + '\n', True),
                                ('p123\nn' + str(self.wt) + '-other\n', False)]:
            with patch('server.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=output)):
                if blocked:
                    with self.assertRaisesRegex(GuardError, '123'):
                        self.repo.check_worktree_processes(self.wt)
                else:
                    self.repo.check_worktree_processes(self.wt)
        with patch('server.shutil.which', return_value=None):
            with self.assertRaisesRegex(GuardError, 'lsof'):
                self.repo.check_worktree_processes(self.wt)
        with patch('server.subprocess.run', side_effect=subprocess.TimeoutExpired('lsof', 15)):
            with self.assertRaisesRegex(GuardError, '失败'):
                self.repo.check_worktree_processes(self.wt)

    def test_actual_cwd_process_blocks_release_with_spaces_and_chinese(self):
        self.process_check.stop()
        proc = subprocess.Popen([sys.executable, '-c', 'import sys;print("ready",flush=True);sys.stdin.read()'],
                                cwd=self.wt, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            self.assertEqual(proc.stdout.readline(), b'ready\n')
            with self.assertRaisesRegex(GuardError, str(proc.pid)):
                self.preview()
        finally:
            proc.communicate(input=b'', timeout=5)
        self.preview()

    def test_force_deletes_ignored_files_only_after_second_confirmation(self):
        (self.wt / 'keep.secret').write_text('discard explicitly')
        refs = self.cmd('show-ref')
        with self.assertRaisesRegex(GuardError, '忽略文件'):
            self.preview()
        plan = self.repo.preview_release(str(self.wt), force=True)
        self.assertTrue(plan['force'])
        self.assertEqual(plan['ignored'], [dict(path='keep.secret', kind='文件', count=1)])
        for confirmation in (None, False, 'incoming', 1, 'true'):
            if confirmation is not None:
                plan = self.repo.preview_release(str(self.wt), force=True)
            with self.assertRaisesRegex(GuardError, '二次确认'):
                self.repo.release_worktree(plan['plan'], confirmation)
            self.assertTrue((self.wt / 'keep.secret').exists())
        plan = self.repo.preview_release(str(self.wt), force=True)
        self.repo.release_worktree(plan['plan'], True)
        self.assertFalse(self.wt.exists())
        self.assertEqual(self.cmd('show-ref'), refs)
        self.assertEqual(self.cmd('rev-parse', 'incoming'), self.incoming)

    def test_force_rechecks_ignored_file_content_metadata_and_inventory(self):
        file = self.wt / 'keep.secret'
        file.write_text('before')
        plan = self.repo.preview_release(str(self.wt), force=True)
        before = file.stat()
        file.write_text('edited')
        os.utime(file, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaisesRegex(GuardError, '已变化'):
            self.repo.release_worktree(plan['plan'], True)
        self.assertEqual(file.read_text(), 'edited')
        plan = self.repo.preview_release(str(self.wt), force=True)
        (self.wt / 'new.secret').write_text('new local file')
        with self.assertRaisesRegex(GuardError, '已变化'):
            self.repo.release_worktree(plan['plan'], True)
        self.assertTrue(file.exists())

    def test_force_removes_symlink_without_touching_external_dependency(self):
        external = Path(self.temp.name) / 'shared dependencies'
        external.mkdir()
        (external / 'keep.txt').write_text('shared data')
        (self.wt / 'modules.secret').symlink_to(external, target_is_directory=True)
        plan = self.repo.preview_release(str(self.wt), force=True)
        self.assertEqual(plan['ignored'], [dict(path='modules.secret', kind='链接', count=1)])
        self.repo.release_worktree(plan['plan'], True)
        self.assertEqual((external / 'keep.txt').read_text(), 'shared data')
        self.assertFalse(self.wt.exists())

    def test_force_previews_recursive_directory_and_blocks_special_files(self):
        cache = self.wt / 'cache.secret'
        (cache / 'nested').mkdir(parents=True)
        (cache / 'nested' / 'cache.bin').write_bytes(b'discard')
        plan = self.repo.preview_release(str(self.wt), force=True)
        self.assertEqual(plan['ignored'], [dict(path='cache.secret/', kind='目录', count=3)])
        os.mkfifo(cache / 'special')
        with self.assertRaisesRegex(GuardError, '特殊本地文件'):
            self.repo.release_worktree(plan['plan'], True)
        (cache / 'special').unlink()
        plan = self.repo.preview_release(str(self.wt), force=True)
        self.repo.release_worktree(plan['plan'], True)
        self.assertFalse(cache.exists())
        self.assertEqual(self.cmd('rev-parse', 'incoming'), self.incoming)

    def test_force_still_blocks_dirty_locked_main_nested_repository_and_active_process(self):
        with self.assertRaisesRegex(GuardError, '主工作区'):
            self.repo.preview_release(self.target, force=True)
        (self.wt / 'new.txt').write_text('untracked data')
        with self.assertRaisesRegex(GuardError, '未提交'):
            self.repo.preview_release(str(self.wt), force=True)
        (self.wt / 'new.txt').unlink()
        self.cmd('worktree', 'lock', str(self.wt))
        with self.assertRaisesRegex(GuardError, '锁定'):
            self.repo.preview_release(str(self.wt), force=True)
        self.cmd('worktree', 'unlock', str(self.wt))
        (self.wt / 'repository.secret').mkdir()
        (self.wt / 'repository.secret' / '.git').write_text('nested git marker')
        with self.assertRaisesRegex(GuardError, '嵌套 Git'):
            self.repo.preview_release(str(self.wt), force=True)
        (self.wt / 'repository.secret' / '.git').unlink()
        self.repo.check_worktree_processes.side_effect = GuardError('进程占用')
        with self.assertRaisesRegex(GuardError, '进程占用'):
            self.repo.preview_release(str(self.wt), force=True)

    def test_normal_plan_cannot_be_upgraded_to_force_by_confirmation(self):
        plan = self.preview()
        (self.wt / 'keep.secret').write_text('keep')
        with self.assertRaisesRegex(GuardError, '忽略文件'):
            self.repo.release_worktree(plan['plan'], True)
        for value in ('true', 1, None):
            with self.assertRaisesRegex(GuardError, '布尔值'):
                self.repo.preview_release(str(self.wt), force=value)
        self.assertTrue((self.wt / 'keep.secret').exists())


class ReleaseRequestChecks(unittest.TestCase):
    def test_routes_require_token_check_development_and_forward_only_plan_or_path(self):
        for route, method, argument in [('preview', 'preview_release', '/fixture'), ('', 'release_worktree', 'id')]:
            for token, running in [('wrong', False), ('test-token', True), ('test-token', False)]:
                handler = test_server.RequestChecks.handler(self, {'Host': '127.0.0.1:8123', 'X-Session-Token': token,
                                                       'Content-Type': 'application/json'})
                handler.path = '/api/worktree/release' + ('/preview' if route else '')
                body = json.dumps({'path': '/fixture', 'plan': 'id', 'force': True, 'confirmation': True}).encode()
                handler.headers['Content-Length'] = str(len(body))
                handler.rfile = io.BytesIO(body)
                handler.server.repo = Mock()
                handler.server.development = SimpleNamespace(status=lambda: {'id': {'status': 'running' if running else 'ready'}})
                replies = []
                handler.reply = lambda *args: replies.append(args)
                handler.do_POST()
                if token == 'test-token' and not running:
                    self.assertEqual(replies[0][0], 200)
                    options = dict(force=True) if route else dict(confirmation=True)
                    getattr(handler.server.repo, method).assert_called_once_with(argument, **options)
                else:
                    self.assertEqual(replies[0][0], 400)
                    getattr(handler.server.repo, method).assert_not_called()
