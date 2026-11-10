"""Ordinary commits only touch disposable Git fixtures; no server or business repo."""
import io
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from server import GIT, GuardError
import test_server


class WorktreeCommitChecks(unittest.TestCase):
    def setUp(self):
        # Disable the machine's Git AI Trace2 listener in every fixture subprocess,
        # including server.git(), which normally sanitizes inherited GIT_* values.
        original_process = subprocess.Popen

        def fixture_process(*args, **kwargs):
            kwargs['env'] = {**(kwargs.get('env') or os.environ), 'GIT_TRACE2_EVENT': '0'}
            return original_process(*args, **kwargs)

        process_patch = patch('server.subprocess.Popen', fixture_process)
        process_patch.start()
        self.addCleanup(process_patch.stop)
        self.f = test_server.MergeChecks('test_fast_forward_stays_uncommitted_and_preserves_refs')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.repo, self.path = self.f.repo, self.f.target
        self.addCleanup(self.repo.resolver.close)

    def raw(self, *args):
        return subprocess.check_output([GIT, '-C', self.path, *args], stderr=subprocess.STDOUT)

    def write(self, name, content):
        (self.f.path / name).write_text(content, encoding='utf-8')

    def staged(self):
        return set(self.raw('diff', '--cached', '--name-only', '--no-renames', '-z').decode().rstrip('\0').split('\0')) - {''}

    def state(self):
        return (self.raw('rev-parse', 'HEAD'), self.raw('ls-files', '--stage', '-z'),
                self.raw('config', '--null', '--list'), self.raw('status', '--porcelain=v1', '-z'))

    def preview(self):
        return self.repo.preview_changes(self.path)

    def stage(self, files, message='fix: selected fixture files'):
        return self.repo.stage_changes(self.preview()['plan'], files, message)

    def hook(self, name, content):
        directory = Path(self.f.temp.name) / 'hooks'
        directory.mkdir(exist_ok=True)
        script = directory / name
        script.write_text('#!/bin/sh\n' + content + '\n')
        script.chmod(0o755)
        self.f.cmd('config', 'core.hooksPath', str(directory))

    def test_preview_is_read_only_and_reports_staged_unstaged_untracked(self):
        self.write('common.txt', 'staged version\n')
        self.f.cmd('add', 'common.txt')
        self.write('common.txt', 'working version\n')
        self.write('staged.txt', 'staged new file\n')
        self.f.cmd('add', 'staged.txt')
        self.write('untracked.txt', 'untracked\n')
        self.write('ignored.secret', 'keep private\n')
        (self.f.path / '.gitignore').unlink()
        # Restore ignore policy through .git/info/exclude without changing index.
        exclude = self.f.path / '.git' / 'info' / 'exclude'
        exclude.parent.mkdir(exist_ok=True)
        exclude.write_text('*.secret\n')
        before = self.state()
        index = (self.f.path / '.git' / 'index').read_bytes()
        result = self.preview()
        self.assertEqual(self.state(), before)
        self.assertEqual((self.f.path / '.git' / 'index').read_bytes(), index)
        self.assertEqual(result['branch'], 'target')
        self.assertEqual(result['head'], self.f.base)
        self.assertEqual(result['path'], self.path)
        files = {item['path']: item for item in result['files']}
        self.assertEqual(set(files), {'common.txt', 'staged.txt', '.gitignore', 'untracked.txt'})
        for name, flags in {'common.txt': ('M', 'M', True, True, False),
                            'staged.txt': ('A', ' ', True, False, False),
                            '.gitignore': (' ', 'D', False, True, False),
                            'untracked.txt': ('?', '?', False, False, True)}.items():
            self.assertEqual(tuple(files[name][key] for key in ('index', 'worktree', 'staged', 'unstaged', 'untracked')), flags)
        self.assertEqual(self.repo.commit_plans, {})
        self.assertEqual((self.f.path / 'common.txt').read_text(), 'working version\n')

    def test_selected_files_include_existing_index_without_overwriting_partial_staging(self):
        remote = self.f.local_origin()
        remote_before = subprocess.check_output([GIT, '--git-dir', str(remote), 'show-ref'])
        self.write('common.txt', 'reviewed staged content\n')
        self.f.cmd('add', 'common.txt')
        self.write('common.txt', 'keep unstaged content\n')
        self.write('selected.txt', 'selected\n')
        self.write('unselected.txt', 'keep untracked\n')
        self.write('private.secret', 'keep ignored\n')
        changes = self.preview()
        plan = self.repo.stage_changes(changes['plan'], ['selected.txt'], 'fix: selected files')
        self.assertEqual(plan['mode'], 'worktree')
        self.assertIn('全部', plan['warning'])
        self.assertEqual(self.staged(), {'common.txt', 'selected.txt'})
        self.assertEqual(self.f.cmd('show', ':common.txt'), 'reviewed staged content')
        self.assertEqual(self.f.cmd('rev-parse', 'HEAD'), self.f.base)
        with self.assertRaisesRegex(GuardError, '失效'):
            self.repo.stage_changes(changes['plan'], ['selected.txt'], 'fix: selected files')
        result = self.repo.commit_merge(plan['plan'])
        self.assertEqual(result['message'], 'fix: selected files')
        self.assertEqual(self.f.cmd('rev-list', '--parents', '-n', '1', 'HEAD').split()[1:], [self.f.base])
        self.assertEqual(self.f.cmd('show', 'HEAD:common.txt'), 'reviewed staged content')
        self.assertEqual((self.f.path / 'common.txt').read_text(), 'keep unstaged content\n')
        self.assertEqual(self.staged(), set())
        self.assertEqual(set(self.raw('ls-tree', '-r', '--name-only', '-z', 'HEAD').decode().rstrip('\0').split('\0')),
                         {'.gitignore', 'common.txt', 'selected.txt'})
        self.assertEqual((self.f.path / 'unselected.txt').read_text(), 'keep untracked\n')
        self.assertEqual((self.f.path / 'private.secret').read_text(), 'keep ignored\n')
        self.assertEqual(subprocess.check_output([GIT, '--git-dir', str(remote), 'show-ref']), remote_before)
        with self.assertRaisesRegex(GuardError, '失效'):
            self.repo.commit_merge(plan['plan'])

    def test_selecting_partially_staged_file_explicitly_stages_its_working_content(self):
        self.write('common.txt', 'staged version\n')
        self.f.cmd('add', 'common.txt')
        self.write('common.txt', 'selected working version\n')
        plan = self.stage(['common.txt'])
        self.assertEqual(self.f.cmd('show', ':common.txt'), 'selected working version')
        self.repo.commit_merge(plan['plan'])
        self.assertEqual(self.f.cmd('show', 'HEAD:common.txt'), 'selected working version')

    def test_empty_selection_can_commit_existing_index_and_preserves_unstaged_content(self):
        self.write('common.txt', 'staged only\n')
        self.f.cmd('add', 'common.txt')
        self.write('common.txt', 'unstaged only\n')
        plan = self.stage([])
        self.assertEqual(plan['state']['path'], self.path)
        self.assertEqual(plan['state']['branch'], 'target')
        self.assertTrue(plan['state']['status'])
        self.assertEqual(self.f.cmd('show', ':common.txt'), 'staged only')
        self.repo.commit_merge(plan['plan'])
        self.assertEqual(self.f.cmd('show', 'HEAD:common.txt'), 'staged only')
        self.assertEqual((self.f.path / 'common.txt').read_text(), 'unstaged only\n')

    def test_all_staged_files_preview_without_selectable_files_keeps_workspace_state(self):
        self.write('common.txt', 'staged only\n')
        self.f.cmd('add', 'common.txt')
        plan = self.stage([])
        self.assertEqual(plan['mode'], 'worktree')
        self.assertEqual(plan['state']['path'], self.path)
        self.assertEqual(plan['state']['branch'], 'target')
        self.assertTrue(plan['state']['status'])
        result = self.repo.commit_merge(plan['plan'])
        self.assertEqual(result['state']['branch'], 'target')
        self.assertEqual(self.f.cmd('show', 'HEAD:common.txt'), 'staged only')

    def test_already_staged_selected_file_can_be_committed(self):
        self.write('common.txt', 'staged only\n')
        self.f.cmd('add', 'common.txt')
        plan = self.stage(['common.txt'])
        self.assertEqual(self.repo.commit_merge(plan['plan'])['state']['operations'], [])

    def test_staged_and_unstaged_deletions_are_committed_only_as_selected(self):
        (self.f.path / 'common.txt').unlink()
        self.f.cmd('add', '--', 'common.txt')
        (self.f.path / '.gitignore').unlink()
        plan = self.stage(['common.txt'])
        self.assertEqual(self.staged(), {'common.txt'})
        self.repo.commit_merge(plan['plan'])
        self.assertEqual(self.f.cmd('ls-tree', '--name-only', 'HEAD', '--', 'common.txt'), '')
        self.assertEqual(self.f.cmd('show', 'HEAD:.gitignore'), '*.secret')
        self.assertFalse((self.f.path / '.gitignore').exists())
        plan = self.stage(['.gitignore'])
        self.repo.commit_merge(plan['plan'])
        self.assertEqual(self.f.cmd('ls-tree', '--name-only', 'HEAD'), '')

    def test_empty_selection_without_staged_content_is_rejected_without_staging(self):
        self.write('common.txt', 'keep unstaged\n')
        before = self.state()
        with self.assertRaisesRegex(GuardError, '请选择'):
            self.stage([])
        self.assertEqual(self.state(), before)

    def test_selection_that_removes_last_staged_change_does_not_create_empty_commit(self):
        self.write('common.txt', 'staged change\n')
        self.f.cmd('add', 'common.txt')
        self.write('common.txt', 'base\n')
        with self.assertRaisesRegex(GuardError, '没有可提交'):
            self.stage(['common.txt'])
        self.assertEqual(self.staged(), set())
        self.assertEqual(self.f.cmd('rev-parse', 'HEAD'), self.f.base)

    def assert_stale_stage(self, mutation):
        self.write('common.txt', 'first content\n')
        plan = self.preview()
        mutation()
        before = self.state()
        with self.assertRaisesRegex(GuardError, '变化'):
            self.repo.stage_changes(plan['plan'], ['common.txt'], 'fix: stale preview')
        self.assertEqual(self.state(), before)
        self.assertEqual(self.repo.commit_plans, {})

    def test_changed_working_content_with_same_status_rejects_staging(self):
        self.assert_stale_stage(lambda: self.write('common.txt', 'other content\n'))

    def test_changed_index_rejects_staging(self):
        self.assert_stale_stage(lambda: self.f.cmd('add', 'common.txt'))

    def test_changed_configuration_rejects_staging(self):
        self.assert_stale_stage(lambda: self.f.cmd('config', 'user.name', 'Changed Fixture'))

    def test_expired_change_plan_rejects_staging(self):
        self.write('common.txt', 'keep unstaged\n')
        plan = self.preview()
        self.repo.change_plans[plan['plan']]['time'] -= 601
        before = self.state()
        with self.assertRaisesRegex(GuardError, '失效'):
            self.repo.stage_changes(plan['plan'], ['common.txt'], 'fix: expired preview')
        self.assertEqual(self.state(), before)

    def assert_stale_commit(self, mutation):
        self.write('common.txt', 'selected content\n')
        self.write('unselected.txt', 'first unselected content\n')
        plan = self.stage(['common.txt'])
        mutation()
        before = self.state()
        with self.assertRaisesRegex(GuardError, '变化'):
            self.repo.commit_merge(plan['plan'])
        self.assertEqual(self.state(), before)
        self.assertEqual(self.f.cmd('rev-parse', 'HEAD'), self.f.base)

    def test_unselected_working_content_change_invalidates_commit(self):
        self.assert_stale_commit(lambda: self.write('unselected.txt', 'other unselected content\n'))

    def test_index_change_invalidates_ordinary_commit(self):
        self.assert_stale_commit(lambda: self.f.cmd('add', 'unselected.txt'))

    def test_configuration_change_invalidates_ordinary_commit(self):
        self.assert_stale_commit(lambda: self.f.cmd('config', 'user.email', 'changed@example.invalid'))

    def test_invalid_path_lists_are_rejected_without_changing_index(self):
        self.write('common.txt', 'keep unstaged\n')
        self.write('selected.txt', 'keep untracked\n')
        for files in (None, {}, 'common.txt', [1], [None], ['common.txt', 'common.txt'],
                      ['../outside'], ['/etc/passwd'], ['.git/config'], ['missing.txt'],
                      [':(glob)*'], ['*'], ['']):
            with self.subTest(files=files):
                plan = self.preview()
                before = self.state()
                with self.assertRaises(GuardError):
                    self.repo.stage_changes(plan['plan'], files, 'fix: invalid selection')
                self.assertEqual(self.state(), before)

    def test_invalid_messages_are_rejected_before_staging(self):
        self.write('common.txt', 'keep unstaged\n')
        for message in (None, {}, '', 'plain message', 'fix!: breaking', 'fix: ends.',
                        'fix: ' + 'x' * 104, 'fix: body\nno blank line', 'fix: nul\0', 'x' * 4097):
            with self.subTest(message=message):
                plan = self.preview()
                before = self.state()
                with self.assertRaises(GuardError):
                    self.repo.stage_changes(plan['plan'], ['common.txt'], message)
                self.assertEqual(self.state(), before)

    def test_validation_error_can_be_corrected_with_same_read_only_preview(self):
        self.write('common.txt', 'selected content\n')
        changes = self.preview()
        with self.assertRaises(GuardError):
            self.repo.stage_changes(changes['plan'], ['common.txt'], 'invalid message')
        self.assertEqual(self.staged(), set())
        plan = self.repo.stage_changes(changes['plan'], ['common.txt'], 'fix: corrected message')
        self.assertEqual(plan['message'], 'fix: corrected message')
        self.assertEqual(self.staged(), {'common.txt'})

    def test_unregistered_paths_are_rejected(self):
        for path in (None, {}, self.f.temp.name, str(self.f.path / 'common.txt')):
            with self.subTest(path=path), self.assertRaisesRegex(GuardError, '登记'):
                self.repo.preview_changes(path)

    def test_nested_worktree_is_excluded_without_blocking_parent_commit(self):
        nested = self.f.path / '.aidp' / 'worktrees' / 'another'
        self.f.cmd('worktree', 'add', '--no-track', '-b', 'another', str(nested), 'HEAD')
        (nested / 'local.txt').write_text('keep nested edit\n')
        self.write('common.txt', 'parent change\n')
        with self.assertRaisesRegex(GuardError, '嵌套'):
            self.preview()
        (self.f.path / '.git' / 'info').mkdir(exist_ok=True)
        (self.f.path / '.git' / 'info' / 'exclude').write_text('.aidp/\n')
        plan = self.stage(['common.txt'])
        self.repo.commit_merge(plan['plan'])
        self.assertEqual(self.f.cmd('show', 'HEAD:common.txt'), 'parent change')
        self.assertEqual(self.f.cmd('-C', str(nested), 'rev-parse', 'HEAD'), self.f.base)
        self.assertEqual((nested / 'local.txt').read_text(), 'keep nested edit\n')

    def test_special_filenames_are_literal_and_do_not_stage_matching_neighbors(self):
        selected = ['[abc].txt', 'wild*.txt', ':(glob)*', '-leading.txt', '中文 space.txt', 'line\nbreak.txt']
        neighbors = ['a.txt', 'wild-neighbor.txt', 'keep.txt']
        for name in selected + neighbors:
            self.write(name, name + '\n')
        files = {entry['path'] for entry in self.preview()['files']}
        self.assertEqual(files, set(selected + neighbors))
        plan = self.stage(selected)
        self.assertEqual(self.staged(), set(selected))
        self.repo.commit_merge(plan['plan'])
        committed = set(self.raw('ls-tree', '-r', '--name-only', '-z', 'HEAD').decode().rstrip('\0').split('\0'))
        self.assertEqual(committed, set(selected) | {'.gitignore', 'common.txt'})
        self.assertTrue(all((self.f.path / name).exists() for name in neighbors))

    def test_master_can_commit_locally_while_detached_head_is_blocked(self):
        self.f.cmd('checkout', '-b', 'master')
        self.write('common.txt', 'local master change\n')
        plan = self.stage(['common.txt'], 'fix: local master change')
        result = self.repo.commit_merge(plan['plan'])
        self.assertEqual(result['state']['branch'], 'master')
        self.assertEqual(self.f.cmd('rev-list', '--parents', '-n', '1', 'HEAD').split()[1:], [self.f.base])
        self.assertEqual(self.f.cmd('show', 'HEAD:common.txt'), 'local master change')
        with self.assertRaisesRegex(GuardError, '禁止推送 master'):
            self.repo.preview_push(self.path)
        self.f.cmd('checkout', 'target')
        self.f.cmd('checkout', '--detach')
        with self.assertRaisesRegex(GuardError, '分离 HEAD'):
            self.preview()
        self.f.cmd('checkout', 'target')

    def test_ongoing_merge_and_other_git_operations_are_blocked(self):
        self.repo.merge(self.f.plan()['plan'])
        with self.assertRaisesRegex(GuardError, 'Git 操作'):
            self.preview()
        self.f.cmd('merge', '--abort')
        for name in ('CHERRY_PICK_HEAD', 'REVERT_HEAD', 'BISECT_LOG'):
            marker = self.f.path / '.git' / name
            marker.write_text(self.f.base + '\n')
            try:
                with self.subTest(operation=name), self.assertRaisesRegex(GuardError, 'Git 操作'):
                    self.preview()
            finally:
                marker.unlink()

    def test_wrong_upstream_and_unsafe_push_configurations_are_blocked(self):
        self.f.local_origin()
        self.f.cmd('fetch', 'origin')
        self.f.cmd('branch', '--set-upstream-to=origin/incoming', 'target')
        with self.assertRaisesRegex(GuardError, '同名'):
            self.preview()
        self.f.cmd('branch', '--set-upstream-to=origin/target', 'target')
        self.f.cmd('config', 'remote.origin.push', 'HEAD:refs/heads/master')
        with self.assertRaisesRegex(GuardError, '推送引用'):
            self.preview()
        self.f.cmd('config', '--unset-all', 'remote.origin.push')
        self.f.cmd('config', 'remote.origin.mirror', 'true')
        with self.assertRaisesRegex(GuardError, 'mirror'):
            self.preview()
        self.f.cmd('config', '--unset', 'remote.origin.mirror')
        self.f.cmd('config', '--add', 'remote.origin.pushurl', '/fixture/first.git')
        self.f.cmd('config', '--add', 'remote.origin.pushurl', '/fixture/second.git')
        with self.assertRaisesRegex(GuardError, '远端地址'):
            self.preview()

    def test_hooks_run_only_after_confirmation_and_message_is_literal(self):
        pre_marker = Path(self.f.temp.name) / 'pre-commit-ran'
        message_marker = Path(self.f.temp.name) / 'commit-msg-ran'
        self.hook('pre-commit', 'touch "' + str(pre_marker) + '"')
        self.hook('commit-msg', 'touch "' + str(message_marker) + '"')
        self.write('common.txt', 'selected content\n')
        message = 'fix: $(touch shell-must-not-run)'
        plan = self.stage(['common.txt'], message)
        self.assertFalse(pre_marker.exists())
        self.assertFalse(message_marker.exists())
        result = self.repo.commit_merge(plan['plan'])
        self.assertTrue(pre_marker.exists())
        self.assertTrue(message_marker.exists())
        self.assertEqual(result['message'], message)
        self.assertFalse((self.f.path / 'shell-must-not-run').exists())
        self.assertEqual(self.f.cmd('rev-list', '--parents', '-n', '1', 'HEAD').split()[1:], [self.f.base])

    def test_hook_rejection_preserves_staged_content_without_retry(self):
        marker = Path(self.f.temp.name) / 'hook-ran'
        self.hook('commit-msg', 'touch "' + str(marker) + '"\necho "fixture hook rejected" >&2\nexit 1')
        self.write('common.txt', 'selected content\n')
        plan = self.stage(['common.txt'])
        index = self.raw('ls-files', '--stage', '-z')
        with self.assertRaisesRegex(GuardError, 'fixture hook rejected'):
            self.repo.commit_merge(plan['plan'])
        self.assertTrue(marker.exists())
        self.assertEqual(self.f.cmd('rev-parse', 'HEAD'), self.f.base)
        self.assertEqual(self.raw('ls-files', '--stage', '-z'), index)
        self.assertEqual(self.repo.snapshot(self.path)['operations'], [])
        with self.assertRaisesRegex(GuardError, '失效'):
            self.repo.commit_merge(plan['plan'])

    def test_disabled_hooks_prevent_ordinary_commit(self):
        self.write('common.txt', 'selected content\n')
        for variable in ('HUSKY', 'HUSKY_SKIP_HOOKS'):
            plan = self.stage(['common.txt'])
            with self.subTest(variable=variable), patch.dict(os.environ, {variable: '0' if variable == 'HUSKY' else '1'}):
                with self.assertRaisesRegex(GuardError, 'hooks'):
                    self.repo.commit_merge(plan['plan'])
            self.assertEqual(self.f.cmd('rev-parse', 'HEAD'), self.f.base)
            self.assertEqual(self.f.cmd('show', ':common.txt'), 'selected content')

    def test_hook_changes_to_committed_tree_are_reported_without_rollback(self):
        self.hook('pre-commit', 'printf "hook changed\\n" > common.txt\ngit add common.txt')
        self.write('common.txt', 'selected content\n')
        plan = self.stage(['common.txt'])
        with self.assertRaisesRegex(GuardError, '提交已创建'):
            self.repo.commit_merge(plan['plan'])
        self.assertNotEqual(self.f.cmd('rev-parse', 'HEAD'), self.f.base)
        self.assertEqual(self.f.cmd('show', 'HEAD:common.txt'), 'hook changed')
        self.assertEqual(self.f.cmd('rev-list', '--parents', '-n', '1', 'HEAD').split()[1:], [self.f.base])


class WorktreeCommitRequestChecks(unittest.TestCase):
    def test_routes_require_session_and_forward_only_reviewed_arguments(self):
        routes = [
            ('changes', {'path': '/fixture', 'unreviewed': True}, 'preview_changes', ('/fixture',), {}),
            ('stage', {'plan': 'reviewed', 'files': ['selected.txt'], 'message': 'fix: selected', 'all': True},
             'stage_changes', ('reviewed', ['selected.txt'], 'fix: selected'), {}),
            ('commit/preview', {'path': '/fixture', 'message': 'fix: selected', 'mode': 'worktree'},
             'preview_commit', ('/fixture', 'fix: selected'), {'mode': 'worktree'}),
            ('commit', {'plan': 'reviewed', 'path': '/unreviewed', 'message': 'ignored'},
             'commit_merge', ('reviewed',), {}),
        ]
        for route, data, method, args, kwargs in routes:
            for headers in ({}, {'X-Session-Token': 'wrong'}, {'X-Session-Token': 'test-token'},
                            {'X-Session-Token': 'test-token', 'Origin': 'https://evil.example'}):
                with self.subTest(route=route, headers=headers):
                    handler = test_server.RequestChecks().handler({'Host': '127.0.0.1:8123',
                        'Content-Type': 'application/json', **headers})
                    body = json.dumps(data).encode()
                    handler.headers['Content-Length'] = str(len(body))
                    handler.path = '/api/' + route
                    handler.rfile = io.BytesIO(body)
                    operation = Mock(return_value={'fixture': True})
                    handler.server.repo = SimpleNamespace(**{method: operation})
                    handler.reply = Mock()
                    handler.do_POST()
                    if headers != {'X-Session-Token': 'test-token'}:
                        operation.assert_not_called()
                        self.assertEqual(handler.reply.call_args.args[0], 400)
                    else:
                        operation.assert_called_once_with(*args, **kwargs)
                        handler.reply.assert_called_once_with(200, {'fixture': True})


if __name__ == '__main__':
    unittest.main()
