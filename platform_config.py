"""Per-user Branch Desk project registry, kept outside application and repository files."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import threading
import sqlite3
import platform_support as platform


ROOT = platform.user_root()
FILE = ROOT / 'projects.json'
LOCK = threading.RLock()
GIT = '/usr/bin/git' if Path('/usr/bin/git').is_file() else 'git'


def repository(path):
    if not isinstance(path, str) or not path or len(path) > 4096 or not Path(path).is_absolute():
        raise ValueError('请选择有效的本地 Git 仓库目录。')
    try:
        result = subprocess.run([GIT, '-C', path, 'rev-parse', '--path-format=absolute', '--show-toplevel'],
                                capture_output=True, text=True, timeout=10)
        if result.returncode or result.stderr.strip():
            raise ValueError()
        root = str(Path(result.stdout.removesuffix('\n')).resolve(strict=True))
        common = subprocess.run([GIT, '-C', root, 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                                capture_output=True, text=True, timeout=10, check=True).stdout.removesuffix('\n')
        common = str(Path(common).resolve(strict=True))
    except (OSError, subprocess.SubprocessError, ValueError):
        raise ValueError('路径不是可访问的 Git 仓库或 worktree。') from None
    return root, common


def project_id(common):
    return hashlib.sha256(common.encode()).hexdigest()


def pinned_file(common):
    return ROOT / ('preferences-' + project_id(common) + '.json')


def read_pinned(common):
    if not _secure_root():
        return None
    path = pinned_file(common)
    try:
        info = path.lstat()
        if (not platform.private_file(path, info) or info.st_size > 500_000):
            raise ValueError()
        data = json.loads(path.read_text())
        pinned = data['pinned']
        if (data.get('version') != 1 or not isinstance(pinned, list) or len(pinned) > 100 or
                any(not isinstance(ref, str) or len(ref) > 1024 for ref in pinned)):
            raise ValueError()
        return pinned
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, KeyError):
        raise ValueError('常用分支配置无效或权限异常，请人工检查，未覆盖原配置。') from None


def write_pinned(common, pinned):
    if (not isinstance(pinned, list) or len(pinned) > 100 or
            any(not isinstance(ref, str) or not 0 < len(ref) <= 1024 or
                not ref.startswith(('refs/heads/', 'refs/remotes/')) for ref in pinned) or
            len(set(pinned)) != len(pinned)):
        raise ValueError('常用分支只能保存至多 100 个有效分支引用。')
    for ref in pinned:
        try:
            valid = subprocess.run([GIT, 'check-ref-format', ref], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5).returncode == 0
        except (OSError, subprocess.SubprocessError):
            raise ValueError('无法验证常用分支引用，原配置未修改。') from None
        if not valid:
            raise ValueError('常用分支包含无效的 Git 引用，原配置未修改。')
    with LOCK:
        read_pinned(common)
        try:
            _secure_root(create=True)
            fd, temporary = tempfile.mkstemp(prefix='.preferences-', dir=ROOT)
            try:
                platform.private_fd(fd)
                with os.fdopen(fd, 'w') as stream:
                    json.dump({'version': 1, 'pinned': pinned}, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, pinned_file(common))
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        except OSError:
            raise ValueError('常用分支保存失败，原配置未修改。') from None
    return pinned


def _secure_root(create=False):
    if create:
        platform.secure_mkdir(ROOT)
    try:
        info = ROOT.lstat()
    except FileNotFoundError:
        return False
    if not platform.private_directory(ROOT, info):
        raise ValueError('Branch Desk 用户配置目录权限异常，请人工检查。')
    return True


def _read():
    if not _secure_root():
        return {'version': 1, 'default_id': '', 'projects': {}}
    try:
        info = FILE.lstat()
        if not platform.private_file(FILE, info):
            raise ValueError()
        data = json.loads(FILE.read_text())
        projects = data['projects']
        if (data.get('version') != 1 or not isinstance(projects, dict) or len(projects) > 128 or
                any(not re.fullmatch(r'[0-9a-f]{64}', key) or not isinstance(item, dict) or
                    not isinstance(item.get('path'), str) or not Path(item['path']).is_absolute() or
                    not isinstance(item.get('name'), str) for key, item in projects.items()) or
                data.get('default_id', '') not in ('', *projects.keys())):
            raise ValueError()
        return data
    except FileNotFoundError:
        return {'version': 1, 'default_id': '', 'projects': {}}
    except (OSError, ValueError, TypeError, KeyError):
        raise ValueError('Branch Desk 项目配置无效或权限异常，请人工检查，未覆盖原配置。') from None


def _write(data):
    _secure_root(create=True)
    fd, temporary = tempfile.mkstemp(prefix='.projects-', dir=ROOT)
    try:
        platform.private_fd(fd)
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, FILE)
        FILE.chmod(0o600)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class ProjectRegistry:
    def list(self, current):
        root, common = repository(str(current)) if isinstance(current, Path) else repository(current)
        identifier = project_id(common)
        with LOCK:
            data = _read()
        current_entry = data['projects'].setdefault(identifier, {'path': root, 'name': Path(root).name})
        if current_entry['path'] != root:
            current_entry['path'] = root
        return dict(current_id=identifier, default_id=data['default_id'],
                    projects=[dict(id=key, **value) for key, value in data['projects'].items()])

    def add(self, path, name=None):
        root, common = repository(path)
        identifier = project_id(common)
        with LOCK:
            data = _read()
            entry = data['projects'].get(identifier, {})
            entry.update(path=root, name=self.name(name) if name is not None else entry.get('name', Path(root).name))
            data['projects'][identifier] = entry
            if not data['default_id']:
                data['default_id'] = identifier
            _write(data)
        return dict(id=identifier, **entry)

    def name(self, value):
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 80 or '\0' in value:
            raise ValueError('项目名称为 1 到 80 个字符。')
        return value.strip()

    def update(self, identifier, values):
        if not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{64}', identifier) or not isinstance(values, dict):
            raise ValueError('项目设置无效。')
        with LOCK:
            data = _read()
            if identifier not in data['projects']:
                raise ValueError('项目已移出列表，请重新读取。')
            entry = data['projects'][identifier]
            if 'name' in values:
                entry['name'] = self.name(values['name'])
            if 'service' in values:
                entry['service'] = values['service']
            if 'feishu' in values:
                entry['feishu'] = values['feishu']
            if values.get('default') is True:
                data['default_id'] = identifier
            _write(data)
            return dict(id=identifier, **entry)

    def remove(self, identifier, current):
        current_id = self.list(current)['current_id']
        if identifier == current_id:
            raise ValueError('请先切换到其他项目，再移出当前项目。')
        with LOCK:
            data = _read()
            if identifier not in data['projects']:
                raise ValueError('项目已移出列表，请重新读取。')
            del data['projects'][identifier]
            if data['default_id'] == identifier:
                data['default_id'] = current_id
            _write(data)
        return {'removed': identifier}

    def get(self, identifier):
        with LOCK:
            return _read()['projects'].get(identifier)

    def registered(self, identifier, current):
        listing = self.list(current)
        if identifier == listing['current_id']:
            return next(item for item in listing['projects'] if item['id'] == identifier)
        return self.get(identifier)

    def current(self, common):
        identifier = project_id(common)
        with LOCK:
            data = _read()
            entry = data['projects'].get(identifier, {})
        return identifier, entry


def history_file(application):
    """Copy the old SQLite state once, preserving the original if migration fails."""
    target = ROOT / 'operations.sqlite3'
    if target.exists() or target.is_symlink():
        _secure_root()
        info = target.lstat()
        if not platform.private_file(target, info):
            raise ValueError('Branch Desk 历史文件权限异常，请人工检查。')
        return target
    legacy = Path(application) / 'state' / 'operations.sqlite3'
    if not legacy.is_file():
        _secure_root(create=True)
        return target
    with LOCK:
        if target.exists() or target.is_symlink():
            return history_file(application)
        _secure_root(create=True)
        fd, temporary = tempfile.mkstemp(prefix='.operations-', dir=ROOT)
        os.close(fd)
        try:
            with sqlite3.connect(str(legacy)) as source, sqlite3.connect(temporary) as dest:
                source.backup(dest)
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    return target
