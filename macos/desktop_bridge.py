#!/usr/bin/env python3
"""Connect the local desktop shell through the existing guarded launcher."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def connect(config, start=False, project_id=None):
    if not isinstance(config, dict):
        raise ValueError('桌面配置无效，请重新构建应用。')
    paths = []
    for key in ('application', 'python'):
        value = config.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError('桌面配置路径无效，请重新构建应用。')
        paths.append(Path(value).resolve(strict=True))
    application, python = paths
    sys.path.insert(0, str(application))
    from platform_config import ProjectRegistry, repository
    registry = ProjectRegistry()
    if project_id is not None:
        if not re.fullmatch(r'[0-9a-f]{64}', project_id):
            raise ValueError('项目标识无效。')
        entry = registry.get(project_id)
        if not entry:
            raise ValueError('项目未登记或已移出。')
        repo, common = repository(entry['path'])
        if __import__('hashlib').sha256(common.encode()).hexdigest() != project_id:
            raise ValueError('项目 Git 身份已变化。')
    else:
        default_id = registry.list(config['default_repo'])['default_id']
        entry = registry.get(default_id) if default_id else None
        repo, common = repository(entry['path'] if entry else config['default_repo'])
    launcher = application / 'launch_codex.py'
    if not launcher.is_file() or not python.is_file():
        raise ValueError('工作台、仓库或 Python 已移动，请重新构建应用。')
    env = os.environ.copy()
    if not isinstance(config.get('path'), str) or '\0' in config['path']:
        raise ValueError('桌面命令路径无效，请重新构建应用。')
    env['PATH'] = config['path']
    if config.get('codex_home'):
        if not isinstance(config['codex_home'], str) or not Path(config['codex_home']).is_absolute():
            raise ValueError('Codex 配置目录无效，请重新构建应用。')
        env['CODEX_HOME'] = config['codex_home']
    command = [str(python), str(launcher), '--repo', repo]
    if start:
        command.append('--start')
    result = subprocess.run(command, cwd=application, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=25)
    try:
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or value.get('status') not in ('running', 'started', 'not_running', 'error'):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError('工作台启动器未返回有效状态；请检查本机安装，不会再次启动。') from None
    if result.returncode and value.get('status') != 'error':
        raise ValueError('工作台连接结果不确定，请检查已有服务，不会自动重试。')
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--start', action='store_true')
    parser.add_argument('--project-id')
    args = parser.parse_args()
    try:
        config = json.loads(Path(args.config).read_text())
        value = connect(config, args.start, args.project_id)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        value = dict(status='error', error=str(error) if not isinstance(error, subprocess.TimeoutExpired)
                     else '连接超时，请稍后重新连接；不会自动重试或停止已有后台。')
    print(json.dumps(value, ensure_ascii=False))
    return 1 if value['status'] == 'error' else 0


if __name__ == '__main__':
    raise SystemExit(main())
