"""Start a skill-driven requirement review and hand its conversation to Codex."""
import json
import os
import platform_support as platform
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading

from conflict_resolver import open_codex
from feishu_project import ScheduleError

READ_TOOLS = ('list_workitem_field_config', 'get_workitem_brief', 'get_node_detail', 'list_workitem_comments')
REQUIRED_TOOLS = frozenset(READ_TOOLS[:2])


class DevelopmentLauncher:
    def __init__(self, repo, schedule):
        self.repo, self.schedule = repo, schedule
        self.jobs = {}
        self.history = getattr(repo, 'history', None)
        self.lock = threading.RLock()
        self.stopping = False

    def status(self):
        with self.lock:
            if not self.schedule.config.get('enabled'):
                return {}
            saved = self.history.tasks(self.schedule.space_id) if self.history else {}
            summaries = dict(finished='上次计划已生成，可打开已有开发任务继续确认和实施。',
                             blocked='上次需求读取受阻，请打开已有会话查看原因。',
                             failed='上次需求读取未完成，请查看已有会话后决定是否重新读取。',
                             interrupted='服务曾中断，读取结果未确认；请先查看已有会话，不自动重试。',
                             running='需求正在读取，请等待本轮结束。')
            result = {key: dict(work_item_id=key, status=item['status'], url=item['thread_url'],
                               previous_url=item['previous_url'], summary=summaries[item['status']], warning='')
                      for key, item in saved.items() if item['status'] in summaries}
            result.update({key: dict(job['public']) for key, job in self.jobs.items()})
            return result

    def persist(self, public):
        if self.history:
            self.history.remember_development(public, self.schedule.space_id)
            if self.history.warning:
                public['warning'] = self.history.warning

    def start(self, data):
        self.schedule.ensure_enabled()
        identifier = self.schedule.identifier(data.get('work_item_id'))
        restart_from = data.get('restart_from')
        if restart_from is not None and (not isinstance(restart_from, str) or
                not re.fullmatch(r'codex://threads/[A-Za-z0-9_-]{1,128}', restart_from)):
            raise ScheduleError('重新读取需要有效的原任务链接，请刷新后重试。')
        with self.lock:
            if self.stopping:
                raise ScheduleError('工具正在关闭，未启动开发任务。')
            previous = self.jobs.get(identifier)
            previous_public = self.status().get(identifier)
            if restart_from:
                if not previous_public or previous_public['url'] != restart_from:
                    raise ScheduleError('开发任务已变化，请刷新后重新选择；未重复启动。')
                if previous_public['status'] == 'running':
                    raise ScheduleError('需求仍在读取，请等待结束后再重新读取。')
            elif previous_public and (previous_public['status'] not in ('failed', 'blocked', 'interrupted') or previous_public['url']):
                return dict(previous_public)
            if any(job['public']['status'] == 'running' for job in self.jobs.values()):
                raise ScheduleError('已有需求正在读取，请等待本轮完成后再启动其他需求。')
            skill = Path.home() / '.agents/skills/lark-requirement-frontend-dev/SKILL.md'
            if not skill.is_file():
                raise ScheduleError('未找到 lark-requirement-frontend-dev skill，请先在 Codex 中安装此 skill。')
            executable = shutil.which('codex')
            if not executable:
                raise ScheduleError('未找到 Codex CLI，请先安装并登录。')
            requirement = self.schedule.requirement({'work_item_id': identifier})
            client = self.schedule.client
            tools = [name for name in READ_TOOLS if name not in client.disabled and
                     (client.allowed is None or name in client.allowed)]
            if not REQUIRED_TOOLS.issubset(tools):
                raise ScheduleError('读取受阻：FeishuProjectMcp 的需求读取工具未启用，请检查工具权限。')
            # Keep the conversation in the registered project; the skill manages worktrees later.
            path = self.repo.target(self.repo.root)
            directory = Path(tempfile.mkdtemp(prefix='worktree-merge-development-'))
            public = dict(work_item_id=identifier, status='running', summary=f'正在 {Path(path).name} 项目中自动运行本地 skill、读取需求并生成计划…',
                          url='', warning='')
            job = dict(public=public, process=None, worker=None, tools=tools)
            context = dict(**requirement, path=path,
                           read_status_file=str(directory / 'read-status.json'))
            if restart_from:
                public['previous_url'] = restart_from
                context['previous_thread_url'] = restart_from
            self.jobs[identifier] = job
            worker = threading.Thread(target=self._run, args=(job, executable, directory, context, skill), daemon=True)
            job['worker'] = worker
            try:
                worker.start()
                self.persist(public)
            except RuntimeError:
                if previous:
                    self.jobs[identifier] = previous
                else:
                    self.jobs.pop(identifier, None)
                raise ScheduleError('无法启动 Codex 任务线程，请稍后重试。') from None
            return dict(public)

    @staticmethod
    def prompt(context, skill):
        return f'''$lark-requirement-frontend-dev
{context['url']}

请实际读取本机 skill：{skill}，按其流程审查和命名会话，直接以正常中文 Markdown 回复。
项目目录：{context['path']}。从此项目审查，按 skill 查找已有需求 worktree；本轮不创建或切换 worktree。
本轮自动完成只读需求审查和开发计划，不修改业务代码、索引、Git 配置、分支、飞书数据，不启停服务、不提交推送。
需求与关联文档通过接口读取，必要分页读全；不使用浏览器插件或浏览器控制工具获取信息，不绕过权限拒绝。必要正文读取失败时明确报告缺口，不自动改用网页采集。
原型按 analyze-web-prototype 静态读取；静态资料不足时列入计划缺口，不操作浏览器。
在计划末尾集中列出实施所需目录/Git、网络和 MCP 写入范围，等待用户在本会话确认计划后继续开发。CLI 首轮使用完全访问；桌面续聊须按其实际权限状态判断。服务启停、浏览器、外部写入、提交推送仍按用户授权边界执行，不修改全局权限。
后续实施如 Git/worktree 权限受阻，停止业务文件写入，不绕到临时副本开发。重新读取不继承旧计划确认，不覆盖已有成果。
只为工具同步读取状态：结束前在 {context['read_status_file']} 写入 {{"status":"ready"}} 或 {{"status":"blocked"}}；主需求正文读全、计划可形成时为 ready，主正文或必要分页受阻时为 blocked。接口契约或可选原型待确认列入计划缺口。
状态文件不代替本会话的计划；主需求读取失败时明确说明“读取受阻”，不要将 JSON 作为最终聊天回复。
'''

    def _stop(self, job):
        with self.lock:
            process = job['process']
            if process is not None and process.poll() is None:
                try:
                    platform.terminate_tree(process.pid) if platform.IS_WINDOWS else os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    process.kill()  # macOS can reject killpg when the short-lived process has just exited.

    def close(self):
        with self.lock:
            self.stopping = True
            for job in self.jobs.values():
                self._stop(job)

    def _run(self, job, executable, directory, context, skill):
        process = timer = None
        public = job['public']
        try:
            prompt = self.prompt(context, skill)
            (directory / 'prompt.txt').write_text(prompt)
            args = [executable, '--no-daemon', 'exec', '--sandbox', 'danger-full-access',
                    '-C', context['path'], '--json', '--output-last-message', str(directory / 'result.md')]
            # Codex exec cannot answer approval prompts; auto-approve only the enabled read tools.
            for name in job['tools']:
                args += ['-c', f'mcp_servers.FeishuProjectMcp.tools.{name}.approval_mode="auto"']
            args += ['-c', 'mcp_servers.cua_repl={enabled=false,command="false"}',
                     '-c', 'mcp_servers.chrome-devtools={enabled=false,command="false"}']
            args.append('-')
            completed = False
            read_successes, read_failures = set(), set()
            with (directory / 'stderr.log').open('w') as stderr, (directory / 'events.jsonl').open('w') as events:
                with self.lock:
                    if self.stopping:
                        raise ScheduleError('工具正在关闭，未启动开发任务。')
                    process = subprocess.Popen(platform.windows_command(args), cwd=context['path'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=stderr, text=True, encoding='utf-8', errors='replace', **platform.child_options(),
                        env=os.environ.copy())
                    job['process'] = process
                timer = threading.Timer(1800, self._stop, args=(job,))
                timer.daemon = True
                timer.start()
                process.stdin.write(prompt)
                process.stdin.close()
                for line in process.stdout:
                    events.write(line)
                    events.flush()
                    event = json.loads(line)
                    item = event.get('item', {})
                    if (event.get('type') == 'item.completed' and item.get('type') == 'mcp_tool_call'
                            and item.get('server') == 'FeishuProjectMcp' and item.get('tool') in job['tools']):
                        name = item.get('tool')
                        if item.get('status') == 'completed' and not item.get('error') and not (item.get('result') or {}).get('isError'):
                            read_successes.add(name)
                            read_failures.discard(name)
                        else:
                            read_failures.add(name)
                    if event.get('type') == 'thread.started':
                        thread_id = event.get('thread_id', '')
                        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', thread_id):
                            raise ScheduleError('Codex 返回的会话标识无效。')
                        with self.lock:
                            public.update(url='codex://threads/' + thread_id, summary='Codex 正在按 skill 优先通过接口读取需求并审查代码…')
                            self.persist(public)
                    if event.get('type') == 'turn.completed':
                        completed = True
                    if event.get('type') in ('turn.failed', 'error'):
                        raise ScheduleError('Codex 读取任务失败，请在对应会话中查看原因。')
                if process.wait() != 0 or not completed or not public['url']:
                    raise ScheduleError('Codex 本轮未正常结束，请查看已有开发会话；未自动重试。')
                result = json.loads((directory / 'read-status.json').read_text())
                report = (directory / 'result.md').read_text().strip()
                if (not isinstance(result, dict) or set(result) != {'status'} or
                        result['status'] not in ('ready', 'blocked') or not report):
                    raise ScheduleError('Codex 未返回有效审查结果。')
            # The report distinguishes missing primary text from optional material or contract gaps.
            blocked = result['status'] == 'blocked' or bool(read_failures & REQUIRED_TOOLS) or not REQUIRED_TOOLS.issubset(read_successes)
            outcome = dict(status='blocked' if blocked else 'finished', summary=(
                '读取受阻：需求或关联资料尚未完整取得，请在已有 Codex 会话查看原因并继续；未自动重试。' if blocked else
                '计划已生成，请到 Codex 一并确认计划和实施权限范围，再继续开发；尚未修改业务代码。'))
        except Exception:
            self._stop(job)
            outcome = dict(status='failed', summary=(
                '读取受阻：Codex 任务失败、超时或结果无效，请查看已有会话；未自动重试。' if public['url'] else
                '读取受阻：未能建立 Codex 会话，请检查 CLI 登录和 MCP 配置后重试。'))
        finally:
            if timer:
                timer.cancel()
            self._stop(job)
            if process is not None:
                process.wait()
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
            # The CLI owns the conversation until it exits, including after turn.completed.
            with self.lock:
                public.update(outcome)
                self.persist(public)
                if public['url'] and not self.stopping:
                    try:
                        open_codex(public['url'])
                    except (OSError, subprocess.SubprocessError):
                        public['warning'] = '未能自动打开 Codex，可点击“打开开发任务”。'
