"""Persistent metadata checks; all Git writes use disposable local fixtures."""
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from operation_history import OperationHistory
from server import GuardError
import test_server
import test_batch_merge
import test_batch_push


class HistoryChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name) / 'state' / 'operations.sqlite3'
        self.history = OperationHistory(self.file, '/fixture/common')
        self.state = dict(path='/fixture/worktree', branch='target', head='a' * 40)

    def test_persistence_repository_isolation_and_interrupted_operation(self):
        identifier = self.history.start('merge', self.state, 'refs/heads/source', 'b' * 40)
        self.assertEqual(self.history.read()['items'][0]['status'], 'running')
        reloaded = OperationHistory(self.file, '/fixture/common')
        row = reloaded.read()['items'][0]
        self.assertEqual((row['id'], row['status']), (identifier, 'interrupted'))
        self.assertEqual(row['sha'], '')
        self.assertEqual(OperationHistory(self.file, '/other/common').read()['items'], [])
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)

    def test_retains_latest_200_per_repository(self):
        other = OperationHistory(self.file, '/other/common')
        other.start('push', self.state)
        for i in range(202):
            entry = self.history.start('commit', {**self.state, 'head': str(i)})
            self.history.finish(entry, 'succeeded', str(i))
        rows = self.history.read()['items']
        self.assertEqual(len(rows), 200)
        self.assertEqual((rows[0]['sha'], rows[-1]['sha']), ('201', '2'))
        self.assertEqual(len(other.read()['items']), 1)

    def test_corrupt_or_unwritable_storage_is_preserved_and_warned(self):
        corrupt = Path(self.temp.name) / 'corrupt.sqlite3'
        corrupt.write_bytes(b'keep corrupt file')
        history = OperationHistory(corrupt, '/fixture/common')
        self.assertTrue(history.read()['warning'])
        self.assertEqual(corrupt.read_bytes(), b'keep corrupt file')
        with patch.object(self.history, 'connect', side_effect=sqlite3.OperationalError('private-secret')):
            self.assertIsNone(self.history.start('push', self.state))
        self.assertTrue(self.history.read()['warning'])
        self.assertNotIn('private-secret', self.history.read()['warning'])


class GitHistoryChecks(unittest.TestCase):
    def setUp(self):
        self.fixture = test_server.MergeChecks('test_fast_forward_stays_uncommitted_and_preserves_refs')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.repo, self.path = self.fixture.repo, self.fixture.target
        self.file = Path(self.fixture.temp.name) / 'history.sqlite3'
        self.repo.history = OperationHistory(self.file, self.repo.common)

    def test_merge_commit_push_survive_restart_without_output_or_credentials(self):
        f = self.fixture
        f.local_origin()
        f.cmd('fetch', 'origin')
        f.cmd('branch', '--set-upstream-to=origin/target', 'target')
        self.repo.merge(f.plan()['plan'])
        commit = self.repo.preview_commit(self.path, 'merge: confidential-message-marker')
        committed = self.repo.commit_merge(commit['plan'])
        self.repo.push(self.repo.preview_push(self.path)['plan'])
        rows = OperationHistory(self.file, self.repo.common).read()['items']
        self.assertEqual([row['operation'] for row in rows], ['push', 'commit', 'merge'])
        self.assertEqual([row['status'] for row in rows], ['succeeded', 'succeeded', 'pending'])
        self.assertEqual(rows[0]['sha'], committed['sha'])
        self.assertEqual(rows[2]['source'], 'refs/heads/incoming')
        self.assertNotIn(b'confidential-message-marker', self.file.read_bytes())
        self.assertNotIn('output', rows[0])
        self.assertNotIn('url', rows[0])
        with self.repo.history.connect() as db:
            # Older records have no commit source; released worktrees must remain readable.
            db.execute("UPDATE operations SET path='/released/worktree'")
            db.execute("UPDATE operations SET source='', source_sha='' WHERE operation='commit'")
        enriched = self.repo.read_history()['items']
        self.assertEqual(enriched[0]['message'], 'merge: confidential-message-marker')
        self.assertEqual(enriched[1]['message'], enriched[0]['message'])
        self.assertEqual(enriched[1]['source'], 'refs/heads/incoming')
        self.assertEqual(enriched[0]['source'], '')
        self.assertNotIn(b'confidential-message-marker', self.file.read_bytes())
        with self.repo.history.connect() as db:
            db.execute("UPDATE operations SET source_sha=? WHERE operation='merge'", ('c' * 40,))
        self.assertEqual(self.repo.read_history()['items'][1]['source'], '')

    def test_history_reads_actual_hook_message_and_preserves_rows_when_details_unavailable(self):
        self.repo.merge(self.fixture.plan()['plan'])
        hooks = Path(self.fixture.temp.name) / 'message-hooks'
        hooks.mkdir()
        hook = hooks / 'commit-msg'
        hook.write_text('#!/bin/sh\nprintf "merge: hook adjusted title\\n\\nActual body\\n" > "$1"\n')
        hook.chmod(0o755)
        self.fixture.cmd('config', 'core.hooksPath', str(hooks))
        self.repo.commit_merge(self.repo.preview_commit(self.path, 'merge: original title')['plan'])
        row = self.repo.read_history()['items'][0]
        self.assertEqual(row['message'], 'merge: hook adjusted title\n\nActual body')
        self.assertNotIn(b'Actual body', self.file.read_bytes())
        with patch('server.git', side_effect=GuardError('private Git error')):
            unavailable = self.repo.read_history()['items'][0]
        self.assertEqual((unavailable['id'], unavailable['status']), (row['id'], 'succeeded'))
        self.assertIn('无法读取', unavailable['message_warning'])
        self.assertNotIn('private Git error', str(unavailable))
        with self.repo.history.connect() as db:
            db.execute("UPDATE operations SET sha=? WHERE operation='commit'", ('f' * 40,))
        missing = self.repo.read_history()['items'][0]
        self.assertEqual(missing['status'], 'succeeded')
        self.assertIn('无法读取', missing['message_warning'])

    def test_failed_validation_is_unconfirmed_and_invalid_plan_never_replays(self):
        plan = self.fixture.plan()
        self.fixture.cmd('config', 'alias.unrelated', 'status')
        (Path(self.path) / 'common.txt').write_text('preserve edits')
        with self.assertRaises(GuardError):
            self.repo.merge(plan['plan'])
        rows = self.repo.history.read()['items']
        self.assertEqual(rows[0]['status'], 'unconfirmed')
        with self.assertRaises(GuardError):
            self.repo.merge(plan['plan'])
        self.assertEqual(len(self.repo.history.read()['items']), 1)
        self.assertEqual(self.fixture.cmd('rev-parse', 'HEAD'), self.fixture.base)

    def test_journal_failure_does_not_turn_successful_git_write_into_retry(self):
        plan = self.fixture.plan()
        with patch.object(self.repo.history, 'connect', side_effect=sqlite3.OperationalError('disk full')):
            result = self.repo.merge(plan['plan'])
        self.assertEqual(result['kind'], 'pending')
        self.assertTrue(result['history_warning'])
        self.assertEqual(self.repo.snapshot(self.path)['operations'], ['MERGE_HEAD'])
        self.assertEqual(self.fixture.cmd('rev-parse', 'HEAD'), self.fixture.base)

    def test_finish_failure_keeps_git_success_and_started_record(self):
        plan = self.fixture.plan()
        original = self.repo.history.finish
        def unavailable(*args):
            with patch.object(self.repo.history, 'connect', side_effect=sqlite3.OperationalError('disk full')):
                original(*args)
        with patch.object(self.repo.history, 'finish', side_effect=unavailable):
            result = self.repo.merge(plan['plan'])
        self.assertEqual(result['kind'], 'pending')
        self.assertTrue(result['history_warning'])
        rows = OperationHistory(self.file, self.repo.common).read()['items']
        self.assertEqual(rows[0]['status'], 'interrupted')

    def test_commit_written_before_hook_validation_error_is_not_called_rollback(self):
        self.repo.merge(self.fixture.plan()['plan'])
        hooks = Path(self.fixture.temp.name) / 'hooks'
        hooks.mkdir()
        hook = hooks / 'pre-commit'
        hook.write_text('#!/bin/sh\nprintf "hook changed\\n" > incoming.txt\ngit add incoming.txt\n')
        hook.chmod(0o755)
        self.fixture.cmd('config', 'core.hooksPath', str(hooks))
        with self.assertRaisesRegex(GuardError, '提交已创建'):
            self.repo.commit_merge(self.repo.preview_commit(self.path)['plan'])
        row = self.repo.history.read()['items'][0]
        self.assertEqual(row['status'], 'unconfirmed')
        self.assertNotEqual(self.fixture.cmd('rev-parse', 'HEAD'), self.fixture.base)


class BatchHistoryChecks(unittest.TestCase):
    def test_batch_worker_uses_same_durable_merge_commit_history(self):
        f = test_batch_merge.BatchChecks('test_all_in_preserves_order_and_creates_local_merge_commits')
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.repo.history = OperationHistory(Path(f.fixture.temp.name) / 'history.sqlite3', f.repo.common)
        result = f.finish(f.preview())
        self.assertEqual(result['status'], 'succeeded')
        rows = f.repo.history.read()['items']
        self.assertEqual([r['operation'] for r in rows], ['commit', 'merge', 'commit', 'merge'])
        self.assertEqual([r['status'] for r in rows], ['succeeded', 'pending', 'succeeded', 'pending'])

    def test_batch_worker_records_push_without_browser(self):
        f = test_batch_push.BatchPushChecks('test_all_in_deduplicates_final_target_and_push_requires_confirmation')
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.repo.history = OperationHistory(Path(f.f.temp.name) / 'history.sqlite3', f.repo.common)
        f.completed()
        result = f.finish(f.batch.preview_push(f.batch.job['id']))
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(f.repo.history.read()['items'][0]['operation'], 'push')


class HistoryRequestChecks(unittest.TestCase):
    def test_errors_keep_original_shape_unless_storage_warning_exists(self):
        for warning in ('', 'storage unavailable'):
            handler = test_server.RequestChecks().handler({})
            handler.server.repo = SimpleNamespace(history=SimpleNamespace(warning=warning))
            handler.send_response = Mock()
            handler.send_header = Mock()
            handler.end_headers = Mock()
            handler.wfile = io.BytesIO()
            handler.reply(400, {'error': 'original failure'})
            expected = {'error': 'original failure'}
            if warning:
                expected['history_warning'] = warning
            self.assertEqual(json.loads(handler.wfile.getvalue()), expected)

    def test_history_is_authenticated_and_readable_during_batch(self):
        for token in ('wrong', 'test-token'):
            handler = test_server.RequestChecks().handler({'Host': '127.0.0.1:8123',
                'X-Session-Token': token, 'Content-Type': 'application/json', 'Content-Length': '2'})
            history = SimpleNamespace(warning='')
            read_history = Mock(return_value=dict(items=[], warning=''))
            batch = SimpleNamespace(guard=Mock(side_effect=ValueError('running')))
            handler.server.repo = SimpleNamespace(history=history, read_history=read_history, batch=batch)
            handler.path = '/api/history'
            handler.rfile = io.BytesIO(b'{}')
            handler.reply = Mock()
            handler.do_POST()
            batch.guard.assert_not_called()
            if token == 'wrong':
                read_history.assert_not_called()
                self.assertEqual(handler.reply.call_args.args[0], 400)
            else:
                read_history.assert_called_once_with()
                handler.reply.assert_called_once_with(200, dict(items=[], warning=''))


if __name__ == '__main__':
    unittest.main()
