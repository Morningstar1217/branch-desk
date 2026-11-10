"""Private local instance discovery shared by the server and Codex launcher."""
import platform_support as platform
if platform.IS_WINDOWS:
    import msvcrt
else:
    import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

APP = Path(__file__).resolve().parent
RUNTIME = platform.user_root() / 'runtime'


class RuntimeError(Exception):
    pass


def checked(path, flags):
    fd = os.open(path, flags | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    info = os.fstat(fd)
    if not platform.private_file(path, info):
        os.close(fd)
        raise RuntimeError('工作台运行文件的所有者或权限异常，请人工检查。')
    return fd


def directory(create=False):
    for path in (RUNTIME.parent, RUNTIME):
        if create:
            platform.secure_mkdir(path)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return False
        if (not platform.private_directory(path, info, strict=path == RUNTIME)):
            raise RuntimeError('工作台运行目录的所有者或权限异常，请人工检查。')
    return True


def instance_path(common, suffix):
    return RUNTIME / (hashlib.sha256(common.encode()).hexdigest() + suffix)


def lock_descriptor(fd):
    if platform.IS_WINDOWS:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno in (13, 36):
                raise BlockingIOError() from error
            raise
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


class FileLock:
    def __init__(self, path):
        self.path, self.fd = path, None

    def __enter__(self):
        self.fd = checked(self.path, os.O_CREAT | os.O_RDWR)
        try:
            lock_descriptor(self.fd)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            raise RuntimeError('该仓库的工作台已运行或正在启动，请稍后重新打开。') from None
        return self

    def __exit__(self, *_):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def lock_held(common):
    try:
        fd = checked(instance_path(common, '.lock'), os.O_RDWR if platform.IS_WINDOWS else os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        try:
            lock_descriptor(fd)
            return False
        except BlockingIOError:
            return True
    finally:
        os.close(fd)


def read_instance(common):
    try:
        with os.fdopen(checked(instance_path(common, '.json'), os.O_RDONLY)) as source:
            data = json.loads(source.read(8193))
        if (not isinstance(data, dict) or data.get('common') != common or data.get('app') != str(APP)
                or type(data.get('pid')) is not int or data['pid'] <= 1
                or not isinstance(data.get('identity'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{32}', data['identity'])
                or not isinstance(data.get('url'), str)):
            raise ValueError
        url = urlsplit(data['url'])
        if (url.scheme != 'http' or not re.fullmatch(r'127\.0\.0\.1:[0-9]{1,5}', url.netloc)
                or not url.port or url.path != '/' or url.query or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', url.fragment)):
            raise ValueError
        return data
    except FileNotFoundError:
        return None
    except (ValueError, TypeError):
        raise RuntimeError('工作台运行描述无效，请人工检查后重新启动。') from None


def process_alive(pid):
    if platform.IS_WINDOWS:
        return platform.process_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def legacy_process():
    # Old servers have no registry; refuse a second instance until the user restarts them.
    try:
        registered = set()
        if directory():
            for path in RUNTIME.glob('*.json'):
                with os.fdopen(checked(path, os.O_RDONLY)) as source:
                    candidate = json.loads(source.read(8193))
                common = candidate.get('common') if isinstance(candidate, dict) else None
                if not isinstance(common, str) or not Path(common).is_absolute() or path != instance_path(common, '.json'):
                    raise RuntimeError('工作台运行描述无效，请人工检查。')
                data = read_instance(common)
                if data and process_alive(data['pid']) and lock_held(common):
                    registered.add(data['pid'])
        if platform.IS_WINDOWS:
            return platform.legacy_process(APP, registered)
        output = subprocess.run(['/bin/ps', '-axo', 'uid=,pid=,command='], check=True,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5).stdout
        for line in output.splitlines():
            fields = line.strip().split(None, 2)
            if (len(fields) != 3 or fields[0] != str(os.getuid()) or fields[1] == str(os.getpid())
                    or int(fields[1]) in registered):
                continue
            command = fields[2]
            if not re.match(r'\S*python\S*\s', command, re.IGNORECASE) or not re.search(r'(?:^|[/\s])server\.py(?:\s|$)', command):
                continue
            if str(APP / 'server.py') in command:
                return True
            cwd = subprocess.run(['/usr/sbin/lsof', '-a', '-p', fields[1], '-d', 'cwd', '-Fn'],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5)
            paths = [line[1:] for line in cwd.stdout.splitlines() if line.startswith('n')]
            if not paths:
                raise RuntimeError('发现未登记的 Python 服务但无法确认目录，请先人工检查已有工作台。')
            if str(APP) in paths:
                return True
        return False
    except (OSError, ValueError, subprocess.SubprocessError):
        raise RuntimeError('无法核对已有工作台进程，请人工检查后再启动。') from None


def status(common, check_legacy=True):
    data = read_instance(common) if directory() else None
    if data and process_alive(data['pid']):
        url = urlsplit(data['url'])
        connection = None
        try:
            connection = http.client.HTTPConnection('127.0.0.1', url.port, timeout=2)
            connection.request('POST', '/api/runtime', body=b'{}', headers={
                'Content-Type': 'application/json', 'X-Session-Token': url.fragment})
            response = connection.getresponse()
            identity = json.loads(response.read(8193))
            expected = {key: data[key] for key in ('identity', 'common', 'app', 'pid')}
            if response.status != 200 or identity != expected:
                raise ValueError
        except (OSError, ValueError, http.client.HTTPException):
            raise RuntimeError('已有工作台正在忙碌或身份无法确认；请稍后重试，不会启动重复服务。') from None
        finally:
            if connection is not None:
                connection.close()
        return dict(status='running', url=data['url'], common=common, pid=data['pid'])
    if directory() and lock_held(common):
        raise RuntimeError('工作台正在启动或状态无法确认，请稍后重试。')
    if check_legacy and legacy_process():
        raise RuntimeError('发现尚未登记的旧版工作台。请先手动关闭旧服务并重新启动；不会自动停止它。')
    return dict(status='not_running', common=common)


class RuntimeSession:
    def __init__(self, common):
        self.common = str(Path(common).resolve(strict=True))
        self.identity = dict(identity=secrets.token_urlsafe(24), common=self.common,
                             app=str(APP), pid=os.getpid())
        self.lock = None

    def __enter__(self):
        directory(create=True)
        self.lock = FileLock(instance_path(self.common, '.lock'))
        self.lock.__enter__()
        try:
            if legacy_process():
                raise RuntimeError('发现尚未登记的旧版工作台，请先手动关闭旧服务并重新启动。')
        except BaseException:
            self.lock.__exit__()
            raise
        return self

    def publish(self, url):
        fd, temporary = tempfile.mkstemp(prefix='.instance-', dir=RUNTIME)
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump(dict(self.identity, url=url), output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, instance_path(self.common, '.json'))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def __exit__(self, *_):
        try:
            current = read_instance(self.common)
            if current and current['identity'] == self.identity['identity']:
                instance_path(self.common, '.json').unlink()
        finally:
            self.lock.__exit__()
