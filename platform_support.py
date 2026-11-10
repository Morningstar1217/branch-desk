"""Windows adapters; POSIX behavior stays in the existing platform implementations."""
from functools import lru_cache
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

IS_WINDOWS = sys.platform == 'win32'


def user_root():
    if IS_WINDOWS:
        base = os.environ.get('LOCALAPPDATA')
        if not base or not Path(base).is_absolute():
            raise ValueError('未找到当前用户的 LOCALAPPDATA，不能创建工作台配置。')
        return Path(base) / 'Branch Desk'
    return Path.home() / ('Library/Application Support/Branch Desk' if sys.platform == 'darwin' else '.config/branch-desk')


def windows_psutil():
    try:
        import psutil
        return psutil
    except ImportError:
        raise ValueError('Windows 进程组件缺失，请重新安装完整安装包。') from None


def _security_descriptor(path):
    """Read owner and DACL as SDDL without invoking a shell or trusting display names."""
    import ctypes
    from ctypes import wintypes
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    descriptor = ctypes.c_void_p()
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
                                           ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    code = advapi.GetNamedSecurityInfoW(str(path), 1, 5, None, None, None, None, ctypes.byref(descriptor))
    if code:
        raise OSError(code, '无法检查本机配置目录 ACL')
    output = wintypes.LPWSTR()
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                                                          wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.c_void_p]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    try:
        if not advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 5, ctypes.byref(output), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return output.value
    finally:
        if output:
            kernel.LocalFree(ctypes.cast(output, ctypes.c_void_p))
        kernel.LocalFree(descriptor)


@lru_cache(maxsize=1)
def current_sid():
    # whoami returns a numeric SID; no user-controlled command text is executed.
    import csv
    result = subprocess.run(['whoami.exe', '/user', '/fo', 'csv', '/nh'], capture_output=True,
                            text=True, encoding='utf-8', errors='replace', timeout=5, check=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    row = next(csv.reader(result.stdout.strip().splitlines()))
    if len(row) != 2 or not row[1].startswith('S-1-5-'):
        raise ValueError('无法确认 Windows 用户身份。')
    return row[1]


def private_windows_path(path):
    import re
    info = Path(path).lstat()
    if info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    descriptor = _security_descriptor(path)
    sid = current_sid()
    owner = re.search(r'O:(.*?)(?:G:|D:|S:|$)', descriptor)
    if not owner or owner[1] != sid or 'D:' not in descriptor:
        return False
    # OWNER RIGHTS refers to the owner, verified above as the current user.
    allowed = {sid, 'SY', 'BA', 'OW', 'S-1-3-4', 'S-1-5-18', 'S-1-5-32-544'}
    entries = re.findall(r'\(([^()]*)\)', descriptor)
    grants = []
    for entry in entries:
        fields = entry.split(';')
        if len(fields) != 6:
            return False
        if fields[0] == 'A':
            if fields[5] not in allowed:
                return False
            grants.append(fields[5])
        elif fields[0] != 'D':
            return False
    return sid in grants


def secure_mkdir(path):
    path = Path(path)
    if path.exists():
        return
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if IS_WINDOWS:
        sid = current_sid()
        subprocess.run(['icacls.exe', str(path), '/inheritance:r', '/grant:r',
                        '*' + sid + ':(OI)(CI)F', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F'],
                       check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
        if not private_windows_path(path):
            raise ValueError('配置目录 ACL 校验失败，未继续启动。')


def private_file(path, info):
    if not stat.S_ISREG(info.st_mode):
        return False
    return private_windows_path(path) if IS_WINDOWS else info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600


def private_directory(path, info, strict=True):
    if not stat.S_ISDIR(info.st_mode):
        return False
    return private_windows_path(path) if IS_WINDOWS else info.st_uid == os.getuid() and not info.st_mode & (0o077 if strict else 0o022)


def private_fd(fd):
    if not IS_WINDOWS:
        os.fchmod(fd, 0o600)


def child_options():
    return dict(creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if IS_WINDOWS else dict(start_new_session=True)


def windows_command(command):
    if not IS_WINDOWS:
        return command
    command = list(command)
    executable = shutil.which(command[0]) or command[0]
    if Path(executable).suffix.lower() not in ('.cmd', '.bat'):
        return [executable, *command[1:]]
    root = Path(executable).parent
    scripts = {'codex': root / 'node_modules/@openai/codex/bin/codex.js',
               'npx': root / 'node_modules/npm/bin/npx-cli.js',
               'npm': root / 'node_modules/npm/bin/npm-cli.js'}
    script = scripts.get(Path(executable).stem.lower())
    node = shutil.which('node')
    if not script or not script.is_file() or not node:
        raise ValueError('Windows 启动命令需要可执行程序或受支持的 Node CLI，请检查安装和 PATH。')
    return [node, str(script), *command[1:]]


def bash_binary():
    git = shutil.which('git')
    if git:
        root = Path(git).resolve().parent.parent
        for path in (root / 'bin/bash.exe', root / 'usr/bin/bash.exe'):
            if path.is_file():
                return str(path)
    raise ValueError('未找到 Git for Windows 的 Bash，请安装完整 Git for Windows。')


def owned_processes():
    psutil = windows_psutil()
    user = psutil.Process().username().casefold()
    for process in psutil.process_iter():
        try:
            if process.username().casefold() == user:
                yield process
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def service_inventory(java_main=None):
    psutil = windows_psutil()
    rows = []
    for process in owned_processes():
        try:
            name = process.name().lower()
            if name not in (('java.exe', 'javaw.exe') if java_main else ('node.exe',)):
                continue
            args = process.cmdline()
            match = java_main in args if java_main else any(
                value.replace('\\', '/').endswith(('/vite/bin/vite.js', '/.bin/vite')) for value in args)
            if not match:
                continue
            listeners = [conn for conn in process.net_connections(kind='tcp') if conn.status == psutil.CONN_LISTEN]
            ports = sorted({conn.laddr.port for conn in listeners})
            bindings = [dict(host=conn.laddr.ip, port=conn.laddr.port) for conn in listeners]
            mode = args[args.index('--mode') + 1] if '--mode' in args and args.index('--mode') + 1 < len(args) else 'development'
            rows.append(dict(pid=process.pid, cwd=str(Path(process.cwd()).resolve()), ports=ports, bindings=bindings, mode=mode))
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            raise ValueError('无法读取当前用户服务进程；请检查其启动账户，不会重复启动。') from None
    return rows


def process_identities(processes):
    psutil = windows_psutil()
    rows = []
    for item in sorted(processes, key=lambda row: row['pid']):
        try:
            process = psutil.Process(item['pid'])
            if str(Path(process.cwd()).resolve()) != str(Path(item['cwd']).resolve()):
                raise ValueError('服务工作区已变化。')
            identity = f'{process.create_time():.6f} ' + json.dumps(process.cmdline(), ensure_ascii=True)
            rows.append(dict(item, identity=identity))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            raise ValueError('无法确认进程身份，请重新检查。') from None
    return rows


def process_alive(pid):
    return windows_psutil().pid_exists(pid)


def terminate_tree(pid):
    psutil = windows_psutil()
    try:
        process = psutil.Process(pid)
        children = process.children(recursive=True)
        for child in reversed(children):
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        process.terminate()
    except psutil.NoSuchProcess:
        pass
    except psutil.AccessDenied:
        raise ValueError('停止进程失败：当前用户没有权限。') from None


def legacy_process(app, registered):
    psutil = windows_psutil()
    for process in owned_processes():
        try:
            if process.pid in registered or process.pid == os.getpid() or process.name().lower() not in ('python.exe', 'pythonw.exe'):
                continue
            args = process.cmdline()
            if any(Path(arg).name == 'server.py' for arg in args[1:]):
                if str(Path(process.cwd()).resolve()) == str(app) or str(app / 'server.py') in args:
                    return True
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            raise ValueError('无法确认已有工作台进程，请先人工检查。') from None
    return False


def check_worktree_processes(directory):
    psutil = windows_psutil()
    for process in owned_processes():
        try:
            if Path(process.cwd()).resolve().is_relative_to(directory):
                raise ValueError(f'工作区仍有进程占用（PID {process.pid}），请先停止对应服务或终端。')
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            raise ValueError('无法确认当前用户进程占用，未释放工作区。') from None
