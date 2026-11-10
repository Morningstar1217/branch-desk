"""Conflict handoff checks use a fake Codex executable; no model or desktop calls."""
import json
from pathlib import Path
import shutil
import sys
import time
import unittest
from unittest.mock import patch

import test_server as fixtures
from conflict_resolver import rename_codex


class ResolverChecks(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MergeChecks("test_conflicts_keep_both_sides_and_no_commit")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.resolver_patch.stop()
        self.resolver = self.fixture.repo.resolver
        self.addCleanup(self.clean_logs)
        self.addCleanup(self.resolver.close)
        self.mode = "resolved"
        fake = Path(self.fixture.temp.name) / "fake-codex"
        script = """import json, pathlib, subprocess, sys, time
args = sys.argv[1:]
mode = pathlib.Path(__file__).with_name('mode.txt').read_text()
if 'app-server' in args:
    requests = []
    title = ''
    for line in sys.stdin:
        message = json.loads(line)
        requests.append(message)
        pathlib.Path(__file__).with_name('rename-invocation.json').write_text(json.dumps({'args':args,'requests':requests}))
        if 'id' not in message: continue
        result = {}
        if message['method'] == 'thread/name/set':
            if mode == 'rename-timeout': time.sleep(60)
            if mode == 'rename-exit': sys.exit(1)
            if mode == 'rename-denied':
                print(json.dumps({'id':message['id'],'error':{'message':'fixture denied'}}), flush=True)
                continue
            title = message['params']['name']
            print(json.dumps({'method':'thread/name/updated','params':{'name':title}}), flush=True)
        if message['method'] == 'thread/read':
            result = {'thread':{'name':'wrong title' if mode == 'rename-mismatch' else title}}
        print(json.dumps({'id':message['id'],'result':result}), flush=True)
    sys.exit(0)
root = pathlib.Path(args[args.index('-C') + 1])
prompt = sys.stdin.read()
pathlib.Path(__file__).with_name('invocation.json').write_text(json.dumps({'args':args,'prompt':prompt}))
mode = pathlib.Path(__file__).with_name('mode.txt').read_text()
print(json.dumps({'type':'thread.started','thread_id':'fixture-thread'}), flush=True)
if mode == 'sleep': time.sleep(60)
if mode == 'error':
    print(json.dumps({'type':'turn.failed'}), flush=True)
    sys.exit(1)
if mode != 'blocked':
    (root/'common.txt').write_text('target content\\nsource content\\n')
if mode == 'markers':
    (root/'common.txt').write_text('<<<<<<< target\\ntarget content\\n=======\\nsource content\\n>>>>>>> source\\n')
if mode == 'outside': (root/'incoming.txt').write_text('unexpected outside edit')
if mode == 'move-head':
    subprocess.check_call([GIT, '-C', str(root), 'update-ref', 'refs/heads/target', INCOMING])
result = {'status':'blocked' if mode == 'blocked' else 'resolved','summary':'fixture blocked' if mode == 'blocked' else 'fixture checked'}
directory = pathlib.Path(args[args.index('--output-last-message')+1]).parent
if mode != 'missing-result':
    if mode == 'invalid-result': result = {'status':'resolved','summary':[]}
    if mode == 'invalid-status': result['status'] = 'done'
    if mode == 'extra-result': result['unchecked'] = True
    if mode == 'list-result': result = ['resolved']
    (directory/'result.json').write_text('invalid json' if mode == 'malformed-result' else json.dumps(result))
report = '冲突已处理，等待工作台核验。\\n\\n- 保留双方功能。\\n- fixture checked。'
if mode == 'empty-report': report = ''
if mode == 'json-report': report = json.dumps(result)
pathlib.Path(args[args.index('--output-last-message')+1]).write_text(report)
print(json.dumps({'type':'turn.completed'}), flush=True)
if mode == 'linger': time.sleep(.3)
"""
        fake.write_text("#!" + sys.executable + "\nGIT = " + repr(fixtures.GIT) + "\nINCOMING = " + repr(self.fixture.incoming) + "\n" + script)
        fake.chmod(0o755)
        self.mode_file = fake.with_name("mode.txt")
        self.mode_file.write_text(self.mode)
        self.open_patch = patch("conflict_resolver.open_codex")
        self.open_app = self.open_patch.start()
        self.addCleanup(self.open_patch.stop)
        self.which_patch = patch("conflict_resolver.shutil.which", return_value=str(fake))
        self.which_patch.start()
        self.addCleanup(self.which_patch.stop)
        self.skill = Path(self.fixture.temp.name) / 'SKILL.md'
        self.skill.write_text('Fixture conflict workflow: compare both sides and preserve behavior.\n')
        self.skill_patch = patch('conflict_resolver.CONFLICT_SKILL', self.skill)
        self.skill_patch.start()
        self.addCleanup(self.skill_patch.stop)
        f = self.fixture
        f.cmd("checkout", "incoming")
        (f.path / "common.txt").write_text("source content\n")
        f.commit("source conflict")
        f.cmd("checkout", "target")
        (f.path / "common.txt").write_text("target content\n")
        f.commit("target conflict")
        self.head = f.cmd("rev-parse", "HEAD")

    def clean_logs(self):
        for job in self.resolver.jobs.values():
            if job.get("worker"):
                job["worker"].join(timeout=10)
            logs = job["public"]["logs"]
            if logs:
                shutil.rmtree(logs)

    def merge(self, mode="resolved"):
        self.mode_file.write_text(mode)
        result = self.fixture.repo.merge(self.fixture.plan()["plan"])
        self.assertEqual(result["kind"], "conflicts")
        return result

    def finish(self):
        self.resolver.jobs[self.fixture.target]["worker"].join(timeout=15)
        job = self.resolver.status(self.fixture.target)
        self.assertNotEqual(job["status"], "running")
        return job

    def test_automatic_handoff_uses_exact_worktree_and_stages_only_conflicts(self):
        self.merge()
        job = self.finish()
        self.assertEqual(job["status"], "succeeded", job["summary"])
        self.assertEqual(self.fixture.cmd("ls-files", "-u"), "")
        self.assertEqual(self.fixture.cmd("rev-parse", "HEAD"), self.head)
        self.assertEqual(self.fixture.cmd("show", ":incoming.txt"), "incoming")
        self.assertEqual(self.fixture.cmd("show", ":common.txt"), "target content\nsource content")
        self.open_app.assert_called_once_with("codex://threads/fixture-thread")
        invocation = json.loads((self.mode_file.parent / "invocation.json").read_text())
        args = invocation["args"]
        self.assertEqual(args[args.index("-C") + 1], self.fixture.target)
        self.assertEqual(args[args.index("--sandbox") + 1], "workspace-write")
        self.assertNotIn("--worktree", args)
        self.assertFalse(any(arg.startswith("--dangerously") for arg in args))
        self.assertIn("common.txt", invocation["prompt"])
        self.assertIn("不创建任何提交、不推送，只编辑获准的合并文件", invocation["prompt"])
        self.assertTrue(invocation['prompt'].startswith('$dev-command-workflow\n'))
        self.assertIn(str(self.skill), invocation['prompt'])
        self.assertNotIn('--output-schema', args)
        self.assertNotIn('立即将标题', invocation['prompt'])
        self.assertIn('本轮聊天标题由工作台在 CLI 完全退出后', invocation['prompt'])
        self.assertEqual(job['warning'], '')
        directory = Path(job['logs'])
        self.assertEqual(args[args.index('--add-dir') + 1], str(directory))
        self.assertEqual(args[args.index('--output-last-message') + 1], str(directory / 'report.md'))
        self.assertIn(str(directory / 'result.json'), invocation['prompt'])
        self.assertTrue((directory / 'report.md').read_text().startswith('冲突已处理'))
        self.assertEqual(json.loads((directory / 'result.json').read_text())['status'], 'resolved')

    def test_master_conflicts_follow_the_same_review_and_staging_flow(self):
        self.fixture.cmd('branch', '-m', 'master')
        self.test_automatic_handoff_uses_exact_worktree_and_stages_only_conflicts()
        self.assertEqual(self.fixture.repo.snapshot(self.fixture.target)['branch'], 'master')

    def test_invalid_report_or_machine_status_never_stages(self):
        for mode in ('missing-result', 'invalid-result', 'invalid-status', 'extra-result',
                     'list-result', 'malformed-result', 'empty-report', 'json-report'):
            with self.subTest(mode=mode):
                case = ResolverChecks('test_blocked_agent_leaves_conflict_index_intact')
                case.setUp()
                try:
                    case.merge(mode)
                    job = case.finish()
                    self.assertEqual(job['status'], 'failed')
                    self.assertIn('有效的状态文件或中文报告', job['summary'])
                    self.assertTrue(case.fixture.cmd('ls-files', '-u'))
                    self.assertEqual(case.fixture.cmd('rev-parse', 'HEAD'), case.head)
                finally:
                    case.doCleanups()

    def test_missing_conflict_skill_does_not_start_agent_or_stage(self):
        with patch('conflict_resolver.CONFLICT_SKILL', Path(self.fixture.temp.name) / 'missing-skill.md'):
            self.merge()
            job = self.finish()
        self.assertEqual(job['status'], 'failed')
        self.assertIn('dev-command-workflow skill', job['summary'])
        self.assertIsNone(self.resolver.jobs[self.fixture.target]['process'])
        self.assertTrue(self.fixture.cmd('ls-files', '-u'))
        self.open_app.assert_not_called()

    def test_handoff_waits_for_process_exit_and_closed_streams(self):
        observed=[]
        def renamed(*args):
            process = self.resolver.jobs[self.fixture.target]['process']
            observed.append(('rename', process.poll(), process.stdin.closed, process.stdout.closed))
            rename_codex(*args)
        def opened(url):
            process=self.resolver.jobs[self.fixture.target]['process']
            observed.append(('open',process.poll(),process.stdin.closed,process.stdout.closed,
                             self.resolver.status(self.fixture.target)['status']))
        self.open_app.side_effect=opened
        with patch('conflict_resolver.rename_codex', side_effect=renamed) as rename:
            self.merge('linger')
            self.assertEqual(self.resolver.status(self.fixture.target)['status'],'running')
            self.open_app.assert_not_called()
            rename.assert_not_called()
            self.assertEqual(self.finish()['status'],'succeeded')
        self.assertEqual(observed,[('rename',0,True,True),('open',0,True,True,'succeeded')])

    def test_finished_title_keeps_requirement_and_business_tags(self):
        for branch, expected in [('M-7149751847-【Unity】自动组合', '【Unity】自动组合'),
                                 ('M-sentry错误修复', 'M-sentry错误修复'), ('pre', 'pre')]:
            with self.subTest(branch=branch):
                rename_codex(str(self.mode_file.with_name('fake-codex')), 'fixture-thread', branch,
                             self.fixture.target, self.mode_file.parent)
                invocation = json.loads(self.mode_file.with_name('rename-invocation.json').read_text())
                requests = invocation['requests']
                self.assertEqual([item['method'] for item in requests],
                                 ['initialize', 'initialized', 'thread/name/set', 'thread/read'])
                self.assertEqual(requests[2]['params'], {'threadId':'fixture-thread', 'name':expected})
                self.assertEqual(requests[3]['params'], {'threadId':'fixture-thread', 'includeTurns':False})

    def test_rename_failures_preserve_resolution_and_open_the_session(self):
        for mode in ('rename-denied', 'rename-mismatch', 'rename-exit', 'rename-timeout'):
            with self.subTest(mode=mode):
                case = ResolverChecks('test_automatic_handoff_uses_exact_worktree_and_stages_only_conflicts')
                case.setUp()
                try:
                    with patch('conflict_resolver.RENAME_TIMEOUT', .5):
                        case.merge(mode)
                        job = case.finish()
                    self.assertEqual(job['status'], 'succeeded', job['summary'])
                    self.assertIn('聊天标题设置失败', job['warning'])
                    self.assertTrue((Path(job['logs']) / 'rename-error.txt').read_text())
                    self.assertEqual(case.fixture.cmd('ls-files', '-u'), '')
                    case.open_app.assert_called_once_with('codex://threads/fixture-thread')
                finally:
                    case.doCleanups()

    def test_failed_merge_repair_also_defers_title_until_exit(self):
        self.merge('blocked')
        previous = self.finish()
        self.assertEqual(previous['status'], 'failed')
        self.mode_file.write_text('resolved')
        state = self.fixture.repo.snapshot(self.fixture.target)
        self.resolver.start_repair(self.fixture.target, state, {'summary':'fixture failure'})
        job = self.finish()
        self.assertEqual(job['status'], 'succeeded', job['summary'])
        self.assertEqual(job['kind'], 'repair')
        self.assertEqual(job['warning'], '')
        self.assertTrue(self.fixture.cmd('ls-files', '-u'))
        invocation = json.loads(self.mode_file.with_name('invocation.json').read_text())
        self.assertNotIn('立即将标题', invocation['prompt'])
        self.assertIn('本轮聊天标题由工作台在 CLI 完全退出后', invocation['prompt'])
        shutil.rmtree(previous['logs'])

    def test_master_repair_can_be_reviewed_and_staged_without_committing(self):
        self.fixture.cmd('branch', '-m', 'master')
        self.test_failed_merge_repair_also_defers_title_until_exit()
        repo, path = self.fixture.repo, self.fixture.target
        plan = repo.preview_resolution(path)
        self.assertEqual(plan['branch'], 'master')
        result = repo.confirm_resolution(plan['plan'], True)
        self.assertEqual(result['state']['conflicts'], [])
        self.assertEqual(result['state']['operations'], ['MERGE_HEAD'])
        self.assertEqual(result['state']['head'], self.head)
        self.assertEqual(self.fixture.cmd('show', ':common.txt'), 'target content\nsource content')

    def test_missing_cli_preserves_conflicts_and_does_not_open_desktop(self):
        with patch("conflict_resolver.shutil.which", return_value=None):
            self.merge()
        job = self.resolver.status(self.fixture.target)
        self.assertEqual(job["status"], "failed")
        self.assertIn("未找到", job["summary"])
        self.assertTrue(self.fixture.cmd("ls-files", "-u"))
        self.open_app.assert_not_called()

    def test_desktop_open_failure_does_not_interrupt_resolution(self):
        self.open_app.side_effect = OSError("desktop unavailable")
        self.merge()
        job = self.finish()
        self.assertEqual(job["status"], "succeeded", job["summary"])
        self.assertTrue(job["warning"])
        self.assertEqual(job["url"], "codex://threads/fixture-thread")
        self.assertEqual(self.fixture.cmd("ls-files", "-u"), "")

    def test_next_merge_does_not_display_previous_resolver_result(self):
        self.merge()
        self.assertEqual(self.finish()["status"], "succeeded")
        self.fixture.cmd("commit", "-m", "fixture merge resolved")
        result = self.fixture.repo.merge(self.fixture.plan()["plan"])
        self.assertEqual(result["kind"], "unchanged")
        self.assertIsNone(result["state"]["resolution"])

    def test_outside_edit_stops_staging_without_reverting_any_changes(self):
        self.merge("outside")
        job = self.finish()
        self.assertEqual(job["status"], "failed")
        self.assertIn("范围外", job["summary"])
        self.assertTrue(self.fixture.cmd("ls-files", "-u"))
        self.assertEqual((self.fixture.path / "incoming.txt").read_text(), "unexpected outside edit")
        self.assertEqual(self.fixture.cmd("show", ":incoming.txt"), "incoming")

    def test_remaining_markers_are_not_reported_as_resolved(self):
        self.merge("markers")
        job = self.finish()
        self.assertEqual(job["status"], "failed")
        self.assertIn("标记", job["summary"])
        self.assertTrue(self.fixture.cmd("ls-files", "-u"))

    def test_blocked_agent_leaves_conflict_index_intact(self):
        self.merge("blocked")
        job = self.finish()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["summary"], "fixture blocked")
        self.assertTrue(self.fixture.cmd("ls-files", "-u"))

    def test_failed_turn_is_not_staged_or_reported_as_success(self):
        self.merge("error")
        self.assertEqual(self.finish()["status"], "failed")
        self.assertTrue(self.fixture.cmd("ls-files", "-u"))

    def test_changed_head_stops_automatic_staging(self):
        self.merge("move-head")
        job = self.finish()
        self.assertEqual(job["status"], "failed")
        self.assertIn("HEAD", job["summary"])
        self.assertTrue(self.fixture.cmd("ls-files", "-u"))

    def test_closing_tool_stops_its_agent_and_blocks_overlapping_operations(self):
        self.merge("sleep")
        deadline = time.monotonic() + 5
        job = self.resolver.jobs[self.fixture.target]
        while job["process"] is None and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertIsNotNone(job["process"])
        with self.assertRaisesRegex(ValueError, "Codex"):
            self.fixture.repo.fetch_remote()
        self.resolver.close()
        self.assertEqual(self.finish()["status"], "failed")
        self.assertIsNotNone(job["process"].poll())
        self.open_app.assert_not_called()
        self.assertTrue(self.fixture.cmd("ls-files", "-u"))
        self.assertFalse(self.mode_file.with_name('rename-invocation.json').exists())
