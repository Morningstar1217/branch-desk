"""Small JSON interface for the Windows shell; opening it never starts a service."""
import argparse
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import platform_support as platform
from platform_config import ProjectRegistry, _read, project_id, repository
from launch_codex import launch
from workbench_runtime import RuntimeError, status


def project_list():
    data = _read()
    return dict(status='ok', default_id=data['default_id'],
                projects=[dict(id=key, **value) for key, value in data['projects'].items()])


def shutdown_all():
    stopped, failures = [], []
    for item in project_list()['projects']:
        try:
            _, common = repository(item['path'])
            if project_id(common) != item['id']:
                raise ValueError('项目 Git 身份已变化')
            state = status(common, check_legacy=False)
            if state['status'] != 'running':
                continue
            url = urlsplit(state['url'])
            connection = http.client.HTTPConnection('127.0.0.1', url.port, timeout=10)
            try:
                connection.request('POST', '/api/runtime/shutdown', body=b'{"confirmed":true}',
                                   headers={'Content-Type': 'application/json', 'X-Session-Token': url.fragment})
                response = connection.getresponse()
                value = json.loads(response.read(8193))
                if response.status != 200:
                    raise ValueError(value.get('error', '后台拒绝退出'))
                stopped.append(item['name'])
            finally:
                connection.close()
        except (OSError, ValueError, RuntimeError, http.client.HTTPException) as error:
            failures.append(item['name'] + ': ' + str(error))
    return dict(status='error' if failures else 'ok', stopped=stopped,
                error='\n'.join(failures), message='已请求关闭空闲工作台后台；前后端开发服务继续运行。')


def install_check():
    """Never stop processes during installation; refuse replacement of a busy install."""
    root = Path(__file__).resolve().parents[2]
    psutil = platform.windows_psutil()
    active = []
    for process in platform.owned_processes():
        if process.pid in (os.getpid(), os.getppid()):
            continue
        try:
            if Path(process.exe()).resolve().is_relative_to(root):
                active.append(f'{process.name()} (PID {process.pid})')
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            raise ValueError('无法检查当前用户进程，请关闭工作台后重试。') from None
    if active:
        raise ValueError('请先在工作台“更多操作”菜单关闭后台，再退出所有窗口：' + ', '.join(active))
    return dict(status='ok')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('list', 'add', 'connect', 'shutdown', 'install-check'))
    parser.add_argument('--path')
    parser.add_argument('--id')
    parser.add_argument('--start', action='store_true')
    parser.add_argument('--confirmed', action='store_true')
    args = parser.parse_args()
    try:
        if args.action == 'list':
            value = project_list()
        elif args.action == 'add':
            value = dict(status='ok', project=ProjectRegistry().add(args.path))
        elif args.action == 'connect':
            entry = ProjectRegistry().get(args.id)
            if not entry:
                raise ValueError('项目未登记，请重新选择。')
            repo, common = repository(entry['path'])
            if project_id(common) != args.id:
                raise ValueError('项目 Git 身份已变化，请重新添加。')
            value = launch(repo, common, args.start)
        elif args.action == 'shutdown':
            if not args.confirmed:
                raise ValueError('请先明确确认关闭工作台后台。')
            value = shutdown_all()
        else:
            value = install_check()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        value = dict(status='error', error=str(error))
    print(json.dumps(value, ensure_ascii=False))
    return 1 if value['status'] == 'error' else 0


if __name__ == '__main__':
    raise SystemExit(main())
