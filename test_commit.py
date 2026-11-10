"""Merge commits use isolated Git fixtures; never commit in the user's repository."""
from pathlib import Path
import io
import json
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from server import GIT, GuardError, Repository, commit_message
from operation_history import OperationHistory
import test_server


class MessageChecks(unittest.TestCase):
    def test_long_branch_number_uses_the_same_identifier_as_prepare_commit_hook(self):
        branch = 'feat/M-20260929152031/bimgnt-migration/20260929'
        message = 'merge: M-2026092915 合并代码'
        self.assertEqual(commit_message(message, branch), message)
        with self.assertRaisesRegex(GuardError, 'M-2026092915'):
            commit_message('merge: 合并代码', branch)
        with self.assertRaisesRegex(GuardError, '多个需求编号'):
            commit_message(message, branch + '-M-12345678901234')

    def test_personal_commit_rules(self):
        self.assertEqual(commit_message('fix: 项目说明', 'M-1234567890-branch', False), 'fix: 项目说明')
        self.assertEqual(commit_message('merge: 合并代码', 'test'), 'merge: 合并代码')
        self.assertEqual(commit_message('fix: M-1234567890 描述\n\n正文', 'M-1234567890-需求'), 'fix: M-1234567890 描述\n\n正文')
        screenshot_message = 'fix: M-7139236919 【数据看板】V2.2.1新增环形堆叠柱状报表&周期对比&子报表筛选逻辑'
        screenshot_branch = 'M-7139236919-【数据看板】V2.2.1新增环形堆叠柱状报表&周期对比&子报表筛选逻辑'
        self.assertEqual(commit_message(screenshot_message, screenshot_branch), screenshot_message)
        for message, branch in [('', 'test'), ('not conventional', 'test'), ('merge!: 改动', 'test'),
                ('fix: 结尾.', 'test'), ('fix: ' + '长' * 104, 'test'),
                ('fix: 描述\n缺少空行', 'test'), ('fix: 描述\n\n' + '长' * 101, 'test'),
                ('fix: 无编号', 'M-1234567890-需求'),
                ('fix: M-1234567890', 'M-1234567890-需求'),
                ('fix: M-1234567890 M-1234567890 重复', 'M-1234567890-需求'),
                ('fix: M-1234567890 描述', 'M-1234567890-M-0987654321')]:
            with self.subTest(message=message), self.assertRaises(GuardError):
                commit_message(message, branch)
        self.assertEqual(len(commit_message('fix: ' + '长' * 103, 'test')), 108)


class RequestChecks(unittest.TestCase):
    def test_commit_routes_require_session_and_forward_only_reviewed_arguments(self):
        for route, data, method, args in [
                ('commit/preview', {'path': '/fixture', 'message': 'merge: 描述'}, 'preview_commit', ('/fixture', 'merge: 描述')),
                ('commit', {'plan': 'single-use'}, 'commit_merge', ('single-use',))]:
            for token in ('wrong', 'test-token'):
                handler = test_server.RequestChecks().handler({'Host': '127.0.0.1:8123',
                    'X-Session-Token': token, 'Content-Type': 'application/json'})
                body = json.dumps(data).encode()
                handler.headers['Content-Length'] = str(len(body))
                handler.path = '/api/' + route
                handler.rfile = io.BytesIO(body)
                operation = Mock(return_value={'fixture': True})
                handler.server.repo = SimpleNamespace(**{method: operation})
                handler.reply = Mock()
                handler.do_POST()
                if token == 'wrong':
                    operation.assert_not_called()
                    self.assertEqual(handler.reply.call_args.args[0], 400)
                else:
                    operation.assert_called_once_with(*args)
                    handler.reply.assert_called_once_with(200, {'fixture': True})


class CommitChecks(unittest.TestCase):
    def setUp(self):
        self.fixture = test_server.MergeChecks('test_fast_forward_stays_uncommitted_and_preserves_refs')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.repo, self.path = self.fixture.repo, self.fixture.target

    def pending(self):
        self.assertEqual(self.repo.merge(self.fixture.plan()['plan'])['kind'], 'pending')

    def hook(self, name, content):
        directory = Path(self.fixture.temp.name) / 'hooks'
        directory.mkdir(exist_ok=True)
        file = directory / name
        file.write_text('#!/bin/sh\n' + content + '\n')
        file.chmod(0o755)
        self.fixture.cmd('config', 'core.hooksPath', str(directory))

    def test_commit_only_reviewed_index_no_push_and_single_use(self):
        remote = self.fixture.local_origin()
        self.pending()
        (self.fixture.path / 'keep-untracked.txt').write_text('keep')
        remote_before = subprocess.check_output([GIT, '--git-dir', str(remote), 'show-ref', '--heads'])
        plan = self.repo.preview_commit(self.path)
        self.assertIn('incoming.txt', plan['files'])
        self.assertNotIn('keep-untracked.txt', plan['files'])
        result = self.repo.commit_merge(plan['plan'])
        self.assertEqual(result['message'].splitlines()[0], 'merge: 合并 incoming → target')
        self.assertIn('本次新增提交（不含合并提交）：1 条', result['message'])
        self.assertIn('incoming change', result['message'])
        self.assertIn('来源提交：' + self.fixture.incoming, result['message'])
        self.assertEqual(self.fixture.cmd('rev-list', '--parents', '-n', '1', 'HEAD').split()[1:], [self.fixture.base, self.fixture.incoming])
        self.assertEqual(result['state']['operations'], [])
        self.assertEqual(self.fixture.cmd('ls-tree', '--name-only', 'HEAD', '--', 'keep-untracked.txt'), '')
        self.assertEqual((self.fixture.path / 'keep-untracked.txt').read_text(), 'keep')
        self.assertEqual(subprocess.check_output([GIT, '--git-dir', str(remote), 'show-ref', '--heads']), remote_before)
        with self.assertRaisesRegex(GuardError, '失效'):
            self.repo.commit_merge(plan['plan'])
        self.assertEqual(self.fixture.cmd('rev-parse', 'HEAD'), result['sha'])

    def test_pending_merge_survives_tool_restart(self):
        history_path = Path(self.fixture.temp.name) / 'history.sqlite3'
        self.repo.history = OperationHistory(history_path, self.repo.common)
        self.pending()
        self.fixture.cmd('branch', '-D', 'incoming')
        self.repo = Repository(self.path)
        self.repo.history = OperationHistory(history_path, self.repo.common)
        self.addCleanup(self.repo.resolver.close)
        plan = self.repo.preview_commit(self.path)
        self.assertEqual(plan['message'].splitlines()[0], 'merge: 合并 incoming → target')
        self.assertIn('incoming change', plan['message'])
        self.assertEqual(self.repo.commit_merge(plan['plan'])['state']['operations'], [])

    def test_long_branch_identifier_prevents_prepare_hook_from_prefixing_message(self):
        self.repo.bibase = True
        self.fixture.cmd('branch', '-m', 'feat/M-20260929152031/bimgnt-migration/20260929')
        self.pending()
        # The real hook excludes merge from its conventional types and otherwise prefixes M-ID.
        self.hook('prepare-commit-msg', r'''message=$(cat "$1")
if ! printf '%s\n' "$message" | grep -qE '^(feat|fix|docs|style|refactor|perf|test|chore|revert|build)(\([^)]*\))?: '; then
  identifier=$(git symbolic-ref --short HEAD | grep -oE 'M-[0-9]{10}')
  if ! head -n 1 "$1" | grep -qE 'M-[0-9]{10}'; then
    printf '%s %s\n' "$identifier" "$message" > "$1"
  fi
fi''')
        self.hook('commit-msg', r'''head -n 1 "$1" | grep -qE '^merge: M-2026092915 .+' || exit 1''')
        plan = self.repo.preview_commit(self.path)
        self.assertTrue(plan['message'].startswith('merge: M-2026092915 合并 incoming → '))
        self.assertEqual(plan['message'].splitlines()[0].count('M-2026092915'), 1)
        result = self.repo.commit_merge(plan['plan'])
        self.assertEqual(result['message'], plan['message'])
        self.assertEqual(result['state']['operations'], [])

    def test_merge_description_bounds_and_ambiguous_source_do_not_guess_a_branch(self):
        self.fixture.cmd('branch', 'alias', 'incoming')
        self.pending()
        message = self.repo.preview_commit(self.path)['message']
        self.assertIn('合并 ' + self.fixture.incoming[:12] + ' → target', message)
        self.assertNotIn('来源分支：alias', message)
        self.fixture.cmd('branch', '-D', 'alias')
        long_source = 'M-1234567890-' + 'requirement-' * 18
        self.fixture.cmd('branch', '-m', 'incoming', long_source)
        message = self.repo.preview_commit(self.path)['message']
        self.assertLessEqual(len(message.splitlines()[0]), 108)
        self.assertTrue(all(len(line) <= 100 for line in message.splitlines()[1:]))
        self.assertIn(long_source, message.replace('\n', ''))
        self.assertEqual(commit_message(message, 'target'), message)

    def test_changed_index_after_preview_is_rejected(self):
        self.pending()
        plan = self.repo.preview_commit(self.path)
        (self.fixture.path / 'incoming.txt').write_text('different staged contents')
        self.fixture.cmd('add', 'incoming.txt')
        with self.assertRaisesRegex(GuardError, '变化'):
            self.repo.commit_merge(plan['plan'])
        self.assertEqual(self.fixture.cmd('rev-parse', 'HEAD'), self.fixture.base)
        self.assertTrue(self.repo.snapshot(self.path)['operations'])

    def test_unstaged_changes_are_not_automatically_added(self):
        self.pending()
        (self.fixture.path / 'incoming.txt').write_text('unstaged changes')
        with self.assertRaisesRegex(GuardError, '未暂存'):
            self.repo.preview_commit(self.path)
        self.assertEqual(self.fixture.cmd('show', ':incoming.txt'), 'incoming')

    def test_without_pending_merge_is_rejected(self):
        with self.assertRaisesRegex(GuardError, '待提交合并'):
            self.repo.preview_commit(self.path)

    def test_master_pending_merge_can_be_committed_locally(self):
        self.fixture.cmd('checkout', '-b', 'master')
        self.fixture.cmd('merge', '--no-commit', '--no-ff', 'incoming')
        plan = self.repo.preview_commit(self.path)
        self.assertEqual(plan['branch'], 'master')
        result = self.repo.commit_merge(plan['plan'])
        self.assertEqual(result['state']['branch'], 'master')
        self.assertEqual(result['state']['operations'], [])
        self.assertEqual(self.fixture.cmd('rev-list', '--parents', '-n', '1', 'HEAD').split()[1:],
                         [self.fixture.base, self.fixture.incoming])
        with self.assertRaisesRegex(GuardError, '禁止推送 master'):
            self.repo.preview_push(self.path)

    def test_unresolved_conflict_is_rejected(self):
        f = self.fixture
        f.cmd('checkout', 'incoming');(f.path / 'common.txt').write_text('source\n');f.commit('source')
        f.cmd('checkout', 'target');(f.path / 'common.txt').write_text('target\n');f.commit('target')
        self.assertEqual(self.repo.merge(f.plan()['plan'])['kind'], 'conflicts')
        with self.assertRaisesRegex(GuardError, '冲突'):
            self.repo.preview_commit(self.path)

    def test_expired_preview_and_changed_configuration_are_rejected(self):
        self.pending()
        plan = self.repo.preview_commit(self.path)
        self.repo.commit_plans[plan['plan']]['time'] -= 601
        with self.assertRaisesRegex(GuardError, '失效'):
            self.repo.commit_merge(plan['plan'])
        plan = self.repo.preview_commit(self.path)
        self.fixture.cmd('config', 'user.name', 'changed')
        with self.assertRaisesRegex(GuardError, '变化'):
            self.repo.commit_merge(plan['plan'])

    def test_wrong_upstream_and_push_mapping_are_rejected(self):
        self.fixture.local_origin();self.fixture.cmd('fetch', 'origin');self.pending()
        self.fixture.cmd('branch', '--set-upstream-to=origin/incoming', 'target')
        with self.assertRaisesRegex(GuardError, '同名'):
            self.repo.preview_commit(self.path)
        self.fixture.cmd('branch', '--set-upstream-to=origin/target', 'target')
        self.fixture.cmd('config', 'remote.origin.push', 'HEAD:refs/heads/master')
        with self.assertRaisesRegex(GuardError, '推送引用'):
            self.repo.preview_commit(self.path)

    def test_hooks_are_run_and_rejection_preserves_pending_merge(self):
        self.pending()
        marker = Path(self.fixture.temp.name) / 'hook-ran'
        self.hook('commit-msg', 'touch "' + str(marker) + '"\necho "fixture hook rejected" >&2\nexit 1')
        plan = self.repo.preview_commit(self.path)
        self.assertFalse(marker.exists())
        with self.assertRaisesRegex(GuardError, 'fixture hook rejected'):
            self.repo.commit_merge(plan['plan'])
        self.assertTrue(marker.exists())
        self.assertEqual(self.fixture.cmd('rev-parse', 'HEAD'), self.fixture.base)
        self.assertTrue(self.repo.snapshot(self.path)['operations'])

    def test_disabled_hooks_are_rejected(self):
        self.pending()
        plan = self.repo.preview_commit(self.path)
        with patch.dict('os.environ', {'HUSKY': '0'}), self.assertRaisesRegex(GuardError, 'hooks'):
            self.repo.commit_merge(plan['plan'])
        self.assertEqual(self.fixture.cmd('rev-parse', 'HEAD'), self.fixture.base)

    def test_hook_changes_to_committed_tree_are_reported_without_rollback(self):
        self.pending()
        self.hook('pre-commit', 'printf "hook changed\\n" > incoming.txt\ngit add incoming.txt')
        plan = self.repo.preview_commit(self.path)
        with self.assertRaisesRegex(GuardError, '提交已创建'):
            self.repo.commit_merge(plan['plan'])
        self.assertNotEqual(self.fixture.cmd('rev-parse', 'HEAD'), self.fixture.base)
        self.assertEqual(self.fixture.cmd('show', 'HEAD:incoming.txt'), 'hook changed')

    def test_hook_invalid_message_is_reported_without_amend(self):
        self.pending()
        self.hook('prepare-commit-msg', 'printf "invalid message\\n" > "$1"')
        plan = self.repo.preview_commit(self.path)
        with self.assertRaisesRegex(GuardError, '提交已创建'):
            self.repo.commit_merge(plan['plan'])
        self.assertEqual(self.fixture.cmd('log', '-1', '--format=%B'), 'invalid message')

    def test_commit_message_is_passed_as_literal_text(self):
        self.pending()
        message = 'merge: $(touch not-executed)'
        result = self.repo.commit_merge(self.repo.preview_commit(self.path, message)['plan'])
        self.assertEqual(result['message'], message)
        self.assertFalse((self.fixture.path / 'not-executed').exists())


if __name__ == '__main__':
    unittest.main()
