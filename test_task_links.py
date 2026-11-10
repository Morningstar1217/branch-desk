"""Task links and history migration use local metadata, fake CLI and temporary Git only."""
import io
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from development import DevelopmentLauncher
from operation_history import OperationHistory, failure_detail
import test_server
import test_development
import test_conflict_resolver


class MetadataChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name) / 'state.sqlite3'
        self.history = OperationHistory(self.file, '/fixture/common')

    def test_legacy_database_upgrades_without_losing_rows(self):
        old = Path(self.temp.name) / 'old.sqlite3'
        with closing(sqlite3.connect(old)) as db, db:
            db.execute('CREATE TABLE operations (id TEXT PRIMARY KEY, repository TEXT, operation TEXT, started REAL, '
                       'finished REAL, path TEXT, branch TEXT, source TEXT, source_sha TEXT, before_sha TEXT, sha TEXT, status TEXT)')
            db.execute('INSERT INTO operations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       ('old', self.history.repository, 'merge', 1, 2, '/wt', 'topic', 'refs/heads/source', 'b', 'a', 'a', 'pending'))
        migrated = OperationHistory(old, '/fixture/common')
        rows = migrated.read()['items']
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['id'], rows[0]['detail'], rows[0]['thread_url']), ('old', '', ''))
        entry = migrated.start('push', dict(path='/wt', branch='topic', head='b'))
        migrated.finish(entry, 'succeeded', 'b')
        self.assertEqual(len(migrated.read()['items']), 2)

    def test_task_compare_and_save_preserves_session_and_repository_isolation(self):
        self.history.remember_development(dict(work_item_id='123', status='finished', url='codex://threads/fixture'))
        self.history.bind_task('123', 'refs/heads/M-123-topic', '')
        with self.assertRaisesRegex(ValueError, '关联已变化'):
            self.history.bind_task('123', 'refs/heads/other', '')
        reloaded = OperationHistory(self.file, '/fixture/common')
        self.assertEqual(reloaded.tasks()['123']['branch_ref'], 'refs/heads/M-123-topic')
        self.assertEqual(reloaded.tasks()['123']['thread_url'], 'codex://threads/fixture')
        reloaded.bind_task('123', '', 'refs/heads/M-123-topic')
        self.assertEqual(reloaded.tasks()['123']['thread_url'], 'codex://threads/fixture')
        self.assertEqual(OperationHistory(self.file, '/different').tasks(), {})

    def test_same_task_id_stays_separate_across_feishu_spaces(self):
        self.history.remember_development(dict(work_item_id='123', status='finished', url='codex://threads/fixture'))
        self.history.bind_task('123', 'refs/heads/M-123-topic', '')
        self.assertEqual(self.history.tasks('other-space'), {})
        self.history.bind_task('123', 'refs/heads/other-topic', '', 'other-space')
        self.history.remember_development(dict(work_item_id='123', status='finished', url='codex://threads/other'), 'other-space')
        reloaded = OperationHistory(self.file, '/fixture/common')
        self.assertEqual(reloaded.tasks()['123']['branch_ref'], 'refs/heads/M-123-topic')
        self.assertEqual(reloaded.tasks('other-space')['123']['branch_ref'], 'refs/heads/other-topic')
        self.assertEqual(reloaded.tasks('other-space')['123']['thread_url'], 'codex://threads/other')

    def test_legacy_task_links_migrate_only_to_original_space(self):
        legacy = Path(self.temp.name) / 'legacy-tasks.sqlite3'
        with closing(sqlite3.connect(legacy)) as db, db:
            db.execute('''CREATE TABLE task_links (repository TEXT, task_id TEXT, branch_ref TEXT NOT NULL DEFAULT '',
                thread_url TEXT NOT NULL DEFAULT '', previous_url TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (repository, task_id))''')
            db.execute('INSERT INTO task_links VALUES (?, ?, ?, ?, ?, ?)',
                       (self.history.repository, '123', 'refs/heads/old', 'codex://threads/old', '', 'finished'))
        migrated = OperationHistory(legacy, '/fixture/common')
        self.assertEqual(migrated.tasks()['123']['branch_ref'], 'refs/heads/old')
        self.assertEqual(migrated.tasks('new-space'), {})
        migrated.bind_task('123', 'refs/heads/new', '', 'new-space')
        self.assertEqual(migrated.tasks()['123']['branch_ref'], 'refs/heads/old')

    def test_url_validation_and_controlled_diagnostics_exclude_raw_secrets(self):
        for message, expected in [('pre-commit hook rejected token=secret-marker', 'hooks'),
                ('remote https://user:secret-marker@host/x timed out', '超时'),
                ('unknown source secret-marker', '未完成'), ('提交已创建 secret-marker', '提交已经创建')]:
            detail = failure_detail(message)
            self.assertIn(expected, detail)
            self.assertNotIn('secret-marker', detail)
        self.history.remember_development(dict(work_item_id='123', status='finished', url='javascript:secret-marker'))
        self.assertEqual(self.history.tasks()['123']['thread_url'], '')
        self.assertNotIn(b'secret-marker', self.file.read_bytes())

    def test_resolution_link_and_status_survive_restart(self):
        entry = self.history.start('merge', dict(path='/wt', branch='topic', head='a'))
        self.history.finish(entry, 'conflicts', 'a')
        self.history.resolution(entry, dict(status='running', url='codex://threads/fixture'))
        reloaded = OperationHistory(self.file, '/fixture/common')
        row = reloaded.read()['items'][0]
        self.assertEqual((row['status'], row['resolution_status'], row['thread_url']),
                         ('conflicts', 'interrupted', 'codex://threads/fixture'))
        reloaded.resolution(entry, dict(status='failed', summary='timeout secret-marker', url='https://unsafe.invalid'))
        row = reloaded.read()['items'][0]
        self.assertEqual(row['thread_url'], '')
        self.assertIn('超时', row['resolution_detail'])
        self.assertNotIn('secret-marker', row['resolution_detail'])


class TaskRequestChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.history = OperationHistory(Path(self.temp.name) / 'history.sqlite3', '/repo')
        self.repo = SimpleNamespace(history=self.history, branches=Mock(return_value=[
            dict(ref='refs/heads/topic', branch='topic'), dict(ref='refs/heads/master', branch='master'),
            dict(ref='refs/remotes/origin/topic', branch='topic')]))

    def request(self, data, token='test-token', route='task-links/bind'):
        body = json.dumps(data).encode()
        handler = test_server.RequestChecks().handler(dict(Host='127.0.0.1:8123',
            **{'X-Session-Token': token, 'Content-Type': 'application/json', 'Content-Length': str(len(body))}))
        handler.server.repo = self.repo
        handler.server.schedule = SimpleNamespace(space_id='62983005bc43adc6923d707d', ensure_enabled=lambda: None)
        handler.path = '/api/' + route
        handler.rfile = io.BytesIO(body)
        handler.reply = Mock()
        handler.do_POST()
        return handler.reply.call_args.args

    def test_explicit_confirm_local_ref_and_stale_update_guards(self):
        data = dict(work_item_id='123', ref='refs/heads/topic', expected_ref='', confirmed=True)
        for change in (dict(confirmed=False), dict(ref='refs/heads/master'), dict(ref='refs/remotes/origin/topic'),
                       dict(ref='refs/heads/missing'), dict(work_item_id='../bad')):
            self.assertEqual(self.request({**data, **change})[0], 400)
        self.assertEqual(self.request(data, token='wrong')[0], 400)
        self.assertEqual(self.history.tasks(), {})
        self.assertEqual(self.request(data)[0], 200)
        self.assertEqual(self.request({**data, 'ref': ''})[0], 400)
        self.assertEqual(self.request({**data, 'ref': '', 'expected_ref': 'refs/heads/topic'})[0], 200)
        self.assertEqual(self.history.tasks()['123']['branch_ref'], '')

    def test_read_remains_available_during_batch_but_binding_is_blocked(self):
        guard = Mock(side_effect=ValueError('queue running'))
        self.repo.batch = SimpleNamespace(guard=guard)
        self.assertEqual(self.request({}, route='task-links')[0], 200)
        guard.assert_not_called()
        self.assertEqual(self.request(dict(work_item_id='123', ref='refs/heads/topic', expected_ref='', confirmed=True))[0], 400)


class LifecycleChecks(unittest.TestCase):
    def test_real_conflict_worker_persists_exact_session_and_final_outcome(self):
        f = test_conflict_resolver.ResolverChecks('test_automatic_handoff_uses_exact_worktree_and_stages_only_conflicts')
        f.setUp()
        self.addCleanup(f.doCleanups)
        file = Path(f.fixture.temp.name) / 'history.sqlite3'
        history = OperationHistory(file, f.fixture.repo.common)
        f.fixture.repo.history = history
        f.merge()
        f.finish()
        row = history.read()['items'][0]
        self.assertEqual((row['operation'], row['status'], row['resolution_status']), ('merge', 'conflicts', 'succeeded'))
        self.assertEqual(row['thread_url'], 'codex://threads/fixture-thread')
        self.assertEqual(OperationHistory(file, f.fixture.repo.common).read()['items'][0]['thread_url'], row['thread_url'])

    def test_development_reuses_persisted_session_without_starting_cli_again(self):
        f = test_development.DevelopmentTests('test_duplicate_start_reuses_thread_and_explicit_restart_is_guarded')
        f.setUp()
        self.addCleanup(f.doCleanups)
        file = f.root / 'history.sqlite3'
        history = OperationHistory(file, '/repo/common')
        f.repo.history = history
        f.launcher = DevelopmentLauncher(f.repo, f.schedule)
        f.launcher.start(dict(work_item_id='123'))
        first = f.finish()
        reloaded = DevelopmentLauncher(SimpleNamespace(**{**vars(f.repo), 'history': OperationHistory(file, '/repo/common')}), f.schedule)
        self.addCleanup(reloaded.close)
        self.assertEqual(reloaded.status()['123']['url'], first['url'])
        self.assertEqual(reloaded.start(dict(work_item_id='123'))['url'], first['url'])
        self.assertEqual(reloaded.jobs, {})
        reloaded.start(dict(work_item_id='123', restart_from=first['url']))
        reloaded.jobs['123']['worker'].join(timeout=4)
        self.assertEqual(reloaded.status()['123']['previous_url'], first['url'])


if __name__ == '__main__':
    unittest.main()
