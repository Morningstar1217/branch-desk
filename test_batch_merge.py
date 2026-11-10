"""Batch merges only modify temporary Git fixtures; no real CLI, browser or remote."""
import io
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_server
import test_conflict_resolver


class BatchChecks(unittest.TestCase):
    def setUp(self):
        self.fixture = test_server.MergeChecks('test_fast_forward_stays_uncommitted_and_preserves_refs')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.repo = self.fixture.repo
        self.batch = self.repo.batch
        self.addCleanup(self.stop)
        f = self.fixture
        (f.path / '.gitignore').write_text('*.secret\n.aidp/\n')
        f.commit('fixture ignore')
        self.head = f.cmd('rev-parse', 'HEAD')
        f.cmd('branch', 'pre', 'target')
        f.cmd('checkout', '-b', 'second', self.fixture.base)
        (f.path / 'second.txt').write_text('second\n')
        f.commit('second source')
        self.second = f.cmd('rev-parse', 'HEAD')
        f.cmd('checkout', 'target')

    def stop(self):
        self.batch.close()
        if self.batch.worker:
            self.batch.worker.join(timeout=30)

    def preview(self, sources=None, targets=None, mode='all-in'):
        return self.batch.preview(dict(mode=mode, sources=sources or ['refs/heads/incoming', 'refs/heads/second'],
                                       targets=targets or ['refs/heads/target']))

    def finish(self, plan):
        self.batch.start(plan['plan'], True)
        self.batch.worker.join(timeout=50)
        result = self.batch.status()
        self.assertNotEqual(result['status'], 'running', result)
        return result

    def test_all_in_preserves_order_and_creates_local_merge_commits(self):
        before = self.fixture.cmd('rev-parse', 'incoming')
        result = self.finish(self.preview())
        self.assertEqual(result['status'], 'succeeded', result)
        rows = result['rows']
        self.assertEqual([r['source'] for r in rows], ['refs/heads/incoming', 'refs/heads/second'])
        self.assertEqual([r['status'] for r in rows], ['succeeded', 'succeeded'])
        parents = self.fixture.cmd('rev-list', '--parents', '-n', '1', 'HEAD').split()
        self.assertEqual(parents[1:], [rows[0]['commit'], self.second])
        self.assertEqual(self.fixture.cmd('rev-list', '--parents', '-n', '1', rows[0]['commit']).split()[1:], [self.head, before])
        self.assertEqual(self.fixture.cmd('status', '--porcelain'), '')
        self.assertEqual(self.fixture.cmd('rev-parse', 'incoming'), before)
        self.assertTrue((self.fixture.path / 'incoming.txt').exists())
        self.assertTrue((self.fixture.path / 'second.txt').exists())
        first_message = self.fixture.cmd('log', '-1', '--format=%B', rows[0]['commit'])
        second_message = self.fixture.cmd('log', '-1', '--format=%B', rows[1]['commit'])
        self.assertTrue(first_message.startswith('merge: 合并 incoming → target\n'))
        self.assertIn('incoming change', first_message)
        self.assertTrue(second_message.startswith('merge: 合并 second → target\n'))
        self.assertIn('second source', second_message)
        self.assertNotIn('incoming change', second_message)

    def test_sync_origin_master_to_multiple_targets_without_changing_remote_or_master(self):
        remote = self.fixture.local_origin()
        self.fixture.cmd('update-ref', 'refs/remotes/origin/master', self.fixture.incoming)
        self.fixture.cmd('branch', 'master', self.fixture.base)
        remote_before = subprocess.check_output([test_server.GIT, '--git-dir', str(remote), 'show-ref'])
        plan = self.preview(['refs/remotes/origin/master'], ['refs/heads/target', 'refs/heads/pre'], 'sync')
        self.assertEqual(len(self.repo.worktrees()), 1, 'Preview must not create worktrees')
        result = self.finish(plan)
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual([r['target'] for r in result['rows']], ['target', 'pre'])
        self.assertTrue(Path(result['rows'][1]['path'], 'incoming.txt').is_file())
        self.assertEqual(self.fixture.cmd('rev-parse', 'master'), self.fixture.base)
        self.assertEqual(self.fixture.cmd('rev-parse', 'origin/master'), self.fixture.incoming)
        self.assertEqual(subprocess.check_output([test_server.GIT, '--git-dir', str(remote), 'show-ref']), remote_before)

    def test_dirty_target_rejects_whole_preview_before_any_writes(self):
        (self.fixture.path / 'common.txt').write_text('preserve local edit\n')
        with self.assertRaisesRegex(ValueError, '存在暂存'):
            self.preview(['refs/heads/incoming'], ['refs/heads/pre', 'refs/heads/target'], 'sync')
        self.assertEqual(len(self.repo.worktrees()), 1)
        self.assertEqual(self.fixture.cmd('rev-parse', 'HEAD'), self.head)

    def test_ref_or_target_change_expiry_and_missing_consent_do_not_start(self):
        plan = self.preview()
        for value in (False, None, 'true', 1):
            with self.assertRaisesRegex(ValueError, '确认允许'):
                self.batch.start(plan['plan'], value)
        self.fixture.cmd('update-ref', 'refs/heads/second', self.fixture.base)
        with self.assertRaisesRegex(ValueError, '已变化'):
            self.batch.start(plan['plan'], True)
        plan = self.preview()
        self.batch.plans[plan['plan']]['time'] -= 601
        with self.assertRaisesRegex(ValueError, '已失效'):
            self.batch.start(plan['plan'], True)
        plan = self.preview()
        (self.fixture.path / 'common.txt').write_text('keep\n')
        with self.assertRaises(ValueError):
            self.batch.start(plan['plan'], True)
        self.assertIsNone(self.batch.status())
        self.assertEqual(self.fixture.cmd('rev-parse', 'HEAD'), self.head)

    def test_hooks_failure_keeps_first_commit_and_second_merge_staged(self):
        hooks = Path(self.fixture.temp.name) / 'hooks'
        hooks.mkdir()
        marker = hooks / 'first-pass'
        hook = hooks / 'pre-commit'
        hook.write_text('#!/bin/sh\nif [ -f "' + str(marker) + '" ]; then echo fixture-hook-rejected; exit 1; fi\ntouch "' + str(marker) + '"\n')
        hook.chmod(0o755)
        self.fixture.cmd('config', 'core.hooksPath', str(hooks))
        result = self.finish(self.preview())
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual([r['status'] for r in result['rows']], ['succeeded', 'failed'])
        self.assertIn('fixture-hook-rejected', result['summary'])
        self.assertEqual(self.fixture.cmd('rev-parse', 'HEAD'), result['rows'][0]['commit'])
        self.assertEqual(self.fixture.cmd('show', ':second.txt'), 'second')
        self.assertEqual(self.repo.snapshot(self.fixture.target)['operations'], ['MERGE_HEAD'])
        resumed = self.batch.preview_resume(result['id'])
        self.assertTrue(resumed['message'].startswith('merge: 合并 second → target\n'))
        self.assertIn('second source', resumed['message'])
        self.assertNotIn('incoming change', resumed['message'])

    def assert_stale_merge_recovery_is_read_only(self, transition):
        with patch.object(self.repo, 'commit_merge', side_effect=ValueError('fixture commit rejected')):
            result = self.finish(self.preview())
        self.assertEqual(result['status'], 'failed', result)
        self.assertTrue(result['rows'][0]['recoverable'])
        reviewed = self.batch.preview_resume(result['id'])
        # External Trace2 integrations must not mutate notes during the read-only assertions.
        with patch.dict('os.environ', {'GIT_TRACE2_EVENT': '0'}):
            self.fixture.cmd(*transition)
        before = self.repo.snapshot(self.fixture.target)
        refs = self.fixture.cmd('show-ref')
        self.assertEqual(before['operations'], [])
        with patch.object(self.repo.resolver, 'identity', wraps=self.repo.resolver.identity) as identity, \
                patch.object(self.repo.resolver, 'start_repair') as repair:
            for action in (lambda: self.batch.preview_resume(result['id']),
                           lambda: self.batch.repair(result['id'], 0),
                           lambda: self.batch.resume(reviewed['plan'])):
                with self.subTest(action=action), self.assertRaisesRegex(ValueError, '不能继续旧批次'):
                    action()
            identity.assert_not_called()
            repair.assert_not_called()
        self.assertEqual(self.repo.snapshot(self.fixture.target), before)
        self.assertEqual(self.fixture.cmd('show-ref'), refs)
        self.assertEqual(self.batch.status()['rows'][1]['status'], 'pending')
        self.assertFalse((self.fixture.path / 'second.txt').exists())

    def test_completed_merge_rejects_stale_recovery_before_reading_merge_head(self):
        self.assert_stale_merge_recovery_is_read_only(('commit', '-m', 'complete pending merge'))
        self.assertEqual(self.fixture.cmd('log', '-1', '--format=%s'), 'complete pending merge')

    def test_aborted_merge_rejects_stale_recovery_before_reading_merge_head(self):
        self.assert_stale_merge_recovery_is_read_only(('merge', '--abort'))
        self.assertEqual(self.fixture.cmd('rev-parse', 'HEAD'), self.head)

    def test_cancel_stops_remaining_rows_without_undoing_completed_commit(self):
        commit = self.repo.commit_merge
        def commit_then_stop(identifier):
            result = commit(identifier)
            self.batch.cancel(self.batch.status()['id'])
            return result
        with patch.object(self.repo, 'commit_merge', side_effect=commit_then_stop):
            result = self.finish(self.preview())
        self.assertEqual(result['status'], 'stopped', result)
        self.assertEqual([r['status'] for r in result['rows']], ['succeeded', 'stopped'])
        self.assertEqual(self.fixture.cmd('rev-parse', 'HEAD'), result['rows'][0]['commit'])
        self.assertFalse((self.fixture.path / 'second.txt').exists())

    def test_changed_source_mid_batch_stops_without_merging_new_tip(self):
        commit = self.repo.commit_merge
        def commit_then_move(identifier):
            result = commit(identifier)
            self.fixture.cmd('update-ref', 'refs/heads/second', self.fixture.base)
            return result
        with patch.object(self.repo, 'commit_merge', side_effect=commit_then_move):
            result = self.finish(self.preview())
        self.assertEqual(result['status'], 'failed', result)
        self.assertIn('来源引用已变化', result['summary'])
        self.assertEqual(result['rows'][0]['status'], 'succeeded')
        self.assertFalse((self.fixture.path / 'second.txt').exists())

    def test_already_contained_skips_without_empty_commit(self):
        self.fixture.cmd('branch', 'contained', self.fixture.base)
        result = self.finish(self.preview(['refs/heads/contained']))
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(result['rows'][0]['status'], 'skipped')
        self.assertEqual(self.fixture.cmd('rev-parse', 'HEAD'), self.head)

    def test_external_staging_after_merge_is_preserved_and_not_committed(self):
        preview_commit = self.repo.preview_commit
        def stage_external_change(path):
            (self.fixture.path / 'foreign.txt').write_text('keep external work\n')
            self.fixture.cmd('add', 'foreign.txt')
            return preview_commit(path)
        with patch.object(self.repo, 'preview_commit', side_effect=stage_external_change):
            result = self.finish(self.preview())
        self.assertEqual(result['status'], 'failed', result)
        self.assertIn('合并后工作区状态已变化', result['summary'])
        self.assertEqual(self.fixture.cmd('rev-parse', 'HEAD'), self.head)
        self.assertEqual(self.fixture.cmd('show', ':foreign.txt'), 'keep external work')
        self.assertEqual(self.repo.snapshot(self.fixture.target)['operations'], ['MERGE_HEAD'])

    def test_target_moves_during_checkout_stops_before_merge(self):
        prepare = self.repo.prepare_target
        def commit_during_prepare(ref):
            (self.fixture.path / 'external.txt').write_text('preserve external commit\n')
            self.fixture.commit('external commit')
            return prepare(ref)
        with patch.object(self.repo, 'prepare_target', side_effect=commit_during_prepare):
            result = self.finish(self.preview())
        self.assertEqual(result['status'], 'failed', result)
        self.assertIn('准备工作区期间目标分支已变化', result['summary'])
        self.assertEqual(self.fixture.cmd('log', '-1', '--format=%s'), 'external commit')
        self.assertFalse((self.fixture.path / 'incoming.txt').exists())

    def test_master_batch_target_creates_local_commits_but_cannot_push(self):
        self.fixture.cmd('branch', 'master')
        plan = self.preview(targets=['refs/heads/master'])
        result = self.finish(plan)
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertTrue(all(row['status'] == 'succeeded' for row in result['rows']))
        path = result['rows'][0]['path']
        state = self.repo.snapshot(path)
        self.assertEqual(state['branch'], 'master')
        self.assertEqual(state['status'], '')
        self.assertEqual(self.fixture.cmd('rev-parse', 'target'), self.head)
        with self.assertRaisesRegex(test_server.GuardError, '禁止推送 master'):
            self.batch.preview_push(result['id'])

    def test_invalid_selections_rejected(self):
        for data in [dict(mode='bad', sources=['refs/heads/incoming'], targets=['refs/heads/target']),
                dict(mode='all-in', sources=['refs/heads/incoming'] * 2, targets=['refs/heads/target']),
                dict(mode='sync', sources=['refs/heads/incoming', 'refs/heads/second'], targets=['refs/heads/target']),
                dict(mode='sync', sources=['refs/heads/target'], targets=['refs/heads/target']),
                dict(mode='sync', sources=['not-a-ref'], targets=['refs/heads/target'])]:
            with self.subTest(data=data), self.assertRaises((ValueError, test_server.GuardError)):
                self.batch.preview(data)


class ConflictBatchChecks(unittest.TestCase):
    def setUp(self):
        self.fixture = test_conflict_resolver.ResolverChecks('test_automatic_handoff_uses_exact_worktree_and_stages_only_conflicts')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.batch = self.fixture.fixture.repo.batch
        self.addCleanup(self.stop)

    def stop(self):
        self.batch.close()
        if self.batch.worker:
            self.batch.worker.join(timeout=30)

    def test_real_resolver_fixture_finishes_before_batch_commit(self):
        self.fixture.mode_file.write_text('linger')
        plan = self.batch.preview(dict(mode='all-in', sources=['refs/heads/incoming'], targets=['refs/heads/target']))
        self.batch.start(plan['plan'], True)
        self.batch.worker.join(timeout=60)
        result = self.batch.status()
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(result['rows'][0]['resolution']['status'], 'succeeded')
        f = self.fixture.fixture
        self.assertEqual(f.cmd('show', 'HEAD:common.txt'), 'target content\nsource content')
        self.assertEqual(f.cmd('status', '--porcelain'), '')

    def test_failed_resolver_stops_without_commit_and_keeps_conflicts(self):
        self.fixture.mode_file.write_text('blocked')
        plan = self.batch.preview(dict(mode='all-in', sources=['refs/heads/incoming'], targets=['refs/heads/target']))
        self.batch.start(plan['plan'], True)
        self.batch.worker.join(timeout=60)
        result = self.batch.status()
        self.assertEqual(result['status'], 'failed', result)
        f = self.fixture.fixture
        self.assertEqual(f.cmd('rev-parse', 'HEAD'), self.fixture.head)
        self.assertTrue(f.cmd('ls-files', '-u'))


class RequestChecks(unittest.TestCase):
    def handler(self, route, data):
        handler = test_server.RequestChecks().handler({'Host': '127.0.0.1:8123',
            'X-Session-Token': 'test-token', 'Content-Type': 'application/json'})
        body = json.dumps(data).encode()
        handler.headers['Content-Length'] = str(len(body))
        handler.path = '/api/' + route
        handler.rfile = io.BytesIO(body)
        handler.reply = Mock()
        return handler

    def test_batch_routes_remain_authenticated_and_mutations_are_locked(self):
        for route, args, method in [('batch/preview', {'mode': 'sync'}, 'preview'),
                ('batch/start', {'plan': 'reviewed', 'auto_commit': True}, 'start'),
                ('batch/push/preview', {'id': 'job'}, 'preview_push'), ('batch/push', {'plan': 'reviewed'}, 'push'),
                ('batch/status', {}, 'status'), ('batch/cancel', {'id': 'job'}, 'cancel')]:
            handler = self.handler(route, args)
            batch = Mock()
            handler.server.repo = SimpleNamespace(batch=batch)
            handler.server.development = SimpleNamespace(status=lambda: {})
            handler.do_POST()
            self.assertEqual(handler.reply.call_args.args[0], 200)
            getattr(batch, method).assert_called_once()
            handler.headers['X-Session-Token'] = 'wrong'
            handler.do_POST()
            self.assertEqual(handler.reply.call_args.args[0], 400)
        for route in ('merge', 'fetch', 'target', 'commit', 'commit/preview', 'push', 'push/preview', 'batch/push/preview', 'batch/push', 'development/start'):
            handler = self.handler(route, {})
            repo = Mock()
            repo.batch.guard.side_effect = ValueError('queue running')
            handler.server.repo = repo
            handler.do_POST()
            self.assertEqual(handler.reply.call_args.args, (400, {'error': 'queue running'}))
