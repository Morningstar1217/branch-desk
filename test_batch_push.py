"""Batch push checks use disposable local bare remotes, never business remotes."""
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import test_push


class BatchPushChecks(unittest.TestCase):
    def setUp(self):
        self.fixture = test_push.PushChecks('test_reviewed_push_updates_only_same_branch_and_preserves_head_and_tags')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.f
        self.repo = self.fixture.repo
        self.batch = self.repo.batch
        self.paths = [('target', self.fixture.path)]
        self.addCleanup(self.stop)

    def stop(self):
        self.batch.close()
        if self.batch.worker:
            self.batch.worker.join(timeout=40)

    def add_target(self, name):
        path = Path(self.f.temp.name) / name
        self.f.cmd('branch', '--no-track', name)
        self.f.cmd('worktree', 'add', '--', str(path), name)
        self.paths.append((name, str(path)))
        return path

    def completed(self, duplicate=False):
        rows = [dict(target=branch, path=path, head=self.fixture.head, status='succeeded', commit=self.fixture.head)
                for branch, path in self.paths]
        if duplicate:
            rows += rows[:1]
        self.batch.job = dict(id='completed-merge', mode='all-in' if duplicate else 'sync',
                              status='succeeded', summary='merge completed', rows=rows)

    def finish(self, plan):
        self.batch.push(plan['plan'])
        self.batch.worker.join(timeout=60)
        result = self.batch.status()['push']
        self.assertNotEqual(result['status'], 'running', result)
        return result

    def remote_head(self, branch):
        return self.f.cmd('ls-remote', '--heads', 'origin', 'refs/heads/' + branch)

    def test_all_in_deduplicates_final_target_and_push_requires_confirmation(self):
        self.f.cmd('tag', 'keep-local-tag')
        self.completed(duplicate=True)
        before = self.fixture.remote_refs()
        plan = self.batch.preview_push('completed-merge')
        self.assertEqual(len(plan['rows']), 1)
        self.assertEqual(plan['rows'][0]['ref'], 'refs/heads/target')
        self.assertEqual(self.fixture.remote_refs(), before)
        result = self.finish(plan)
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertIn(self.fixture.head, self.remote_head('target'))
        self.assertNotIn('keep-local-tag', self.fixture.remote_refs())
        with self.assertRaisesRegex(ValueError, '失效'):
            self.batch.push(plan['plan'])

    def test_sync_multiple_first_pushes_keep_same_name_upstreams_and_hooks(self):
        self.add_target('Release-Preview')
        self.f.cmd('branch', '--unset-upstream', 'target')
        subprocess.check_call([test_push.GIT, '--git-dir', str(self.fixture.remote), 'update-ref', '-d', 'refs/heads/target'])
        hooks = Path(self.f.temp.name) / 'hooks'
        hooks.mkdir()
        hook = hooks / 'pre-push'
        marker = hooks / 'ran'
        hook.write_text('#!/bin/sh\necho ran >> "' + str(marker) + '"\n')
        hook.chmod(0o755)
        self.f.cmd('config', 'core.hooksPath', str(hooks))
        self.completed()
        plan = self.batch.preview_push('completed-merge')
        self.assertTrue(all(r['first'] for r in plan['rows']))
        self.assertFalse(marker.exists())
        result = self.finish(plan)
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual([r['branch'] for r in result['rows']], ['target', 'Release-Preview'])
        self.assertEqual(marker.read_text().splitlines(), ['ran', 'ran'])
        for name, _ in self.paths:
            self.assertIn(self.fixture.head, self.remote_head(name))
            self.assertEqual(self.f.cmd('config', '--get', 'branch.' + name + '.remote'), 'origin')
            self.assertEqual(self.f.cmd('config', '--get', 'branch.' + name + '.merge'), 'refs/heads/' + name)

    def test_already_equal_is_skipped_without_git_push(self):
        self.repo.push(self.repo.preview_push(self.fixture.path)['plan'])
        self.completed()
        plan = self.batch.preview_push('completed-merge')
        self.assertTrue(plan['rows'][0]['unchanged'])
        with patch('server.git', wraps=test_push.server.git) as commands:
            result = self.finish(plan)
        self.assertEqual(result['rows'][0]['status'], 'skipped')
        self.assertFalse(any('push' in call.args[1:] for call in commands.call_args_list))

    def test_hook_failure_stops_later_targets_and_repreview_skips_completed_push(self):
        self.add_target('blocked')
        self.add_target('later')
        hooks = Path(self.f.temp.name) / 'hooks'
        hooks.mkdir()
        hook = hooks / 'pre-push'
        hook.write_text('#!/bin/sh\nif [ "$(git symbolic-ref --short HEAD)" = blocked ]; then echo fixture-rejected >&2; exit 1; fi\n')
        hook.chmod(0o755)
        self.f.cmd('config', 'core.hooksPath', str(hooks))
        self.completed()
        result = self.finish(self.batch.preview_push('completed-merge'))
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual([r['status'] for r in result['rows']], ['succeeded', 'failed', 'pending'])
        self.assertIn('fixture-rejected', result['summary'])
        self.assertIn(self.fixture.head, self.remote_head('target'))
        self.assertEqual(self.remote_head('blocked'), '')
        self.assertEqual(self.remote_head('later'), '')
        plan = self.batch.preview_push('completed-merge')
        self.assertTrue(plan['rows'][0]['unchanged'])
        self.assertFalse(plan['rows'][1]['unchanged'])

    def test_remote_changes_before_confirmation_stop_before_any_push(self):
        self.completed()
        plan = self.batch.preview_push('completed-merge')
        self.fixture.remote_advance()
        before = self.fixture.remote_refs()
        with self.assertRaisesRegex(ValueError, '远端已变化'):
            self.batch.push(plan['plan'])
        self.assertEqual(self.fixture.remote_refs(), before)
        self.assertNotIn('push', self.batch.status())

    def test_config_change_between_pushes_is_not_mistaken_for_upstream_setup(self):
        self.add_target('later')
        self.completed()
        push = self.repo.push
        def push_then_change(identifier):
            result = push(identifier)
            self.f.cmd('config', 'core.hooksPath', '/unexpected-hooks')
            return result
        with patch.object(self.repo, 'push', side_effect=push_then_change):
            result = self.finish(self.batch.preview_push('completed-merge'))
        self.assertEqual(result['status'], 'failed', result)
        self.assertIn('配置已变化', result['summary'])
        self.assertEqual([r['status'] for r in result['rows']], ['succeeded', 'failed'])
        self.assertEqual(self.remote_head('later'), '')

    def test_cancel_after_first_push_leaves_later_target_unexecuted(self):
        self.add_target('later')
        self.completed()
        push = self.repo.push
        def push_then_cancel(identifier):
            result = push(identifier)
            self.batch.cancel('completed-merge')
            return result
        with patch.object(self.repo, 'push', side_effect=push_then_cancel):
            result = self.finish(self.batch.preview_push('completed-merge'))
        self.assertEqual(result['status'], 'stopped', result)
        self.assertEqual([r['status'] for r in result['rows']], ['succeeded', 'stopped'])
        self.assertEqual(self.remote_head('later'), '')

    def test_changed_head_expiry_and_wrong_batch_are_rejected(self):
        self.completed()
        with self.assertRaisesRegex(ValueError, '已全部完成'):
            self.batch.preview_push('wrong-job')
        plan = self.batch.preview_push('completed-merge')
        self.batch.push_plans[plan['plan']]['time'] -= 601
        with self.assertRaisesRegex(ValueError, '失效'):
            self.batch.push(plan['plan'])
        (self.f.path / 'new.txt').write_text('new work\n')
        self.f.commit('fix: new work')
        before = self.fixture.remote_refs()
        with self.assertRaisesRegex(ValueError, 'HEAD 或分支已变化'):
            self.batch.preview_push('completed-merge')
        self.assertEqual(self.fixture.remote_refs(), before)
