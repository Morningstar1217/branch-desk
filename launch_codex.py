#!/usr/bin/env python3
"""Return a private workbench URL; only --start may start its local service."""
import argparse
import json
import os
import platform_support as platform
from pathlib import Path
import subprocess
import sys
import time
from workbench_runtime import (APP, FileLock, RuntimeError, checked, directory,
                               instance_path, status)


def repository(path):
    git = '/usr/bin/git' if Path('/usr/bin/git').is_file() else 'git'
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    values = []
    for flag in ('--show-toplevel', '--git-common-dir'):
        result = subprocess.run([git, '-C', str(Path(path).expanduser().resolve()), 'rev-parse', '--path-format=absolute', flag],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env, timeout=10)
        if result.returncode:
            raise RuntimeError('请指定有效的 Git 仓库或其已登记 worktree。')
        values.append(str(Path(result.stdout.removesuffix('\n')).resolve(strict=True)))
    return values


def launch(repo, common, start=False):
    result = status(common)
    if not start or result['status'] == 'running':
        return dict(result, repo=repo)
    directory(create=True)
    with FileLock(instance_path(common, '.start.lock')):
        result = status(common)
        if result['status'] == 'running':
            return dict(result, repo=repo)
        log_path = instance_path(common, '.log')
        with os.fdopen(checked(log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY), 'ab') as log:
            process = subprocess.Popen([sys.executable, '-X', 'utf8', str(APP / 'server.py'), '--repo', repo, '--no-browser'],
                                       cwd=APP, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                       close_fds=True, **platform.child_options())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                result = status(common, check_legacy=False)
                if result['status'] == 'running':
                    return dict(result, status='started', repo=repo)
            except RuntimeError:
                pass
            if process.poll() is not None:
                raise RuntimeError('工作台启动未完成，请检查本机私有日志：' + str(log_path))
            time.sleep(0.1)
        raise RuntimeError('工作台启动尚未确认，请稍后查询状态；不会再次启动。日志：' + str(log_path))


def main():
    parser = argparse.ArgumentParser(description='返回 Branch Desk 的 Codex 入口，不自动打开浏览器。')
    parser.add_argument('--repo', required=True)
    parser.add_argument('--start', action='store_true', help='明确允许在没有服务时启动后台工作台')
    args = parser.parse_args()
    try:
        repo, common = repository(args.repo)
        result = launch(repo, common, args.start)
        code = 0
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        result, code = dict(status='error', error=str(error)), 1
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == '__main__':
    sys.exit(main())
