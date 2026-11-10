"""Automatic skill launch with an isolated fake CLI, never a real model or project."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from development import DevelopmentLauncher, READ_TOOLS
from feishu_project import FeishuProject, ScheduleError

FAKE = r'''import json, os, pathlib, re, sys, time
args=sys.argv[1:]; prompt=sys.stdin.read()
pathlib.Path(os.environ['DEV_CAPTURE']).write_text(json.dumps({'args':args,'prompt':prompt,'cwd':os.getcwd()}))
mode=os.environ.get('DEV_MODE','success')
print(json.dumps({'type':'thread.started','thread_id':'fixture-development-thread'}),flush=True)
if mode=='hold': time.sleep(20)
if mode=='failure': print(json.dumps({'type':'turn.failed'}),flush=True);sys.exit(1)
for name in ('list_workitem_field_config','get_workitem_brief'):
 denied=mode=='denied'
 print(json.dumps({'type':'item.completed','item':{'type':'mcp_tool_call','server':'FeishuProjectMcp','tool':name,
  'status':'failed' if denied else 'completed','result':{'content':[],'isError':denied}}}),flush=True)
path=re.search(r'只为工具同步读取状态：结束前在 (.+) 写入',prompt).group(1)
if mode!='missing': pathlib.Path(path).write_text(json.dumps({'status':'blocked' if mode=='blocked' else 'ready'}))
pathlib.Path(args[args.index('--output-last-message')+1]).write_text('# 需求审查与开发计划\n\n权限范围和计划等待用户确认。')
print(json.dumps({'type':'turn.completed'}),flush=True)
if mode=='linger': time.sleep(.2)
'''


class DevelopmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='auto-skill-check-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo_path = self.root / '中文 #& 项目'; self.repo_path.mkdir()
        self.skill = self.root / '.agents/skills/lark-requirement-frontend-dev/SKILL.md'
        self.skill.parent.mkdir(parents=True);self.skill.write_text('# fixture')
        self.executable = self.root / 'fake-codex';self.executable.write_text('#!'+sys.executable+'\n'+FAKE);self.executable.chmod(0o700)
        self.capture = self.root / 'capture.json'
        self.repo = SimpleNamespace(root=str(self.repo_path),target=lambda p:p,
            worktrees=lambda:[dict(path='/different-worktree',branch='M-123-title',missing=False)])
        self.schedule = SimpleNamespace(identifier=FeishuProject.identifier,config=dict(enabled=True),space_id='62983005bc43adc6923d707d',
            ensure_enabled=lambda: None,client=SimpleNamespace(allowed=None,disabled=[]),
            requirement=Mock(return_value=dict(work_item_id='123',title='实际需求',url='https://project.feishu.cn/platform/story/detail/123')))
        self.launcher = DevelopmentLauncher(self.repo,self.schedule)
        self.addCleanup(self.close)
        for p in [patch('development.Path.home',return_value=self.root),
                  patch('development.shutil.which',return_value=str(self.executable)),
                  patch.dict(os.environ,DEV_CAPTURE=str(self.capture),DEV_MODE='success')]:
            p.start();self.addCleanup(p.stop)
        opener=patch('development.open_codex');self.opener=opener.start();self.addCleanup(opener.stop)
        self.directories=[]
        def directory(prefix):
            path=self.root/(prefix+str(len(self.directories)));path.mkdir();self.directories.append(path);return str(path)
        tmp=patch('development.tempfile.mkdtemp',side_effect=directory);tmp.start();self.addCleanup(tmp.stop)

    def close(self):
        self.launcher.close()
        for job in self.launcher.jobs.values():job['worker'].join(timeout=4)

    def finish(self):
        self.launcher.jobs['123']['worker'].join(timeout=4)
        self.assertFalse(self.launcher.jobs['123']['worker'].is_alive())
        return self.launcher.status()['123']

    def test_click_runs_skill_automatically_in_project_with_formal_review(self):
        self.launcher.start({'work_item_id':'123','url':'https://untrusted.invalid','path':'/other'})
        result=self.finish();captured=json.loads(self.capture.read_text())
        self.assertEqual(result['status'],'finished')
        self.assertEqual(captured['cwd'],self.repo.root)
        self.assertEqual(captured['args'][captured['args'].index('-C')+1],self.repo.root)
        self.assertEqual(captured['args'][captured['args'].index('--sandbox')+1], 'danger-full-access')
        self.assertNotIn('--approve-for-me',captured['args'])
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox',captured['args'])
        self.assertNotIn('sandbox_workspace_write.network_access', '\n'.join(captured['args']))
        self.assertNotIn('features.network_proxy', '\n'.join(captured['args']))
        for name in READ_TOOLS:
            self.assertIn(f'mcp_servers.FeishuProjectMcp.tools.{name}.approval_mode="auto"', captured['args'])
        self.assertNotIn('approval_mode="approve"', '\n'.join(captured['args']))
        self.assertNotIn('--model',captured['args']);self.assertNotIn('--output-schema',captured['args'])
        self.assertTrue(captured['prompt'].startswith('$lark-requirement-frontend-dev\nhttps://project.feishu.cn/platform/story/detail/123\n'))
        self.assertNotIn('untrusted.invalid',captured['prompt'])
        self.assertIn('不修改业务代码',captured['prompt']);self.assertIn('确认计划后继续开发',captured['prompt'])
        self.assertIn('桌面续聊须按其实际权限状态判断', captured['prompt'])
        self.assertNotIn('本会话已使用完全访问执行环境', captured['prompt'])
        self.assertIn('不使用浏览器插件或浏览器控制工具获取信息',captured['prompt'])
        self.assertIn('不操作浏览器',captured['prompt'])
        self.assertIn('mcp_servers.cua_repl={enabled=false,command="false"}',captured['args'])
        self.assertIn('mcp_servers.chrome-devtools={enabled=false,command="false"}',captured['args'])
        self.assertNotIn('browser_transport',self.launcher.jobs['123'])
        self.assertNotIn('mcp_servers.FeishuProjectMcp.enabled_tools=', '\n'.join(captured['args']))
        self.assertIn('实施权限范围',result['summary'])
        self.opener.assert_called_once_with('codex://threads/fixture-development-thread')

    def test_model_released_before_desktop_handoff(self):
        os.environ['DEV_MODE']='linger';observed=[]
        def opened(url):
            p=self.launcher.jobs['123']['process'];observed.append((p.poll(),p.stdin.closed,p.stdout.closed))
        self.opener.side_effect=opened;self.launcher.start({'work_item_id':'123'});self.finish()
        self.assertEqual(observed,[(0,True,True)])

    def test_duplicate_start_reuses_thread_and_explicit_restart_is_guarded(self):
        self.launcher.start({'work_item_id':'123'});first=self.finish()
        self.assertEqual(self.launcher.start({'work_item_id':'123'}),first)
        self.assertEqual(self.opener.call_count,1)
        with self.assertRaises(ScheduleError):self.launcher.start({'work_item_id':'123','restart_from':'codex://threads/stale'})
        self.launcher.start({'work_item_id':'123','restart_from':first['url']});second=self.finish()
        self.assertEqual(second['previous_url'],first['url']);self.assertEqual(self.opener.call_count,2)

    def test_read_failures_and_incomplete_status_are_never_reported_ready(self):
        for mode in ['blocked','denied','failure','missing']:
            os.environ['DEV_MODE']=mode;self.launcher=DevelopmentLauncher(self.repo,self.schedule)
            self.launcher.start({'work_item_id':'123'});result=self.finish()
            self.assertIn(result['status'],['blocked','failed']);self.assertIn('读取受阻',result['summary'])
            self.launcher.close()

    def test_native_open_failure_preserves_real_session_link(self):
        self.opener.side_effect=OSError('no app');self.launcher.start({'work_item_id':'123'});r=self.finish()
        self.assertEqual(r['status'],'finished');self.assertTrue(r['warning']);self.assertTrue(r['url'].startswith('codex://threads/'))

    def test_invalid_input_disabled_read_tools_and_missing_skill_do_not_launch(self):
        for value in [None,'123/..','１23',1]:
            with self.assertRaises(ScheduleError):self.launcher.start({'work_item_id':value})
        self.skill.unlink()
        with self.assertRaises(ScheduleError):self.launcher.start({'work_item_id':'123'})
        self.skill.write_text('# fixture');self.schedule.client.disabled=[READ_TOOLS[0]]
        with self.assertRaisesRegex(ScheduleError,'未启用'):self.launcher.start({'work_item_id':'123'})
        self.assertFalse(self.capture.exists());self.opener.assert_not_called()

    def test_development_history_uses_current_feishu_space(self):
        history = Mock(warning='')
        history.tasks.return_value = {}
        self.launcher.history = history
        self.assertEqual(self.launcher.status(), {})
        history.tasks.assert_called_once_with('62983005bc43adc6923d707d')
        self.launcher.persist(dict(work_item_id='123', status='finished', url='codex://threads/example'))
        history.remember_development.assert_called_once()
        self.assertEqual(history.remember_development.call_args.args[1], '62983005bc43adc6923d707d')
        self.schedule.config['enabled'] = False
        self.schedule.ensure_enabled = Mock(side_effect=ScheduleError('未关联飞书'))
        self.assertEqual(self.launcher.status(), {})
        with self.assertRaises(ScheduleError):
            self.launcher.start({'work_item_id': '123'})

    def test_close_stops_owned_read_only_run_and_blocks_further_starts(self):
        os.environ['DEV_MODE']='hold';self.launcher.start({'work_item_id':'123'})
        deadline=time.monotonic()+3
        while not self.capture.exists() and time.monotonic()<deadline:time.sleep(.01)
        self.close()
        self.assertIsNotNone(self.launcher.jobs['123']['process'].poll())
        with self.assertRaisesRegex(ScheduleError,'关闭'):self.launcher.start({'work_item_id':'123'})


if __name__=='__main__':unittest.main()
