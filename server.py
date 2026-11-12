#!/usr/bin/env python3
"""Local-only Git merge UI with separately reviewed commits and same-name pushes."""
import argparse
import hashlib
import json
import os
import platform_support as platform
from pathlib import Path
import secrets
import shutil
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from conflict_resolver import ConflictResolver, open_codex
from feishu_project import FeishuProject, ScheduleError
from development import DevelopmentLauncher
from worktree_services import WorktreeServices, service_options, validate_service_config
from backend_services import BackendServices
from batch_merge import BatchMerger
from operation_history import LEGACY_SPACE_ID, OperationHistory, record_operation
from workbench_runtime import RuntimeSession, RuntimeError as WorkbenchRuntimeError
from platform_config import ProjectRegistry, history_file, project_id, read_pinned, repository, write_pinned
from urllib.parse import parse_qs, urlencode, urlsplit

WEB = Path(__file__).resolve().parent / "web"
# Use platform Git directly instead of an editor or a PATH wrapper.
GIT = "/usr/bin/git" if Path("/usr/bin/git").is_file() else "git"


class GuardError(Exception):
    pass


def commit_message(message, branch, bibase=True):
    if not isinstance(message, str) or not message.strip() or len(message) > 4096 or "\0" in message:
        raise GuardError("请填写有效提交说明，最多 4096 字符。")
    message = message.strip()
    lines = message.splitlines()
    match = re.fullmatch(r"(?:feat|fix|docs|style|refactor|perf|test|chore|revert|build|merge)(?:\([^()]+\))?: (.+)", lines[0])
    if not match or len(lines[0]) > 108 or match[1].endswith("."):
        raise GuardError("提交标题须为 type: 描述，最多 108 字符，不使用 ! 或英文句号结尾。")
    identifiers = set(re.findall(r"M-[0-9]{10}", branch)) if bibase else set()
    if len(identifiers) > 1:
        raise GuardError("分支包含多个需求编号，请先明确提交对应的需求。")
    if identifiers:
        identifier = next(iter(identifiers))
        if not match[1].startswith(identifier + " ") or lines[0].count(identifier) != 1:
            raise GuardError("提交描述须以 " + identifier + " 开头，标题内仅出现一次。")
    if (len(lines) > 1 and lines[1]) or any(len(line) > 100 for line in lines[1:]):
        raise GuardError("标题与正文之间须空一行，正文每行最多 100 字符。")
    return message


def git(path, *args, allowed=(0,), display=False, literal=False):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
    if args and args[0] == 'push':
        # Trace2 reads early global config; env prevents Git AI from starting extra notes/tag pushes.
        env['GIT_TRACE2_EVENT'] = '0'
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(
            [GIT, "--no-pager", "-C", str(path), *args], env=env,
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            **platform.child_options(),
        )
        try:
            proc.wait(timeout=90)
        except subprocess.TimeoutExpired:
            platform.terminate_tree(proc.pid) if platform.IS_WINDOWS else os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise GuardError("Git 操作超时。请刷新状态并检查工作区；不要直接重复操作。")
        output.seek(0)
        raw = output.read(2_000_001)
    if len(raw) > 2_000_000 and not display:
        raise GuardError("Git 输出过大，请用命令行检查。")
    text = raw[:200_000 if display else 2_000_000].decode("utf-8", errors="surrogateescape" if literal else "replace")
    if display and len(raw) > 200_000:
        text += "\n…输出已截断，请在终端查看完整内容。"
    if proc.returncode not in allowed:
        raise GuardError(text.strip() or "Git 操作失败")
    return proc.returncode, text


class Repository:
    def __init__(self, path):
        self.root = str(Path(git(path, "rev-parse", "--show-toplevel")[1].removesuffix("\n")).resolve())
        self.common = self.common_dir(self.root)
        self.bibase = service_options(self.root)['preset'] == 'bibase'
        self.plans = {}
        self.commit_plans = {}
        self.change_plans = {}
        self.resolution_plans = {}
        self.push_plans = {}
        self.release_plans = {}
        self.branch_delete_plans = {}
        self.merged_cleanup_plans = {}
        self.history = None
        self.resolver = ConflictResolver(git)
        self.batch = BatchMerger(self)

    def common_dir(self, path):
        return str(Path(git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")[1].removesuffix("\n")).resolve())

    def worktrees(self):
        raw = git(self.root, "worktree", "list", "--porcelain", "-z")[1]
        result = []
        for block in raw.split("\0\0"):
            fields = dict(line.partition(" ")[::2] for line in block.split("\0") if line)
            if "worktree" in fields:
                result.append({"path": str(Path(fields["worktree"]).resolve()), "branch": fields.get("branch", "").removeprefix("refs/heads/"),
                               "head": fields.get("HEAD", ""), "missing": not Path(fields["worktree"]).is_dir(),
                               "locked": "locked" in fields, "prunable": "prunable" in fields})
        return result

    def target(self, path):
        if not isinstance(path, str):
            raise GuardError("目标必须是当前仓库登记的 worktree。")
        path = str(Path(path).resolve())
        if path not in {str(Path(w["path"]).resolve()) for w in self.worktrees()}:
            raise GuardError("目标必须是当前仓库登记的 worktree。")
        if self.common_dir(path) != self.common:
            raise GuardError("worktree 的仓库归属已变化，请重启工具。")
        return path

    def refs(self):
        raw = git(self.root, "for-each-ref", "--format=%(refname)%00%(objectname)%00%(symref)", "refs/heads/", "refs/remotes/")[1]
        return [{"ref": parts[0], "sha": parts[1], "name": parts[0].removeprefix("refs/heads/").removeprefix("refs/remotes/")}
                for line in raw.splitlines() if len(parts := line.split("\0")) == 3 and not parts[2]]

    def branches(self):
        refs, trees = self.refs(), self.worktrees()
        local = {r["name"] for r in refs if r["ref"].startswith("refs/heads/")}
        remotes = sorted(git(self.root, "remote")[1].splitlines(), key=len, reverse=True)
        result = []
        for item in refs:
            remote = ""
            branch = item["name"]
            if item["ref"].startswith("refs/remotes/"):
                remote = next((r for r in remotes if branch.startswith(r + "/")), "")
                branch = branch[len(remote) + 1:] if remote else branch
                if branch in local:
                    continue
            tree = next((w for w in trees if w["branch"] == branch), {})
            result.append(dict(ref=item["ref"], name=item["name"], branch=branch, remote=remote,
                               path=tree.get("path", ""), missing=tree.get("missing", False)))
        result.extend(dict(ref="", name="分离 HEAD", remote="", **w) for w in trees if not w["branch"])
        return result

    def initialize_husky(self, path):
        path = Path(self.target(path))
        hooks = path / ".husky"
        if not hooks.is_dir() or not any(p.is_file() and "_/husky.sh" in p.read_text(errors="replace")
                                         for p in hooks.iterdir()):
            return
        runtime = hooks / "_" / "husky.sh"
        configured = git(path, "config", "--get", "core.hooksPath", allowed=(0, 1))[1].strip()
        if runtime.is_file() and configured:
            return
        message = "worktree 已保留，但 Husky 初始化未完成："
        ignore = hooks / "_" / ".gitignore"
        if hooks.is_symlink() or (hooks / "_").is_symlink() or runtime.is_symlink() or ignore.is_symlink():
            raise GuardError(message + "运行目录是符号链接，请先人工检查，不自动覆盖。")
        if ignore.exists() and ignore.read_text().strip() != "*":
            raise GuardError(message + "已有自定义 Husky 忽略文件，不自动覆盖。")
        shared = git(path, "config", "--local", "--get", "core.hooksPath", allowed=(0, 1))[1].strip()
        if configured not in ("", ".husky") or shared not in ("", ".husky"):
            raise GuardError(message + "已有自定义 core.hooksPath，未修改现有 hooks 配置。")
        if os.environ.get("HUSKY") == "0" or os.environ.get("HUSKY_SKIP_HOOKS"):
            raise GuardError(message + "当前环境禁用了 hooks，请恢复正常校验后重选工作区。")
        roots = [path, Path(self.root)]
        if Path(self.common).name == ".git":
            roots.append(Path(self.common).parent)
        package = next((root / "node_modules/husky" for root in roots
                        if (root / "node_modules/husky/package.json").is_file()), None)
        node = shutil.which("node")
        if package is None or node is None:
            raise GuardError(message + "缺少 Node 或仓库已安装的 Husky；请补齐现有项目依赖后重选工作区，不自动联网安装。")
        try:
            version = json.loads((package / "package.json").read_text())["version"]
            if not isinstance(version, str) or not version.startswith("8."):
                raise ValueError("当前自动初始化仅支持项目现有的 Husky 8")
            env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
            env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
            if Path(GIT).is_absolute():
                env["PATH"] = str(Path(GIT).parent) + os.pathsep + env.get("PATH", "")
            result = subprocess.run([node, str(package / "lib/bin.js"), "install"], cwd=path,
                                    env=env, capture_output=True, text=True, timeout=30)
            if result.returncode or not runtime.is_file():
                raise ValueError((result.stdout + result.stderr).strip()[-2000:] or "未生成 Husky 运行文件")
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            raise GuardError(message + str(error) + "；请检查依赖后重选工作区，不删除已创建的分支或 worktree。") from None
        if git(path, "config", "--get", "core.hooksPath", allowed=(0, 1))[1].strip() != ".husky":
            raise GuardError(message + "hooks 路径未生效，请人工检查。")

    def prepare_target(self, ref):
        self.resolver.guard()
        if self.common_dir(self.root) != self.common:
            raise GuardError("仓库归属已变化，请重启工具。")
        item = next((b for b in self.branches() if b["ref"] == ref and ref), None)
        if item is None:
            raise GuardError("目标分支不存在或已变化，请刷新列表。")
        branch = item["branch"]
        if item["path"]:
            self.initialize_husky(item["path"])
            return self.snapshot(item["path"])
        if ref.startswith("refs/remotes/") and not item["remote"]:
            raise GuardError("该远端引用没有对应的远端配置，无法创建目标分支。")
        directory = Path(self.root) / ".aidp" / "worktrees" / branch
        if (directory.exists() or directory.is_symlink() or
                any(p.is_symlink() for p in directory.parents if p != Path(self.root) and Path(self.root) in p.parents)):
            raise GuardError("目标目录已存在或父目录是符号链接，请先检查：" + str(directory))
        if any(Path(w["path"]) in directory.parents for w in self.worktrees() if w["path"] != self.root):
            raise GuardError("目标目录位于其他 worktree 内，请先检查目录安排。")
        self.plans.clear()
        # Existing branches must be passed directly; --no-track would try to create them again.
        args = ("--no-track", "-b", branch) if item["remote"] else ()
        git(self.root, "worktree", "add", *args, "--", str(directory), ref if item["remote"] else branch)
        if item["remote"]:
            git(directory, "branch", "--set-upstream-to=" + item["name"], branch)
        self.initialize_husky(str(directory))
        return self.snapshot(str(directory))

    def snapshot(self, path):
        path = self.target(path)
        _, branch = git(path, "symbolic-ref", "--quiet", "--short", "HEAD", allowed=(0, 1))
        branch = branch.strip()
        head = git(path, "rev-parse", "HEAD")[1].strip()
        status = git(path, "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none")[1]
        conflicts = git(path, "diff", "--name-only", "--diff-filter=U", "-z")[1].rstrip("\0").split("\0")
        operations = []
        for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer", "BISECT_LOG"):
            filename = git(path, "rev-parse", "--path-format=absolute", "--git-path", name)[1].removesuffix("\n")
            if Path(filename).exists():
                operations.append(name)
        upstream = ""
        upstream_ok = True
        if branch:
            raw = git(path, "for-each-ref", "--format=%(upstream)%00%(upstream:remotename)%00%(upstream:remoteref)", "refs/heads/" + branch)[1].strip()
            parts = raw.split("\0")
            upstream = parts[0]
            if upstream:
                upstream_ok = len(parts) == 3 and parts[1] not in ("", ".") and parts[2] == "refs/heads/" + branch
        update_reason = ("分离 HEAD：请选择已检出分支的 worktree。" if not branch else
                         "上游不是同名远端分支，请先按你的 Git 约定修正。" if not upstream_ok else
                         "正在进行 Git 操作：" + ", ".join(operations) if operations else
                         "存在暂存、未暂存或未跟踪文件，请先自行处理。" if status else "")
        return dict(path=path, branch=branch, head=head, status=status, upstream=upstream,
                    conflicts=[p for p in conflicts if p], operations=operations, blocked=update_reason, upstream_ok=upstream_ok,
                    update_blocked=update_reason, resolution=self.resolver.status(path))

    def overview(self):
        refs, branches = self.refs(), self.branches()
        commits = {item['ref']: item['sha'] for item in refs}
        for branch in branches:
            branch['sync'] = None
            local = commits.get(branch['ref']) if branch['ref'].startswith('refs/heads/') else None
            remote_ref = 'refs/remotes/origin/' + branch['branch']
            remote = commits.get(remote_ref)
            if not local or not remote:
                continue
            try:
                ahead, behind = 0, 0
                if local != remote:
                    counts = git(self.root, 'rev-list', '--left-right', '--count', local + '...' + remote)[1]
                    ahead, behind = map(int, counts.split())
                status = 'synced'
                if ahead and behind:
                    status = 'diverged'
                elif behind:
                    status = 'behind'
                elif ahead:
                    status = 'ahead'
                branch['sync'] = dict(status=status, ahead=ahead, behind=behind, ref=remote_ref)
            except (GuardError, ValueError) as error:
                branch['sync'] = dict(status='unknown', ref=remote_ref, error=str(error))
        return dict(root=self.root, worktrees=self.worktrees(), refs=refs, branches=branches)

    def release_context(self, path, force=False):
        if not isinstance(force, bool):
            raise GuardError('释放模式必须是布尔值。')
        self.batch.guard()
        self.resolver.guard()
        path = self.target(path)
        directory = Path(path)
        tree = next(w for w in self.worktrees() if str(Path(w['path']).resolve()) == path)
        if path == str(Path(self.root).resolve()) or not (directory / '.git').is_file():
            raise GuardError('主工作区及工具启动目录不能释放。')
        if directory.is_relative_to(Path.home() / '.codex' / 'worktrees'):
            raise GuardError('Codex 托管的 worktree 请在对应任务中归档，不能从这里释放。')
        if tree['locked'] or tree['prunable']:
            raise GuardError('worktree 已锁定或登记异常，请先检查，工具不会强制移除。')
        state = self.snapshot(path)
        if not state['branch'] or state['branch'] == 'master':
            raise GuardError('分离 HEAD 或 master 工作区不能在这里释放。')
        if state['status'] or state['operations']:
            raise GuardError('存在未提交改动、未跟踪文件或 Git 操作，请先保留并处理后再释放。')
        admin = Path(git(path, 'rev-parse', '--absolute-git-dir')[1].strip())
        if (admin / 'index.lock').exists():
            raise GuardError('Git 索引正在使用，请稍后重新预览。')
        entries = git(path, 'ls-files', '-v', '-z')[1].split('\0')
        if any(e and (e[0].islower() or e[0] == 'S') for e in entries):
            raise GuardError('索引存在隐藏改动的标记（assume-unchanged / skip-worktree），请先检查。')
        if any(e.startswith('160000 ') for e in git(path, 'ls-files', '--stage', '-z')[1].split('\0')):
            raise GuardError('包含子模块，请在终端检查和释放，避免丢失子模块本地文件。')
        if any(Path(w['path']).resolve().is_relative_to(directory) and Path(w['path']).resolve() != directory
               for w in self.worktrees()):
            raise GuardError('目录中包含其他 worktree，不能释放。')
        ignored = [p for p in git(path, 'ls-files', '--others', '--ignored', '--exclude-standard',
                                 '--directory', '-z')[1].split('\0') if p]
        generated = []
        if ignored and all(p in ('.husky/_/', '.husky/_/husky.sh', '.husky/_/.gitignore') for p in ignored):
            runtime = directory / '.husky' / '_'
            candidates = [directory, Path(self.root), Path(self.common).parent]
            for candidate in candidates:
                package = candidate / 'node_modules' / 'husky'
                try:
                    if (str(json.loads((package / 'package.json').read_text())['version']).startswith('8.') and
                            not any(p.is_symlink() for p in (directory / '.husky', runtime, *runtime.iterdir())) and
                            {p.name for p in runtime.iterdir()} == {'husky.sh', '.gitignore'} and
                            (runtime / 'husky.sh').read_bytes() == (package / 'husky.sh').read_bytes() and
                            (runtime / '.gitignore').read_text().strip() == '*'):
                        generated = ['.husky/_/husky.sh', '.husky/_/.gitignore']
                        break
                except (OSError, ValueError, KeyError):
                    continue
        if ignored and not generated and not force:
            raise GuardError('存在需自行保留的忽略文件，释放已阻止（不会自动备份或删除）：\n' + '\n'.join(ignored[:20]))
        inventory, fingerprint = self.ignored_release_inventory(directory, ignored) if force else ([], '')
        self.check_worktree_processes(directory)
        return dict(path=path, branch=state['branch'], head=state['head'], generated=generated,
                    admin=str(admin), registration=(directory / '.git').read_text(), force=force,
                    ignored=inventory, local_fingerprint=fingerprint)

    def ignored_release_inventory(self, directory, paths):
        digest = hashlib.sha256()
        summary = []
        total = 0
        deadline = time.monotonic() + 15
        for relative in paths:
            root = directory / relative.rstrip('/')
            pending = [root]
            count = 0
            kind = ''
            while pending:
                item = pending.pop()
                total += 1
                if total > 200_000 or time.monotonic() > deadline:
                    raise GuardError('本地文件过多，无法完成强行释放预览；请在终端检查处理。')
                if item.name.casefold() == '.git':
                    raise GuardError('忽略目录中包含嵌套 Git 仓库，不能强行释放：' + str(item.relative_to(directory)))
                info = item.lstat()
                link = os.readlink(item) if stat.S_ISLNK(info.st_mode) else ''
                item_kind = ('链接' if stat.S_ISLNK(info.st_mode) else '目录' if stat.S_ISDIR(info.st_mode)
                             else '文件' if stat.S_ISREG(info.st_mode) else '')
                if not item_kind:
                    raise GuardError('存在特殊本地文件，不能强行释放：' + str(item.relative_to(directory)))
                if item == root:
                    kind = item_kind
                digest.update(json.dumps([str(item.relative_to(directory)), info.st_mode, info.st_size,
                                          info.st_mtime_ns, info.st_ctime_ns, info.st_ino, link]).encode())
                count += 1
                if item_kind == '目录':
                    with os.scandir(item) as entries:
                        pending.extend(sorted((Path(e.path) for e in entries), reverse=True))
            summary.append(dict(path=relative, kind=kind, count=count))
        return summary, digest.hexdigest()

    def check_worktree_processes(self, directory):
        directory = directory.resolve()
        if platform.IS_WINDOWS:
            try:
                return platform.check_worktree_processes(directory)
            except ValueError as error:
                raise GuardError(str(error)) from error
        lsof = shutil.which('lsof')
        if not lsof:
            raise GuardError('无法检查工作区进程占用（缺少 lsof），未释放。')
        try:
            result = subprocess.run([lsof, '-nP', '-a', '-d', 'cwd', '-Fpn'],
                                    capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            raise GuardError('检查工作区进程占用失败，未释放。')
        if result.returncode not in (0, 1) or (result.returncode == 1 and not result.stdout):
            raise GuardError('无法确认工作区进程占用，未释放。')
        pid = ''
        for line in result.stdout.splitlines():
            if line.startswith('p'):
                pid = line[1:]
            elif line.startswith('n/') and Path(line[1:]).resolve().is_relative_to(directory):
                raise GuardError('工作区仍有进程占用（PID ' + pid + '），请先停止对应服务或终端。')

    def preview_release(self, path, force=False):
        context = self.release_context(path, force)
        identifier = secrets.token_urlsafe(24)
        self.release_plans = {k: v for k, v in self.release_plans.items() if time.monotonic() - v['time'] < 600}
        if len(self.release_plans) >= 100:
            self.release_plans.clear()
        self.release_plans[identifier] = dict(context=context, time=time.monotonic())
        return dict(plan=identifier, **{k: context[k] for k in ('path', 'branch', 'head', 'generated', 'force', 'ignored')})

    def release_worktree(self, identifier, confirmation=None):
        plan = self.release_plans.pop(identifier, None) if isinstance(identifier, str) else None
        if not plan or time.monotonic() - plan['time'] >= 600:
            raise GuardError('释放预览已失效，请重新预览。')
        context = plan['context']
        if context['force'] and confirmation is not True:
            raise GuardError('强行释放须在二次确认弹窗中确认删除本地文件，请重新预览。')
        if self.release_context(context['path'], context['force']) != context:
            raise GuardError('工作区已变化，请重新预览释放。')
        # Even force mode only bypasses ignored-file protection, never Git's last dirty/lock check.
        git(self.root, 'worktree', 'remove', '--', context['path'])
        self.plans.clear()
        self.commit_plans.clear()
        self.push_plans.clear()
        if (Path(context['path']).exists() or
                git(self.root, 'rev-parse', '--verify', 'refs/heads/' + context['branch'])[1].strip() != context['head']):
            raise GuardError('释放后的状态与预期不符，请刷新检查；不会自动重试或修改分支。')
        self.resolver.forget(context['path'])
        return dict(branch=context['branch'], head=context['head'], path=context['path'])

    def branch_delete_context(self, ref, force=False):
        if not isinstance(force, bool):
            raise GuardError('删除模式必须是布尔值。')
        self.batch.guard()
        self.resolver.guard()
        if self.common_dir(self.root) != self.common:
            raise GuardError('仓库归属已变化，请重启工具。')
        item = next((r for r in self.refs() if r['ref'] == ref and r['ref'].startswith('refs/heads/')), None)
        if not item:
            raise GuardError('只能删除当前仓库已存在的本地分支。')
        branch = item['name']
        if branch == 'master':
            raise GuardError('master 为受保护分支，不能删除。')
        occupied = [w['path'] for w in self.worktrees() if w['branch'] == branch]
        if occupied:
            raise GuardError('分支仍被 worktree 占用，请先释放对应工作区：\n' + '\n'.join(occupied))
        root = self.snapshot(self.root)
        if root['operations']:
            raise GuardError('启动工作区正在进行 Git 操作，请结束后再删除分支。')
        parts = git(self.root, 'for-each-ref', '--format=%(upstream)%00%(upstream:remotename)%00%(upstream:remoteref)', ref)[1].rstrip('\n').split('\0')
        upstream = parts[0]
        if upstream and (len(parts) != 3 or parts[1] in ('', '.') or parts[2] != 'refs/heads/' + branch):
            raise GuardError('上游不是同名远端分支，请先核对跟踪配置。')
        base, base_ref = root['head'], root['branch'] or '启动目录 HEAD'
        if upstream:
            code, value = git(self.root, 'rev-parse', '--verify', '--quiet', upstream + '^{commit}', allowed=(0, 1))
            if not code:
                base, base_ref = value.strip(), upstream
            elif not force:
                raise GuardError('同名上游引用不存在，无法检查安全删除；确定放弃本地提交时可使用强制删除。')
        if not force and git(self.root, 'merge-base', '--is-ancestor', item['sha'], base, allowed=(0, 1))[0]:
            raise GuardError('分支含尚未合入' + ('同名上游' if upstream else '当前分支') + '的提交，不能安全删除；请先合并或保留这些提交。')
        return dict(ref=ref, branch=branch, head=item['sha'], upstream=upstream, base=base, base_ref=base_ref, force=force,
                    root_head=root['head'], root_branch=root['branch'], root_path=root['path'],
                    config=hashlib.sha256(git(self.root, 'config', '--null', '--list')[1].encode()).hexdigest())

    def preview_branch_delete(self, ref, force=False):
        context = self.branch_delete_context(ref, force=force)
        identifier = secrets.token_urlsafe(24)
        self.branch_delete_plans = {k: v for k, v in self.branch_delete_plans.items() if time.monotonic() - v['time'] < 600}
        if len(self.branch_delete_plans) >= 100:
            self.branch_delete_plans.clear()
        self.branch_delete_plans[identifier] = dict(context=context, time=time.monotonic())
        result = dict(plan=identifier, **context)
        if force:
            span = context['base'] + '..' + context['head']
            count = int(git(self.root, 'rev-list', '--count', span)[1].strip())
            commits = git(self.root, 'log', '--format=%h %s', '--no-decorate', '--max-count=80', span, display=True)[1]
            truncated = count > 80 or '…输出已截断' in commits
            if truncated:
                commits += '\n…提交列表已截断，仅列出前 80 条以内；共 ' + str(count) + ' 条，请在终端检查完整记录。'
            result.update(count=count, commits=commits, truncated=truncated)
        return result

    def delete_branch(self, identifier, confirmation=None):
        plan = self.branch_delete_plans.pop(identifier, None) if isinstance(identifier, str) else None
        if not plan or time.monotonic() - plan['time'] >= 600:
            raise GuardError('删除分支预览已失效，请重新预览。')
        if confirmation is not True:
            raise GuardError('请先确认删除本地分支。')
        context = plan['context']
        if self.branch_delete_context(context['ref'], force=context['force']) != context:
            raise GuardError('分支状态已变化，请重新预览删除。')
        # shortcut: pause external Git writers between this recheck and deletion; add ref-lock validation if concurrent writes are needed.
        git(self.root, 'branch', '-D' if context['force'] else '-d', '--', context['branch'])
        if any(r['ref'] == context['ref'] for r in self.refs()):
            raise GuardError('删除后的分支状态与预期不符，请刷新检查，不会自动重试。')
        self.plans.clear()
        self.push_plans.clear()
        return dict(branch=context['branch'])

    def merged_cleanup_base(self):
        self.batch.guard()
        self.resolver.guard()
        if self.common_dir(self.root) != self.common:
            raise GuardError('仓库归属已变化，请重启工具。')
        root = self.snapshot(self.root)
        if root['operations']:
            raise GuardError('启动工作区正在进行 Git 操作，请结束后再清理分支。')
        ref = 'refs/remotes/origin/master'
        baseline = next((item for item in self.refs() if item['ref'] == ref), None)
        if not baseline:
            raise GuardError('缺少 origin/master 本地引用，请先点击右上角“刷新”，再检查已合并分支。')
        return root, baseline

    def merged_cleanup_context(self, ref, discard_ignored=False):
        if type(discard_ignored) is not bool:
            raise GuardError('忽略文件清理选项必须是布尔值。')
        root, baseline = self.merged_cleanup_base()
        item = next((r for r in self.refs() if r['ref'] == ref and r['ref'].startswith('refs/heads/')), None)
        if not item:
            raise GuardError('只能清理当前仓库已存在的本地分支。')
        branch = item['name']
        if branch in ('master', 'main', 'pre', 'test') or 'sentry' in branch.casefold():
            raise GuardError('环境分支和 Sentry 分支保留，不参与批量清理。')
        pinned = read_pinned(self.common) or []
        if ref in pinned or 'refs/remotes/origin/' + branch in pinned:
            raise GuardError('常用分支保留，请在单项操作中处理。')
        if branch == root['branch']:
            raise GuardError('工具启动工作区的当前分支保留。')
        if git(self.root, 'merge-base', '--is-ancestor', item['sha'], baseline['sha'], allowed=(0, 1))[0]:
            raise GuardError('尚未完整合入 origin/master，保留本地分支。')
        parts = git(self.root, 'for-each-ref', '--format=%(upstream)%00%(upstream:remotename)%00%(upstream:remoteref)', ref)[1].rstrip('\n').split('\0')
        upstream = parts[0]
        if upstream and (len(parts) != 3 or parts[1] in ('', '.') or parts[2] != ref):
            raise GuardError('上游不是同名远端分支，请先核对跟踪配置。')
        # Git branch -d also checks the same-name upstream, or the launch HEAD without one.
        delete_base = git(self.root, 'rev-parse', '--verify', upstream + '^{commit}')[1].strip() if upstream else root['head']
        if git(self.root, 'merge-base', '--is-ancestor', item['sha'], delete_base, allowed=(0, 1))[0]:
            raise GuardError('虽然已合入 origin/master，但普通删除的同名上游或启动目录 HEAD 尚未包含该分支；请在单项操作中检查。')
        trees = [tree for tree in self.worktrees() if tree['branch'] == branch]
        if len(trees) > 1:
            raise GuardError('分支被多个 worktree 占用，请先人工检查。')
        release = self.release_context(trees[0]['path'], force=discard_ignored) if trees else None
        return dict(ref=ref, branch=branch, head=item['sha'], upstream=upstream, delete_base=delete_base,
                    base_ref=baseline['ref'], base=baseline['sha'], root_path=root['path'],
                    root_head=root['head'], root_branch=root['branch'], force=discard_ignored, release=release)

    def merged_cleanup_list(self, discard_ignored=False):
        if type(discard_ignored) is not bool:
            raise GuardError('忽略文件清理选项必须是布尔值。')
        _, baseline = self.merged_cleanup_base()
        rows = []
        for branch in self.branches():
            if not branch['ref'].startswith('refs/heads/'):
                continue
            reason = ''
            try:
                self.merged_cleanup_context(branch['ref'], discard_ignored)
            except (GuardError, ValueError, OSError) as error:
                reason = str(error)
            rows.append(dict(**branch, reason=reason))
        return dict(root=self.root, base_ref=baseline['ref'], base=baseline['sha'], rows=rows)

    def preview_merged_cleanup(self, ref, discard_ignored=False):
        context = self.merged_cleanup_context(ref, discard_ignored)
        identifier = secrets.token_urlsafe(24)
        self.merged_cleanup_plans = {key: plan for key, plan in self.merged_cleanup_plans.items()
                                     if time.monotonic() - plan['time'] < 600}
        if len(self.merged_cleanup_plans) >= 100:
            self.merged_cleanup_plans.clear()
        self.merged_cleanup_plans[identifier] = dict(context=context, time=time.monotonic())
        release = context['release'] or {}
        return dict(plan=identifier, **{key: context[key] for key in
                    ('ref', 'branch', 'head', 'upstream', 'base_ref', 'base', 'root_path', 'force')},
                    path=release.get('path', ''), ignored=release.get('ignored', []), generated=release.get('generated', []))

    def cleanup_merged_branch(self, identifier, confirmation=None, discard_confirmation=None):
        plan = self.merged_cleanup_plans.pop(identifier, None) if isinstance(identifier, str) else None
        if not plan or time.monotonic() - plan['time'] >= 600:
            raise GuardError('已合并分支清理预览已失效，请重新预览。')
        context = plan['context']
        if confirmation is not True:
            raise GuardError('请先确认释放 worktree 并删除所选本地分支。')
        release = context['release']
        if release and release['ignored'] and discard_confirmation is not True:
            raise GuardError('清理忽略文件须单独二次确认，请重新预览。')
        if self.merged_cleanup_context(context['ref'], context['force']) != context:
            raise GuardError('分支、工作区或合并基准已变化，请重新预览。')
        result = dict(branch=context['branch'], head=context['head'], path=release['path'] if release else '',
                      released=False, deleted=False, status='failed', error='')
        try:
            if release:
                checked = self.preview_release(release['path'], force=context['force'])
                # Recheck the reviewed inventory too; preview_release must never silently renew changed files.
                if self.merged_cleanup_context(context['ref'], context['force']) != context:
                    raise GuardError('释放前现场已变化，请重新预览。')
                self.release_worktree(checked['plan'], confirmation=True)
                result['released'] = True
            root, baseline = self.merged_cleanup_base()
            if (baseline['sha'] != context['base'] or root['head'] != context['root_head'] or
                    root['branch'] != context['root_branch']):
                raise GuardError('释放后合并基准或启动工作区已变化，保留本地分支。')
            deletion = self.preview_branch_delete(context['ref'])
            if (deletion['head'] != context['head'] or deletion['upstream'] != context['upstream'] or
                    deletion['base'] != context['delete_base']):
                raise GuardError('释放后分支或同名上游已变化，保留本地分支。')
            self.delete_branch(deletion['plan'], confirmation=True)
            result.update(deleted=True, status='succeeded')
        except (GuardError, ValueError, OSError) as error:
            result['error'] = str(error)
        return result

    def fetch_remote(self):
        self.resolver.guard()
        self.plans.clear()
        if self.common_dir(self.root) != self.common:
            raise GuardError("仓库归属已变化，请重启工具。")
        git(self.root, "remote", "get-url", "origin")
        # Explicit mapping prevents custom fetch/prune settings from changing local branches or tags.
        _, output = git(self.root, "-c", "fetch.pruneTags=false", "-c", "remote.origin.pruneTags=false",
                        "fetch", "--prune", "--no-tags", "--no-recurse-submodules", "--refmap=",
                        "origin", "+refs/heads/*:refs/remotes/origin/*", display=True)
        return dict(output=output)

    def source(self, ref):
        item = next((r for r in self.refs() if r["ref"] == ref), None)
        if not item:
            raise GuardError("来源分支不存在或已变化，请刷新列表。")
        return git(self.root, "rev-parse", "--verify", item["sha"] + "^{commit}")[1].strip()

    def update_branch(self, path, branch, head, confirmed):
        self.batch.guard()
        self.resolver.guard()
        if confirmed is not True:
            raise GuardError('请先核对分支并明确确认更新。')
        state = self.snapshot(path)
        path = state['path']
        if state['update_blocked']:
            raise GuardError(state['update_blocked'])
        if state['branch'] != branch or state['head'] != head:
            raise GuardError('分支或 HEAD 已变化，请重新检查并确认更新。')
        remote = git(path, 'config', '--get-all', 'branch.' + branch + '.remote', allowed=(0, 1))[1].splitlines()
        merge = git(path, 'config', '--get-all', 'branch.' + branch + '.merge', allowed=(0, 1))[1].splitlines()
        ref = 'refs/heads/' + branch
        tracking = 'refs/remotes/origin/' + branch
        if (remote or merge) and (remote != ['origin'] or merge != [ref]):
            raise GuardError('更新只支持 origin 同名上游，请先检查跟踪配置。')
        urls = git(path, 'remote', 'get-url', '--all', 'origin')[1].splitlines()
        push_urls = git(path, 'remote', 'get-url', '--push', '--all', 'origin')[1].splitlines()
        if any(len(values) != 1 or not values[0] or values[0].startswith('-') for values in (urls, push_urls)):
            raise GuardError('origin 必须对应一个明确的远端地址，请检查 URL 配置。')
        specs = git(path, 'config', '--get-all', 'remote.origin.push', allowed=(0, 1))[1].splitlines()
        valid = {'HEAD:' + ref, branch + ':' + branch, branch + ':' + ref, ref + ':' + ref}
        if (any(spec not in valid for spec in specs) or
                git(path, 'config', '--type=bool', '--get', 'remote.origin.mirror', allowed=(0, 1))[1].strip() == 'true'):
            raise GuardError('推送引用配置不明确、异名或启用 mirror，请先检查。')
        config = git(path, 'config', '--null', '--list')[1]

        def recheck():
            self.batch.guard()
            self.resolver.guard()
            if self.snapshot(path) != state or git(path, 'config', '--null', '--list')[1] != config:
                raise GuardError('更新检查后分支、工作区或 Git 配置已变化；请重新检查，未合入远端提交。')
            if not git(path, 'symbolic-ref', '--quiet', tracking, allowed=(0, 1))[0]:
                raise GuardError('同名远端引用是符号引用，请先人工检查；更新已阻止。')
            admin = Path(git(path, 'rev-parse', '--absolute-git-dir')[1].strip())
            locks = (admin / 'index.lock', admin / 'HEAD.lock', Path(self.common) / 'packed-refs.lock',
                     Path(self.common) / 'config.lock', Path(self.common) / (ref + '.lock'))
            if any(lock.exists() for lock in locks):
                raise GuardError('Git 正在使用工作区或引用锁，请稍后重新检查。')
            flags = git(path, 'ls-files', '-v', '-z')[1].split('\0')
            if any(entry and (entry[0].islower() or entry[0] == 'S') for entry in flags):
                raise GuardError('索引存在 assume-unchanged / skip-worktree 标记，请先人工检查。')

        recheck()
        for plans in (self.plans, self.commit_plans, self.change_plans, self.resolution_plans,
                      self.push_plans, self.release_plans, self.branch_delete_plans):
            plans.clear()
        try:
            git(path, '-c', 'fetch.pruneTags=false', '-c', 'remote.origin.pruneTags=false',
                'fetch', '--no-prune', '--no-tags', '--no-recurse-submodules', '--refmap=',
                '--', 'origin', '+' + ref + ':' + tracking, display=True)
        except GuardError as error:
            raise GuardError(str(error).replace(urls[0], self.push_url_label(urls[0]))) from None
        recheck()
        sha = git(path, 'rev-parse', '--verify', tracking + '^{commit}')[1].strip()
        if sha == head:
            return dict(state=state, output='已与 origin 同名分支同步，无需更新。', status='unchanged', count=0)
        if not git(path, 'merge-base', '--is-ancestor', sha, head, allowed=(0, 1))[0]:
            count = int(git(path, 'rev-list', '--count', sha + '..' + head)[1])
            return dict(state=state, output=f'本地领先 origin 同名分支 {count} 个提交，已保留本地提交；未推送。',
                        status='ahead', count=count)
        if git(path, 'merge-base', '--is-ancestor', head, sha, allowed=(0, 1))[0]:
            raise GuardError('本地与 origin 同名分支已分叉，无法快进更新；请自行审查处理，不会自动合并或重置。')
        incoming = git(path, 'diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--name-only',
                       '--diff-filter=ACMT', '-z', head, sha, '--')[1].split('\0')
        tracked = set(git(path, 'ls-files', '-z')[1].split('\0'))
        for filename in filter(None, incoming):
            relative = Path(filename)
            for part in (relative, *relative.parents):
                if part == Path('.') or str(part) in tracked:
                    continue
                local = Path(path) / part
                if os.path.lexists(local) and (part == relative or local.is_symlink() or not local.is_dir()):
                    raise GuardError('更新将覆盖本地未跟踪或忽略路径，请先保留并处理：' + str(part))
        count = int(git(path, 'rev-list', '--count', head + '..' + sha)[1])
        recheck()
        self.resolver.forget(path)
        _, output = git(path, '-c', 'merge.autoStash=false', '-c', 'rerere.enabled=false',
                        '-c', 'submodule.recurse=false', 'merge', '--ff-only', '--no-autostash',
                        '--no-overwrite-ignore', '--', sha, display=True)
        after = self.snapshot(path)
        if after['branch'] != branch or after['head'] != sha:
            raise GuardError('更新结果未确认，请先核对当前分支和 HEAD，勿直接重复执行。')
        return dict(state=after, output=f'已从 origin 同名分支快进更新 {count} 个提交。\n' + output,
                    status='updated', count=count)

    def open_codex_conversation(self, path, branch):
        state = self.snapshot(path)
        if not isinstance(branch, str) or not branch or state['branch'] != branch:
            raise GuardError('工作区分支已变化，请刷新列表后重试。')
        self.resolver.guard()
        project_path = self.worktrees()[0]['path']
        if not Path(project_path).is_dir() or self.common_dir(project_path) != self.common:
            raise GuardError('项目主目录不可用或仓库归属已变化，请重新检查项目。')
        prompt = (f'请在已有工作区 {json.dumps(state["path"], ensure_ascii=False)} 中处理，'
                  f'先核对该目录当前分支为「{branch}」。\n'
                  f'打开「{branch}」，进行调整：')
        url = 'codex://new?' + urlencode({'path': project_path, 'prompt': prompt})
        try:
            open_codex(url)
        except (OSError, subprocess.SubprocessError):
            raise GuardError('无法打开 Codex，请检查本机桌面应用。') from None
        return dict(branch=branch)

    def preview(self, path, ref):
        state = self.snapshot(path)
        path = state["path"]
        sha = self.source(ref)
        if ref == "refs/heads/" + state["branch"]:
            raise GuardError("来源和目标不能是同一分支。")
        base_code, base = git(path, "merge-base", state["head"], sha, allowed=(0, 1))
        if base_code:
            raise GuardError("没有共同祖先，工具不支持合并无关历史。")
        count = int(git(path, "rev-list", "--count", state["head"] + ".." + sha)[1])
        commits = git(path, "log", "--max-count=80", "--format=%h %s", state["head"] + ".." + sha, display=True)[1]
        stat = git(path, "diff", "--no-ext-diff", "--no-textconv", "--stat", base.strip(), sha, "--", display=True)[1]
        patch = git(path, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--unified=3", base.strip(), sha, "--", display=True)[1]
        incoming = git(path, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "--diff-filter=ACMT", "-z", base.strip(), sha, "--")[1].split("\0")
        plan_id = secrets.token_urlsafe(24)
        self.plans = {k: v for k, v in self.plans.items() if time.monotonic() - v["time"] < 600}
        if len(self.plans) >= 100:
            self.plans.clear()
        self.plans[plan_id] = dict(path=path, ref=ref, sha=sha, state=state, incoming=incoming, time=time.monotonic())
        return dict(plan=plan_id, state=state, source=ref, sourceSha=sha, count=count,
                    commits=commits, stat=stat, patch=patch)

    @record_operation('merge', 'plans')
    def merge(self, plan_id):
        self.resolver.guard()
        plan = self.plans.pop(plan_id, None) if isinstance(plan_id, str) else None
        if not plan or time.monotonic() - plan["time"] >= 600:
            raise GuardError("预览已失效，请重新预览。")
        state = self.snapshot(plan["path"])
        if state["blocked"]:
            raise GuardError(state["blocked"])
        if state != plan["state"] or self.source(plan["ref"]) != plan["sha"]:
            raise GuardError("预览后分支或工作区已变化，请重新预览。")
        tracked = set(git(plan["path"], "ls-files", "-z")[1].split("\0"))
        # Git's ort strategy can overwrite ignored files despite --no-overwrite-ignore.
        for filename in filter(None, plan["incoming"]):
            relative = Path(filename)
            for part in (relative, *relative.parents):
                if part == Path(".") or str(part) in tracked:
                    continue
                local = Path(plan["path"]) / part
                if os.path.lexists(local) and (part == relative or local.is_symlink() or not local.is_dir()):
                    raise GuardError("来源将覆盖本地未跟踪或忽略路径，请先保留并处理：" + str(part))
        self.resolver.forget(plan["path"])
        # --no-ff also stops a fast-forward from moving HEAD before a separate commit.
        code, output = git(plan["path"], "-c", "merge.autoStash=false", "-c", "rerere.enabled=false",
                           "merge", "--no-commit", "--no-ff", "--no-autostash", "--no-overwrite-ignore",
                           "--", plan["sha"], allowed=(0, 1, 128), display=True)
        after = self.snapshot(plan["path"])
        kind = "conflicts" if after["conflicts"] else "pending" if "MERGE_HEAD" in after["operations"] else "unchanged" if code == 0 else "failed"
        if kind == "conflicts":
            if self.history:
                after["resolution"] = self.resolver.start(plan["path"], after,
                    on_update=lambda public: self.history.resolution(plan.get('_history_id'), public))
            else:
                after["resolution"] = self.resolver.start(plan["path"], after)
        return dict(kind=kind, output=output, state=after)

    def resolution_context(self, path):
        self.batch.guard()
        self.resolver.guard()
        state = self.snapshot(path)
        path = state['path']
        if not state['branch'] or not state['upstream_ok']:
            raise GuardError('只能确认已检出、上游同名的分支冲突修复。')
        if state['operations'] != ['MERGE_HEAD'] or not state['conflicts']:
            raise GuardError('没有待确认的合并冲突，请刷新状态后使用提交预览。')
        flags = git(path, 'ls-files', '-v', '-z')[1].split('\0')
        if any(entry and (entry[0].islower() or entry[0] == 'S') for entry in flags):
            raise GuardError('索引存在隐藏改动的标记，请先人工检查。')
        entries = git(path, 'ls-files', '-u', '-z')[1].split('\0')
        if any(entry and entry.partition(' ')[0] not in ('100644', '100755') for entry in entries):
            raise GuardError('冲突包含符号链接或子模块，请在终端检查和暂存。')
        files = sorted(state['conflicts'])
        content = self.resolution_content(path, files)
        paths = [':(top,literal)' + name for name in files]
        git(path, 'diff', '--check', 'HEAD', '--', *paths)
        return dict(path=path, branch=state['branch'], head=state['head'], files=files,
                    identity=self.resolver.identity(path), content=content,
                    fingerprint=self.resolver.repair_protected(path, files),
                    outside=self.resolver.protected(path, files))

    @staticmethod
    def resolution_content(path, files):
        content = []
        for name in files:
            relative = Path(name)
            if relative.is_absolute() or '..' in relative.parts:
                raise GuardError('冲突路径不在工作区内。')
            source = Path(path) / relative
            if any(part.is_symlink() for part in (source, *source.parents) if part != Path(path) and Path(path) in part.parents):
                raise GuardError('冲突路径或父目录是符号链接，请人工检查：' + name)
            try:
                mode = source.lstat().st_mode
            except FileNotFoundError:
                content.append((name, None, None))
                continue
            if not stat.S_ISREG(mode):
                raise GuardError('冲突结果不是普通文件，请人工检查：' + name)
            raw = source.read_bytes()
            if re.search(rb'^(?:<{7,} |\|{7,} |={7,}\r?$|>{7,} )', raw, re.MULTILINE):
                raise GuardError('仍有冲突标记，请先修复：' + name)
            content.append((name, mode, hashlib.sha256(raw).hexdigest()))
        return content

    def preview_resolution(self, path):
        context = self.resolution_context(path)
        paths = [':(top,literal)' + name for name in context['files']]
        stat_text = git(context['path'], 'diff', '--stat', 'HEAD', '--', *paths, display=True)[1]
        patch = git(context['path'], 'diff', '--no-ext-diff', '--no-textconv', '--no-renames',
                    'HEAD', '--', *paths, display=True)[1]
        if self.resolution_context(context['path']) != context:
            raise GuardError('生成预览期间内容已变化，请重新检查修复。')
        token = secrets.token_urlsafe(24)
        if len(self.resolution_plans) >= 100:
            self.resolution_plans.clear()
        self.resolution_plans[token] = dict(context=context, time=time.monotonic())
        return dict(plan=token, path=context['path'], branch=context['branch'], head=context['head'],
                    files=context['files'], stat=stat_text, patch=patch)

    def confirm_resolution(self, plan_id, confirmed=False):
        if confirmed is not True:
            raise GuardError('请先检查预览并明确确认暂存冲突文件。')
        plan = self.resolution_plans.pop(plan_id, None) if isinstance(plan_id, str) else None
        if not plan or time.monotonic() - plan['time'] >= 600:
            raise GuardError('修复预览已失效，请重新检查修复。')
        context = plan['context']
        if self.resolution_context(context['path']) != context:
            raise GuardError('预览后内容、索引或分支已变化，未暂存，请重新检查修复。')
        path, files = context['path'], context['files']
        git(path, 'add', '-A', '--', *[':(top,literal)' + name for name in files])
        if (self.resolver.identity(path) != context['identity'] or
                self.resolver.protected(path, files) != context['outside'] or
                self.resolution_content(path, files) != context['content'] or
                git(path, 'ls-files', '-u', '-z')[1]):
            raise GuardError('暂存期间现场已变化，已暂存内容保留；请检查状态，尚未提交。')
        git(path, 'diff', '--cached', '--check', '--', *[':(top,literal)' + name for name in files])
        if git(path, 'diff', '--no-ext-diff', '--no-textconv', '--quiet', '--', *[':(top,literal)' + name for name in files], allowed=(0, 1))[0]:
            raise GuardError('暂存期间文件再次变化，请重新检查；已暂存内容保留，尚未提交。')
        self.resolver.forget(path)
        for plans in (self.plans, self.commit_plans, self.push_plans, self.resolution_plans):
            plans.clear()
        return dict(state=self.snapshot(path), summary='已确认并暂存冲突文件，请检查全部暂存内容后单独提交。')

    def commit_context(self, path, mode="merge", require_staged=True):
        if mode not in ("merge", "worktree"):
            raise GuardError("提交模式无效，请重新检查。")
        if mode == "worktree":
            self.batch.guard()
        self.resolver.guard()
        state = self.snapshot(path)
        path, branch = state["path"], state["branch"]
        if not branch:
            raise GuardError("不能在分离 HEAD 上提交，请选择已检出分支。")
        if not state["upstream_ok"]:
            raise GuardError("上游不是同名远端分支，提交已阻止。")
        if mode == "merge":
            if state["conflicts"] or state["operations"] != ["MERGE_HEAD"]:
                raise GuardError("仅支持没有未解决冲突的待提交合并。")
            if git(path, "diff", "--no-ext-diff", "--no-textconv", "--quiet", allowed=(0, 1))[0]:
                raise GuardError("存在未暂存改动，请先处理后重新预览；工具不会自动 git add。")
        elif state["conflicts"] or state["operations"]:
            raise GuardError("存在冲突或进行中的 Git 操作，请先处理后检查普通提交。")
        remote = git(path, "for-each-ref", "--format=%(upstream:remotename)", "refs/heads/" + branch)[1].strip()
        if not remote and "origin" in git(path, "remote")[1].splitlines():
            remote = "origin"
        if remote:
            urls = git(path, "remote", "get-url", "--push", "--all", remote)[1].splitlines()
            if mode == "worktree" and (len(urls) != 1 or not urls[0] or urls[0].startswith("-")):
                raise GuardError("推送必须对应一个明确的远端地址，请检查 push URL 配置。")
            if mode == "worktree" and git(path, "config", "--type=bool", "--get", "remote." + remote + ".mirror", allowed=(0, 1))[1].strip() == "true":
                raise GuardError("推送配置启用了 mirror，请先检查；工具不执行推送。")
            specs = git(path, "config", "--get-all", "remote." + remote + ".push", allowed=(0, 1))[1].splitlines()
            valid = {"HEAD:refs/heads/" + branch, branch + ":" + branch,
                     branch + ":refs/heads/" + branch, "refs/heads/" + branch + ":refs/heads/" + branch}
            if any(spec not in valid for spec in specs):
                raise GuardError("推送引用配置目标不明确或不是同名分支，请先检查；工具不执行推送。")
        git(path, "diff", "--cached", "--check")
        parents = []
        working = {}
        if mode == "merge":
            merge_file = git(path, "rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD")[1].strip()
            parents = Path(merge_file).read_text().splitlines()
        else:
            if require_staged and not git(path, "diff", "--cached", "--quiet", allowed=(0, 1))[0]:
                raise GuardError("暂存区没有可提交的改动，请先选择文件暂存。")
            working = self.changed_files(path)
        context = dict(state=state, parents=parents,
                       index=hashlib.sha256(git(path, "ls-files", "--stage", "-z")[1].encode()).hexdigest(),
                       config=hashlib.sha256(git(path, "config", "--null", "--list")[1].encode()).hexdigest())
        if mode == "worktree":
            context.update(files=working["files"], working=working["fingerprint"])
        return context

    def changed_files(self, path):
        directory = Path(path)
        admin = Path(git(path, "rev-parse", "--absolute-git-dir")[1].strip())
        if (admin / "index.lock").exists():
            raise GuardError("Git 索引正在使用，请稍后重新读取改动。")
        flags = git(path, "ls-files", "-v", "-z")[1].split("\0")
        if any(entry and (entry[0].islower() or entry[0] == "S") for entry in flags):
            raise GuardError("索引存在 assume-unchanged / skip-worktree 标记，请先人工检查。")
        if any(entry.startswith("160000 ") for entry in git(path, "ls-files", "--stage", "-z")[1].split("\0")):
            raise GuardError("包含子模块，请在终端检查和提交。")
        raw = git(path, "status", "--porcelain=v1", "-z", "--no-renames", "--untracked-files=all",
                  "--ignore-submodules=none", literal=True)[1]
        files, digest = [], hashlib.sha256()
        for entry in filter(None, raw.split("\0")):
            name = entry[3:]
            if name.endswith("/"):
                raise GuardError("改动包含目录或嵌套 Git 仓库，不能自动暂存：" + name)
            parts = name.split("/")
            if (len(entry) < 4 or entry[2] != " " or not name or name.startswith("/") or
                    any(part in ("", ".", "..") or part.casefold() == ".git" for part in parts)):
                raise GuardError("改动包含不安全或非文件路径，请在终端检查。")
            try:
                name.encode("utf-8")
            except UnicodeEncodeError:
                raise GuardError("文件名不是有效 UTF-8，请在终端检查和提交。") from None
            source = directory / name
            for parent in source.parents:
                if parent == directory:
                    break
                if parent.is_symlink() or (parent / ".git").exists() or (parent / ".git").is_symlink():
                    raise GuardError("改动位于符号链接目录或嵌套 Git 仓库，不能自动暂存：" + name)
            if source.is_dir():
                raise GuardError("改动包含目录或嵌套 Git 仓库，不能自动暂存：" + name)
            content = hashlib.sha256()
            mode = 0
            if source.is_symlink():
                mode = source.lstat().st_mode
                content.update(os.fsencode(os.readlink(source)))
            elif source.exists():
                info = source.lstat()
                if not stat.S_ISREG(info.st_mode):
                    raise GuardError("改动包含特殊文件，不能自动暂存：" + name)
                mode = info.st_mode
                with source.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        content.update(chunk)
            digest.update(json.dumps([name, entry[:2], mode, content.hexdigest()], ensure_ascii=True).encode())
            index, worktree = entry[:2]
            untracked = entry[:2] == "??"
            files.append(dict(path=name, index=index, worktree=worktree,
                              staged=not untracked and index != " ", unstaged=not untracked and worktree != " ",
                              untracked=untracked))
        return dict(files=files, fingerprint=digest.hexdigest())

    def preview_changes(self, path):
        context = self.commit_context(path, "worktree", require_staged=False)
        identifier = secrets.token_urlsafe(24)
        self.change_plans = {key: value for key, value in self.change_plans.items()
                             if time.monotonic() - value["time"] < 600}
        if len(self.change_plans) >= 100:
            self.change_plans.clear()
        self.change_plans[identifier] = dict(context=context, time=time.monotonic())
        state = context["state"]
        return dict(plan=identifier, path=state["path"], branch=state["branch"], head=state["head"],
                    state=state, files=context["files"])

    def stage_changes(self, identifier, files, message):
        plan = self.change_plans.get(identifier) if isinstance(identifier, str) else None
        if not plan or time.monotonic() - plan["time"] >= 600:
            raise GuardError("改动检查已失效，请重新读取。")
        before = plan["context"]
        path = before["state"]["path"]
        message = commit_message(message, before["state"]["branch"], self.bibase)
        if (not isinstance(files, list) or any(not isinstance(name, str) for name in files) or
                len(files) != len(set(files)) or not set(files).issubset({entry["path"] for entry in before["files"]})):
            raise GuardError("只能暂存本次已检查的文件，请重新读取并选择。")
        if self.commit_context(path, "worktree", require_staged=False) != before:
            raise GuardError("检查后分支、索引、配置或文件内容已变化，请重新读取。")
        if not files and not any(entry["staged"] for entry in before["files"]):
            raise GuardError("请选择要暂存的文件，或先保留已有暂存改动。")
        self.change_plans.pop(identifier, None)
        selected = [entry["path"] for entry in before["files"]
                    if entry["path"] in files and (entry["unstaged"] or entry["untracked"])]
        if selected:
            try:
                git(path, "--literal-pathspecs", "add", "--", *selected)
            except GuardError as error:
                raise GuardError(str(error) + "\n暂存结果未确认，请检查暂存区后重新读取。") from None
        try:
            return self.preview_commit(path, message, mode="worktree")
        except GuardError as error:
            raise GuardError(str(error) + "\n已执行的暂存会保留，请检查暂存区后重新预览。") from None

    def merge_message(self, branch, path, head, parents):
        records = self.history.read()['items'] if self.history else []
        refs = self.refs()
        sources = []
        for sha in parents:
            recorded = {row['source'] for row in records if row['operation'] == 'merge'
                        and row['path'] == path and row['branch'] == branch and row['before_sha'] == head
                        and row['source_sha'] == sha and row['status'] in ('pending', 'conflicts') and row['source']}
            candidates = recorded or {ref['ref'] for ref in refs if ref['sha'] == sha}
            ref = next(iter(candidates)) if len(candidates) == 1 else ''
            sources.append((ref.removeprefix('refs/heads/').removeprefix('refs/remotes/') or sha[:12], sha))
        identifiers = set(re.findall(r"M-[0-9]{10}", branch)) if self.bibase else set()
        prefix = next(iter(identifiers)) + " " if len(identifiers) == 1 else ""
        source = '、'.join(name for name, _ in sources)
        target = branch
        if prefix:
            # Keep the target requirement ID once, as required by the commit hook.
            source, target = (text.replace(prefix.strip(), '本需求') for text in (source, target))
        target = target if len(target) <= 32 else target[:31] + '…'
        available = 108 - len('merge: ' + prefix + '合并  → ' + target)
        source = source if len(source) <= available else source[:available - 1] + '…'
        title = 'merge: ' + prefix + '合并 ' + source + ' → ' + target
        lines = ['目标分支：' + branch, '合并前 HEAD：' + head]
        for name, sha in sources:
            lines.extend(['来源分支：' + name, '来源提交：' + sha])
        span = [*parents, '^' + head, '--']
        count = int(git(path, 'rev-list', '--count', '--no-merges', *span)[1])
        commits = git(path, 'log', '--no-merges', '--max-count=20', '--format=%h %s', *span)[1].splitlines()
        lines.extend(['', '本次新增提交（不含合并提交）：' + str(count) + ' 条'])
        lines.extend('- ' + (text if len(text) <= 96 else text[:95] + '…') for text in commits)
        if count > len(commits):
            lines.append('…仅列出前 ' + str(len(commits)) + ' 条，请在合并预览中查看提交记录。')
        body = '\n'.join(line[i:i + 100] for line in lines for i in range(0, max(1, len(line)), 100))
        message = title + '\n\n' + body
        if len(message) > 4096:
            message = message[:4050] + '\n…说明已截断，请在合并预览中查看完整来源和提交。'
        return commit_message(message, branch, self.bibase)

    def preview_commit(self, path, message=None, mode="merge"):
        context = self.commit_context(path) if mode == "merge" else self.commit_context(path, mode)
        state = context["state"]
        path, branch = state["path"], state["branch"]
        message = (self.merge_message(branch, path, state['head'], context['parents']) if message is None and mode == "merge"
                   else commit_message(message, branch, self.bibase))
        identifier = secrets.token_urlsafe(24)
        self.commit_plans = {k: v for k, v in self.commit_plans.items() if time.monotonic() - v["time"] < 600}
        if len(self.commit_plans) >= 100:
            self.commit_plans.clear()
        self.commit_plans[identifier] = dict(context=context, message=message, mode=mode, time=time.monotonic())
        return dict(plan=identifier, mode=mode, path=path, branch=branch, head=state["head"], state=state, message=message,
                    warning="本次提交包含暂存区全部改动；取消预览会保留已暂存内容。" if mode == "worktree" else "",
                    files=git(path, "diff", "--cached", "--no-ext-diff", "--no-textconv", "--name-status", "--no-renames", display=True)[1],
                    stat=git(path, "diff", "--cached", "--no-ext-diff", "--no-textconv", "--stat", display=True)[1],
                    patch=git(path, "diff", "--cached", "--no-ext-diff", "--no-textconv", "--no-renames", display=True)[1])

    @record_operation('commit', 'commit_plans')
    def commit_merge(self, identifier):
        plan = self.commit_plans.pop(identifier, None) if isinstance(identifier, str) else None
        if not plan or time.monotonic() - plan["time"] >= 600:
            raise GuardError("提交预览已失效，请重新预览。")
        before = plan["context"]
        path = before["state"]["path"]
        mode = plan.get("mode", "merge")
        current = self.commit_context(path) if mode == "merge" else self.commit_context(path, mode)
        if current != before:
            raise GuardError("提交预览后分支、索引、配置或文件内容已变化，请重新预览。")
        if os.environ.get("HUSKY") == "0" or os.environ.get("HUSKY_SKIP_HOOKS"):
            raise GuardError("当前环境禁用了 Git hooks，请恢复后再提交。")
        expected_tree = git(path, "write-tree")[1].strip()
        current = self.commit_context(path) if mode == "merge" else self.commit_context(path, mode)
        if current != before:
            raise GuardError("提交前暂存内容已变化，请重新预览。")
        with tempfile.TemporaryDirectory(prefix="worktree-merge-commit-") as directory:
            filename = Path(directory) / "message.txt"
            filename.write_text(plan["message"] + "\n", encoding="utf-8")
            try:
                code, output = git(path, "commit", "--file", str(filename), allowed=(0, 1, 128), display=True)
            except GuardError as error:
                raise GuardError(str(error) + "\n提交结果未确认，请先刷新并检查 HEAD，不要直接重试。") from None
        state = self.snapshot(path)
        if state["head"] == before["state"]["head"]:
            raise GuardError("提交未完成，暂存内容已保留，请检查 hooks 输出后重新预览。\n" + output)
        sha = state["head"]
        parents = git(path, "rev-list", "--parents", "-n", "1", sha)[1].split()[1:]
        actual_message = git(path, "log", "-1", "--format=%B")[1].strip()
        try:
            if (code != 0 or state["branch"] != before["state"]["branch"] or state["operations"] or
                    parents != [before["state"]["head"], *before["parents"]] or
                    git(path, "rev-parse", sha + "^{tree}")[1].strip() != expected_tree):
                raise GuardError("提交后的分支、父提交或文件内容与预览不符。")
            commit_message(actual_message, state["branch"], self.bibase)
        except GuardError as error:
            raise GuardError("提交已创建 " + sha + "，但校验未通过：" + str(error) + " 未自动回退或修订，请人工检查。") from None
        self.plans.clear()
        self.resolver.forget(path)
        return dict(sha=sha, message=actual_message, output=output, state=self.snapshot(path))

    def read_history(self):
        result = self.history.read() if self.history else dict(items=[], warning='')
        rows = [row for row in result['items'] if row['operation'] in ('commit', 'push') and row['sha']]
        shas = {row['sha'] for row in rows if re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', row['sha'])}
        commits = {}
        if shas:
            try:
                fields = git(self.root, 'log', '--no-walk=unsorted', '--ignore-missing',
                             '--format=%H%x00%P%x00%B%x00', *sorted(shas), '--')[1].split('\0')
                if len(fields) % 3 != 1 or fields[-1].strip():
                    raise GuardError('提交详情格式异常。')
                for offset in range(0, len(fields) - 1, 3):
                    sha, parents, message = fields[offset:offset + 3]
                    sha, parents = sha.strip(), parents.split()
                    if sha not in shas or sha in commits or any(
                            not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', parent) for parent in parents):
                        raise GuardError('提交详情格式异常。')
                    commits[sha] = (parents, message.strip())
            except (GuardError, OSError):
                commits.clear()
        for row in rows:
            if row['sha'] not in commits:
                row['message_warning'] = '无法读取该结果提交的详情；对象可能已清理，历史操作状态仍保留。'
                continue
            parents, row['message'] = commits[row['sha']]
            if row['operation'] == 'commit' and not row['source'] and len(parents) == 2 and parents[0] == row['before_sha']:
                sources = {(item['source'], item['source_sha']) for item in result['items']
                           if item['operation'] == 'merge' and item['source'] and
                           item['path'] == row['path'] and item['branch'] == row['branch'] and
                           item['before_sha'] == parents[0] and item['source_sha'] == parents[1] and
                           item['started'] <= row['started']}
                if len(sources) == 1:
                    row['source'], row['source_sha'] = sources.pop()
        return result

    def push_context(self, path):
        self.resolver.guard()
        state = self.snapshot(path)
        if state['branch'] == 'master':
            raise GuardError('禁止推送 master，请选择其他分支。')
        if state['blocked']:
            raise GuardError(state['blocked'])
        path, branch = state['path'], state['branch']
        remote = git(path, 'for-each-ref', '--format=%(upstream:remotename)', 'refs/heads/' + branch)[1].strip() or 'origin'
        urls = git(path, 'remote', 'get-url', '--push', '--all', remote)[1].splitlines()
        if len(urls) != 1 or not urls[0] or urls[0].startswith('-'):
            raise GuardError('推送必须对应一个明确的远端地址，请检查 push URL 配置。')
        specs = git(path, 'config', '--get-all', 'remote.' + remote + '.push', allowed=(0, 1))[1].splitlines()
        valid = {'HEAD:refs/heads/' + branch, branch + ':' + branch,
                 branch + ':refs/heads/' + branch, 'refs/heads/' + branch + ':refs/heads/' + branch}
        if any(spec not in valid for spec in specs) or git(path, 'config', '--type=bool', '--get', 'remote.' + remote + '.mirror', allowed=(0, 1))[1].strip() == 'true':
            raise GuardError('推送引用配置不明确、异名或启用 mirror，请先检查。')
        if os.environ.get('HUSKY') == '0' or os.environ.get('HUSKY_SKIP_HOOKS'):
            raise GuardError('当前环境禁用了 Git hooks，请恢复后再推送。')
        return dict(state=state, remote=remote, url=urls[0], ref='refs/heads/' + branch,
                    config=hashlib.sha256(git(path, 'config', '--null', '--list')[1].encode()).hexdigest())

    @staticmethod
    def remote_tip(context):
        try:
            raw = git(context['state']['path'], 'ls-remote', '--heads', '--', context['url'], context['ref'])[1]
        except GuardError as error:
            raise GuardError(str(error).replace(context['url'], Repository.push_url_label(context['url']))) from None
        matches = [line.split() for line in raw.splitlines() if line.split()[-1:] == [context['ref']]]
        if not matches:
            return ''
        if len(matches) != 1 or not re.fullmatch(r'[0-9a-f]{40,64}', matches[0][0]):
            raise GuardError('无法核实远端分支，请稍后重新预览。')
        return matches[0][0]

    @staticmethod
    def push_url_label(url):
        return re.sub(r'(?<=//)[^/@]*@', '***@', url).split('?')[0].split('#')[0]

    def preview_push(self, path, allow_unchanged=False):
        context = self.push_context(path)
        state = context['state']
        tip = self.remote_tip(context)
        if tip:
            code = git(path, 'merge-base', '--is-ancestor', tip, state['head'], allowed=(0, 1, 128))[0]
            if code:
                raise GuardError('远端包含本地尚未整合的提交，请刷新远端引用并处理后重试；不会强制推送。')
            if tip == state['head'] and not allow_unchanged:
                raise GuardError('远端已是当前提交，无需推送。')
            revisions = [tip + '..' + state['head']]
        else:
            revisions = [state['head'], '--not', '--remotes']
        messages = git(path, 'log', '--first-parent', '--format=%s%x00', *revisions)[1]
        for header in messages.split('\0'):
            if header.strip():
                commit_message(header.strip(), state['branch'], self.bibase)
        count = git(path, 'rev-list', '--count', *revisions)[1].strip()
        commits = git(path, 'log', '-80', '--format=%h %s', *revisions, display=True)[1]
        identifier = secrets.token_urlsafe(24)
        self.push_plans = {k:v for k,v in self.push_plans.items() if time.monotonic() - v['time'] < 600}
        if len(self.push_plans) >= 100:
            self.push_plans.clear()
        self.push_plans[identifier] = dict(context=context, tip=tip, time=time.monotonic(), unchanged=tip == state['head'])
        return dict(plan=identifier, branch=state['branch'], head=state['head'], remote=context['remote'],
                    url=self.push_url_label(context['url']), ref=context['ref'], first=not tip,
                    count=int(count), commits=commits, unchanged=tip == state['head'])

    @record_operation('push', 'push_plans')
    def push(self, identifier):
        plan = self.push_plans.pop(identifier, None) if isinstance(identifier, str) else None
        if not plan or time.monotonic() - plan['time'] >= 600:
            raise GuardError('推送预览已失效，请重新预览。')
        context = plan['context']
        path = context['state']['path']
        if self.push_context(path) != context or self.remote_tip(context) != plan['tip']:
            raise GuardError('预览后本地或远端已变化，请重新预览；未执行推送。')
        if plan.get('unchanged'):
            return dict(sha=context['state']['head'], remote=context['remote'], ref=context['ref'],
                        output='远端已一致，无需推送。', state=self.snapshot(path), unchanged=True)
        args = ['push', '--porcelain', '--no-follow-tags', '--recurse-submodules=no']
        if not context['state']['upstream']:
            args.append('--set-upstream')
        try:
            code, output = git(path, *args, '--', context['remote'], 'HEAD:' + context['ref'], allowed=(0, 1, 128), display=True)
            output = output.replace(context['url'], self.push_url_label(context['url']))
            if code or self.remote_tip(context) != context['state']['head']:
                raise GuardError(output)
        except GuardError as error:
            raise GuardError(str(error).replace(context['url'], self.push_url_label(context['url'])) + '\n推送结果未确认，请核对远端后重新预览；不会自动重试或强推。') from None
        return dict(sha=context['state']['head'], remote=context['remote'], ref=context['ref'],
                    output=output, state=self.snapshot(path))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Request URLs/headers may contain session credentials.

    def headers_ok(self):
        origin = "http://127.0.0.1:" + str(self.server.server_port)
        if self.headers.get("Host") != origin.removeprefix("http://"):
            raise GuardError("非法 Host")
        if self.headers.get("Origin") not in (None, origin):
            raise GuardError("拒绝跨站请求")
        if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
            raise GuardError("拒绝跨站请求")

    def reply(self, status, data, content_type="application/json; charset=utf-8"):
        if status >= 400 and isinstance(data, dict):
            warning = getattr(getattr(getattr(self.server, 'repo', None), 'history', None), 'warning', '')
            if isinstance(warning, str) and warning:
                data = {**data, 'history_warning': warning}
        body = json.dumps(data, ensure_ascii=True).encode() if content_type.startswith("application/json") else data
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # Vue's local template compiler needs unsafe-eval; scripts/assets stay same-origin.
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            self.headers_ok()
            route = urlsplit(self.path)
            if route.path in ('/api/projects', '/api/projects/metadata', '/api/preferences'):
                if not secrets.compare_digest(self.headers.get('X-Session-Token', ''), self.server.token):
                    raise GuardError('会话失效，请重新连接工作台。')
                if route.path == '/api/preferences':
                    return self.reply(200, {'pinned': read_pinned(self.server.repo.common)})
                if route.path == '/api/projects':
                    return self.reply(200, self.project_listing())
                parameters = parse_qs(route.query, keep_blank_values=True)
                if any(len(value) != 1 for value in parameters.values()):
                    raise GuardError('元数据查询参数无效。')
                return self.reply(200, self.server.schedule.metadata({key: value[0] for key, value in parameters.items()}))
            assets = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/schedule.js": ("schedule.js", "text/javascript; charset=utf-8"),
                      "/batch.js": ("batch.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8"),
                      "/favicon.svg": ("favicon.svg", "image/svg+xml"),
                      "/vendor/vue.global.prod.js": ("vendor/vue.global.prod.js", "text/javascript; charset=utf-8")}
            if self.path not in assets:
                return self.reply(404, {"error": "页面不存在"})
            filename, mime = assets[self.path]
            self.reply(200, (WEB / filename).read_bytes(), mime)
        except (GuardError, ScheduleError, ValueError) as exc:
            self.reply(403, {"error": str(exc)})

    def project_listing(self):
        repo = self.server.repo
        listing = self.server.projects.list(repo.root)
        _, current = self.server.projects.current(repo.common)
        options = service_options(repo.root)
        return dict(**listing,
                    settings=dict(service={**options['defaults'], **current.get('service', {})},
                                  feishu={**(self.server.schedule.config if getattr(self.server, 'feishu_warning', '')
                                              else current.get('feishu', self.server.schedule.config)),
                                          'legacy_space_id': LEGACY_SPACE_ID}),
                    service_options=options,
                    feishu_warning=getattr(self.server, 'feishu_warning', ''),
                    rules=self.project_rules(repo.root, options))

    @staticmethod
    def project_rules(root, options):
        directory = Path(root)
        names = ('AGENTS.md', 'AGENT.md', 'CONTRIBUTING.md', 'README.md', '.commitlintrc', '.commitlintrc.json',
                 '.commitlintrc.js', 'commitlint.config.js', 'commitlint.config.cjs', '.husky/commit-msg')
        sources = []
        for name in names:
            path = directory / name
            if path.is_file() and not path.is_symlink():
                sources.append(name)
        return dict(commit='bibase' if options['preset'] == 'bibase' else 'project', sources=sources,
                    development='飞书前端需求审查（需关联飞书）')

    def project_action(self, repo, data):
        registry = self.server.projects
        if self.path == '/api/projects/add':
            result = registry.add(data.get('path'), data.get('name'))
        elif self.path == '/api/projects/update':
            identifier = data.get('id')
            entry = registry.registered(identifier, repo.root)
            if not entry:
                raise GuardError('项目已移出列表，请重新读取。')
            values = {key: data[key] for key in ('name', 'default') if key in data}
            if identifier == project_id(repo.common) and ('service' in data or 'feishu' in data):
                repo.batch.guard()
            if 'service' in data:
                values['service'] = validate_service_config(entry['path'], data['service'])
            if 'feishu' in data:
                if identifier == project_id(repo.common) and any(job['status'] == 'running' for job in self.server.development.status().values()):
                    raise GuardError('开发读取仍在运行，请结束后修改飞书关联。')
                validator = FeishuProject(config=entry.get('feishu', {}))
                try:
                    values['feishu'] = validator.validate_config(data['feishu'])
                finally:
                    validator.close()
            result = registry.update(identifier, values)
            if identifier == project_id(repo.common):
                if 'service' in values:
                    self.server.services.config = values['service']
                if 'feishu' in values:
                    self.server.schedule.close()
                    self.server.schedule = FeishuProject(config=values['feishu'])
                    self.server.development.schedule = self.server.schedule
                    self.server.feishu_warning = ''
            result = self.project_listing()
        elif self.path == '/api/projects/remove':
            result = registry.remove(data.get('id'), repo.root)
        elif self.path == '/api/projects/connect':
            repo.batch.guard()
            entry = registry.registered(data.get('id'), repo.root)
            if not entry:
                raise GuardError('项目未登记，请重新读取。')
            root, common = repository(entry['path'])
            if project_id(common) != data['id']:
                raise GuardError('项目目录的 Git 身份已变化，请移出后重新登记。')
            from launch_codex import launch
            requested = data.get('start', False)
            if type(requested) is not bool:
                raise GuardError('启动选择无效。')
            result = launch(root, common, requested)
        else:
            return self.reply(404, {'error': '接口不存在'})
        return self.reply(200, result)

    def do_POST(self):
        try:
            self.headers_ok()
            if not secrets.compare_digest(self.headers.get("X-Session-Token", ""), self.server.token):
                raise GuardError("会话失效，请使用终端显示的完整地址重新打开。")
            if self.headers.get("Content-Type") != "application/json":
                raise GuardError("请求格式错误")
            size = int(self.headers.get("Content-Length", "0"))
            limit = 131072 if self.path == '/api/task-links/unbind' else 8192
            if not 0 < size < limit:
                raise GuardError("请求体长度错误")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise GuardError("请求格式错误")
            repo = self.server.repo
            if getattr(self.server, 'restart_requested', False) is True and self.path != '/api/runtime':
                raise GuardError('工作台正在重启，请等待重新连接。')
            if self.path.startswith('/api/projects/'):
                return self.project_action(repo, data)
            if (getattr(repo, 'batch', None) and self.path not in
                    ('/api/runtime', '/api/batch/status', '/api/batch/cancel', '/api/overview', '/api/status', '/api/history', '/api/task-links', '/api/development/status', '/api/services/status', '/api/preferences') and
                    not self.path.startswith('/api/schedule/')):
                repo.batch.guard()
            if self.path == '/api/runtime':
                result = self.server.runtime.identity
            elif self.path in ('/api/runtime/shutdown', '/api/runtime/restart'):
                restart = self.path == '/api/runtime/restart'
                if data.get('confirmed') is not True:
                    raise GuardError('请明确确认' + ('重启' if restart else '关闭') + '工作台后台。')
                if restart and data.get('expected_identity') != self.server.runtime.identity['identity']:
                    raise GuardError('工作台实例已变化，请重新读取后确认。')
                repo.resolver.guard()
                if any(job['status'] == 'running' for job in self.server.development.status().values()):
                    raise GuardError('开发读取任务仍在运行，请等待结束后操作工作台。')
                if restart:
                    self.server.restart_requested = True
                try:
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                except (RuntimeError, OSError):
                    if restart:
                        self.server.restart_requested = False
                    raise GuardError('工作台停止任务未能启动，请检查后台状态后重试。') from None
                self.reply(200, {'status': 'restarting' if restart else 'stopping'})
                return
            elif self.path == '/api/preferences':
                if set(data) != {'pinned'}:
                    raise GuardError('常用分支请求格式无效。')
                result = {'pinned': write_pinned(repo.common, data['pinned'])}
            elif self.path == '/api/batch/preview':
                result = repo.batch.preview(data)
            elif self.path == '/api/batch/start':
                if any(job['status'] == 'running' for job in self.server.development.status().values()):
                    raise GuardError('开发读取任务仍在运行，请等待结束后开始批量合并。')
                result = repo.batch.start(data.get('plan'), data.get('auto_commit'))
            elif self.path == '/api/batch/push/preview':
                result = repo.batch.preview_push(data.get('id'))
            elif self.path == '/api/batch/push':
                result = repo.batch.push(data.get('plan'))
            elif self.path == '/api/batch/status':
                result = repo.batch.status()
            elif self.path == '/api/batch/cancel':
                result = repo.batch.cancel(data.get('id'))
            elif self.path == '/api/batch/repair':
                if any(job['status'] == 'running' for job in self.server.development.status().values()):
                    raise GuardError('开发读取任务仍在运行，请等待结束后修复。')
                result = repo.batch.repair(data.get('id'), data.get('index'))
            elif self.path == '/api/batch/resume/preview':
                result = repo.batch.preview_resume(data.get('id'))
            elif self.path == '/api/batch/resume':
                if any(job['status'] == 'running' for job in self.server.development.status().values()):
                    raise GuardError('开发读取任务仍在运行，请等待结束后继续批量合并。')
                result = repo.batch.resume(data.get('plan'))
            elif self.path == "/api/services/status":
                result = self.server.services.status(force=data.get('refresh') is True)
            elif self.path == "/api/services/start/preview":
                if not isinstance(self.server.services, BackendServices):
                    raise GuardError("此服务无需后端启动预览。")
                result = self.server.services.preview_start(data)
            elif self.path == "/api/services/start":
                result = self.server.services.start(data)
            elif self.path == "/api/services/stop/preview":
                result = self.server.services.preview_stop(data)
            elif self.path == "/api/services/stop":
                result = self.server.services.stop(data)
            elif self.path == "/api/development/start":
                self.server.schedule.ensure_enabled()
                result = self.server.development.start(data)
            elif self.path == "/api/development/status":
                result = self.server.development.status() if self.server.schedule.config.get('enabled') else {}
            elif self.path.startswith("/api/schedule/"):
                self.server.schedule.ensure_enabled()
                result = self.server.schedule.handle(self.path.removeprefix("/api/schedule/"), data)
            elif self.path == "/api/overview":
                result = repo.overview()
                current_id, current = self.server.projects.current(repo.common)
                feishu = self.server.schedule.config
                result['project'] = dict(id=current_id, name=current.get('name', Path(repo.root).name),
                                         feishu_enabled=feishu.get('enabled', False),
                                         space_id=feishu.get('space_id', ''), space_name=feishu.get('space_name', ''),
                                         project_url=feishu.get('project_url', ''))
            elif self.path == "/api/history":
                result = repo.read_history()
            elif self.path == "/api/task-links":
                self.server.schedule.ensure_enabled()
                result = dict(items=repo.history.tasks(self.server.schedule.space_id), warning=repo.history.warning)
            elif self.path == "/api/task-links/bind":
                self.server.schedule.ensure_enabled()
                identifier = FeishuProject.identifier(data.get('work_item_id'))
                ref, expected = data.get('ref'), data.get('expected_ref')
                if data.get('confirmed') is not True or not isinstance(ref, str) or not isinstance(expected, str):
                    raise GuardError('请在关联窗口核对任务与分支，并明确确认。')
                if ref and not any(branch['ref'] == ref and ref.startswith('refs/heads/') and
                                   branch['branch'] != 'master' for branch in repo.branches()):
                    raise GuardError('关联分支已不存在或不是可用的本地需求分支，请重新选择。')
                repo.history.bind_task(identifier, ref, expected, self.server.schedule.space_id)
                result = dict(items=repo.history.tasks(self.server.schedule.space_id), warning=repo.history.warning)
                if result['warning']:
                    raise GuardError(result['warning'])
            elif self.path == '/api/task-links/unbind':
                self.server.schedule.ensure_enabled()
                space_id = self.server.schedule.space_id
                items = data.get('items')
                missing_only = data.get('missing_only')
                if (data.get('confirmed') is not True or data.get('space_id') != space_id or
                        not isinstance(missing_only, bool) or not isinstance(items, list) or not 0 < len(items) <= 100):
                    raise GuardError('请重新预览当前空间的 1–100 项关联，并明确确认。')
                selected = []
                for item in items:
                    if not isinstance(item, dict):
                        raise GuardError('关联清单格式错误，请重新预览。')
                    identifier = FeishuProject.identifier(item.get('work_item_id'))
                    expected = item.get('expected_ref')
                    if (not isinstance(expected, str) or not expected.startswith('refs/heads/') or
                            expected == 'refs/heads/master' or len(expected) > 1024):
                        raise GuardError('关联清单包含无效的本地需求分支，请重新预览。')
                    selected.append((identifier, expected))
                if len({identifier for identifier, _ in selected}) != len(selected):
                    raise GuardError('关联清单包含重复需求，请重新选择。')
                outcomes = []
                for identifier, expected in selected:
                    try:
                        if self.server.schedule.space_id != space_id:
                            raise GuardError('飞书空间已变化，请重新预览。')
                        branch = next((branch for branch in repo.branches() if branch['ref'] == expected), None)
                        if missing_only and branch:
                            outcomes.append(dict(work_item_id=identifier, status='skipped', error='本地分支已存在，请重新检查。'))
                            continue
                        if branch and branch.get('path') and not branch.get('missing'):
                            outcomes.append(dict(work_item_id=identifier, status='skipped', error='分支仍有工作区，请在排期核对关联。'))
                            continue
                        repo.history.bind_task(identifier, '', expected, space_id)
                        outcomes.append(dict(work_item_id=identifier, status='succeeded', error=''))
                    except (ValueError, GuardError) as error:
                        status = 'skipped' if str(error).startswith('任务关联已变化') else 'failed'
                        outcomes.append(dict(work_item_id=identifier, status=status, error=str(error)))
                result = dict(results=outcomes, items=repo.history.tasks(space_id), warning=repo.history.warning)
            elif self.path == "/api/fetch":
                result = repo.fetch_remote()
            elif self.path == "/api/target":
                result = repo.prepare_target(data.get("ref"))
            elif self.path == '/api/branch/update':
                if any(job['status'] == 'running' for job in self.server.development.status().values()):
                    raise GuardError('开发读取任务仍在运行，请等待结束后更新分支。')
                if isinstance(self.server.services, BackendServices):
                    if data.get('branch') == 'master':
                        raise GuardError('master 不作为后端更新目标，请选择同名需求分支。')
                    self.server.services.guard_update(data.get('path'))
                result = repo.update_branch(data.get('path'), data.get('branch'), data.get('head'), data.get('confirmed'))
            elif self.path == '/api/codex/new':
                result = repo.open_codex_conversation(data.get('path'), data.get('branch'))
            elif self.path in ('/api/branch/merged/list', '/api/branch/merged/preview', '/api/branch/merged/delete'):
                if any(job['status'] == 'running' for job in self.server.development.status().values()):
                    raise GuardError('开发读取任务仍在运行，请等待结束后清理分支。')
                if self.path.endswith('/list'):
                    result = repo.merged_cleanup_list(data.get('discard_ignored', False))
                elif self.path.endswith('/preview'):
                    result = repo.preview_merged_cleanup(data.get('ref'), data.get('discard_ignored', False))
                else:
                    result = repo.cleanup_merged_branch(data.get('plan'), data.get('confirmation'), data.get('discard_confirmation'))
            elif self.path in ('/api/branch/delete/preview', '/api/branch/delete'):
                if any(job['status'] == 'running' for job in self.server.development.status().values()):
                    raise GuardError('开发读取任务仍在运行，请等待结束后删除分支。')
                options = {'force': data['force']} if 'force' in data else {}
                result = (repo.preview_branch_delete(data.get('ref'), **options) if self.path.endswith('/preview')
                          else repo.delete_branch(data.get('plan'), confirmation=data.get('confirmation')))
            elif self.path in ('/api/worktree/release/preview', '/api/worktree/release'):
                if any(job['status'] == 'running' for job in self.server.development.status().values()):
                    raise GuardError('开发读取任务仍在运行，请等待结束后释放工作区。')
                result = (repo.preview_release(data.get('path'), force=data.get('force', False)) if self.path.endswith('/preview')
                          else repo.release_worktree(data.get('plan'), confirmation=data.get('confirmation')))
            elif self.path == "/api/status":
                result = repo.snapshot(data.get("path"))
            elif self.path == "/api/preview":
                result = repo.preview(data.get("path"), data.get("ref"))
            elif self.path == '/api/conflicts/preview':
                result = repo.preview_resolution(data.get('path'))
            elif self.path == '/api/conflicts/resolve':
                development = getattr(self.server, 'development', None)
                if development and any(job['status'] == 'running' for job in development.status().values()):
                    raise GuardError('开发读取任务仍在运行，请等待结束后确认修复。')
                result = repo.confirm_resolution(data.get('plan'), data.get('confirmed'))
            elif self.path == "/api/changes":
                result = repo.preview_changes(data.get("path"))
            elif self.path == "/api/stage":
                result = repo.stage_changes(data.get("plan"), data.get("files"), data.get("message"))
            elif self.path == "/api/commit/preview":
                options = {"mode": data["mode"]} if "mode" in data else {}
                result = repo.preview_commit(data.get("path"), data.get("message"), **options)
            elif self.path == "/api/commit":
                result = repo.commit_merge(data.get("plan"))
            elif self.path == "/api/push/preview":
                result = repo.preview_push(data.get("path"))
            elif self.path == "/api/push":
                result = repo.push(data.get("plan"))
            elif self.path == "/api/merge":
                result = repo.merge(data.get("plan"))
            else:
                return self.reply(404, {"error": "接口不存在"})
            self.reply(200, result)
        except (GuardError, ScheduleError, ValueError, TypeError) as exc:
            self.reply(400, {"error": str(exc)})
        except Exception:
            message = ("排期操作异常，请先重新读取或打开飞书核对，避免重复保存。"
                       if self.path.startswith("/api/schedule/") else
                       "操作异常，请刷新状态并在终端检查仓库，避免重复执行。")
            self.reply(500, {"error": message})


def open_browser(url):
    try:
        if sys.platform == "darwin":
            subprocess.run(["/usr/bin/open", url], check=True, timeout=10)
            return
        if webbrowser.open(url, new=2):
            return
    except (webbrowser.Error, OSError, subprocess.SubprocessError):
        pass
    print("未能自动打开浏览器，请手动打开上方完整地址。", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Branch Desk · 本地开发工作台")
    parser.add_argument("--repo", required=True, help="Git 仓库或任一 worktree 的绝对路径")
    parser.add_argument("--port", type=int, default=0, help="默认自动分配空闲端口")
    parser.add_argument("--no-browser", action="store_true", help="由 Codex 启动器打开页面，不启动系统浏览器")
    args = parser.parse_args()
    restart_token = os.environ.pop('BRANCH_DESK_RESTART_TOKEN', '')
    if restart_token and not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', restart_token):
        raise WorkbenchRuntimeError('工作台重启会话无效，请重新启动。')
    if restart_token and platform.IS_WINDOWS:
        # Let the previous server exit before instance discovery checks for unregistered servers.
        psutil = platform.windows_psutil()
        try:
            psutil.Process(os.getppid()).wait(timeout=5)
        except psutil.NoSuchProcess:
            pass
        except (psutil.TimeoutExpired, psutil.AccessDenied):
            raise WorkbenchRuntimeError('旧工作台后台尚未退出，请通过启动器重新连接。') from None
    repo = Repository(Path(args.repo).expanduser().resolve())
    with RuntimeSession(repo.common) as runtime:
        # Serial request handling prevents this process from running concurrent merges.
        server = HTTPServer(("127.0.0.1", args.port), Handler)
        server.restart_requested = False
        try:
            registry = ProjectRegistry()
            registered = registry.add(repo.root)
            repo.history = OperationHistory(history_file(Path(__file__).resolve().parent), repo.common)
            server.repo, server.token, server.runtime = repo, restart_token or secrets.token_urlsafe(32), runtime
            server.projects = registry
            server.feishu_warning = ''
            try:
                server.schedule = FeishuProject(config=registered.get('feishu')) if 'feishu' in registered else FeishuProject(
                    config=None if not platform.IS_WINDOWS and service_options(repo.root)['preset'] == 'bibase' else {})
            except (ScheduleError, ValueError):
                server.schedule = FeishuProject(config={})
                server.feishu_warning = '保存的飞书配置无效；本次已停用飞书入口，请在项目设置重新验证关联。'
            server.development = DevelopmentLauncher(repo, server.schedule)
            service_class = BackendServices if service_options(repo.root).get('kind') == 'backend' else WorktreeServices
            server.services = service_class(repo, registered.get('service'))
            url = f"http://127.0.0.1:{server.server_port}/#{server.token}"
            runtime.publish(url)
            print("\nBranch Desk · 只监听本机，批量确认后可本地提交，不自动推送", flush=True)
            print("仓库：" + repo.root, flush=True)
            print(f"浏览器打开：{url}", flush=True)
            print("保持此终端打开，Ctrl+C 关闭。请勿在合并过程中从其他工具修改同一 worktree。\n", flush=True)
            if not args.no_browser:
                threading.Thread(target=open_browser, args=(url,), daemon=True).start()
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            repo.batch.close()
            if hasattr(server, 'development'):
                server.development.close()
            if hasattr(server, 'schedule'):
                server.schedule.close()
            repo.resolver.close()
            server.server_close()
    if server.restart_requested is True:
        environment = os.environ.copy()
        environment['BRANCH_DESK_RESTART_TOKEN'] = server.token
        command = [sys.executable, '-X', 'utf8', str(Path(__file__).resolve()), '--repo', repo.root,
                   '--port', str(server.server_port), '--no-browser']
        try:
            if platform.IS_WINDOWS:
                subprocess.Popen(command, env=environment, **platform.child_options())
            else:
                os.execve(sys.executable, command, environment)
        except OSError:
            raise WorkbenchRuntimeError('工作台重启启动失败，请通过启动器重新启动并连接。') from None


if __name__ == "__main__":
    try:
        main()
    except WorkbenchRuntimeError as error:
        raise SystemExit(str(error))
