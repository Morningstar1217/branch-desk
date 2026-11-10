"""Bounded local operation metadata; never persist Git output or credentials."""
from functools import wraps
from contextlib import closing, contextmanager
import hashlib
from pathlib import Path
import secrets
import re
import sqlite3
import threading
import time

LEGACY_SPACE_ID = '62983005bc43adc6923d707d'


def thread_url(value):
    return value if isinstance(value, str) and re.fullmatch(r'codex://threads/[A-Za-z0-9_-]{1,128}', value) else ''


def failure_detail(message):
    # Persist controlled diagnostics, never raw hook output, source text or remote credentials.
    text = str(message).lower()
    for words, detail in (
        (('提交已创建',), '提交已经创建，但后续校验未通过；请检查 HEAD、提交内容和说明，不要重复提交。'),
        (('超时', 'timeout', 'timed out'), '操作超时，写入结果未确认；请重新读取 Git 状态并核对远端，勿直接重试。'),
        (('未找到 codex',), '未找到 Codex CLI，未能启动冲突处理；冲突文件已保留。'),
        (('禁用了 git hooks',), '当前环境禁用了 Git hooks，操作已阻止；恢复 hooks 后重新预览。'),
        (('hook', '提交未完成'), 'Git hooks 或提交检查未通过；请检查 hooks 输出和暂存内容，再重新预览。'),
        (('预览', '已变化', '失效'), '预览已失效或分支、索引、配置发生变化；请重新读取状态并生成预览。'),
        (('覆盖本地', '忽略路径'), '来源可能覆盖本地未跟踪或忽略文件；请先保留并处理这些文件。'),
        (('上游', '推送引用', 'push url', 'mirror'), '上游或推送配置不符合单一同名分支要求；请核对配置后重新预览。'),
        (('未提交', '未暂存', '暂存、未暂存'), '工作区存在未提交或未暂存改动；请先检查并保留现场。'),
        (('冲突', 'conflict', '残留标记'), '冲突尚未解决或冲突检查未通过；请打开本次 Codex 会话并检查冲突文件。'),
        (('范围外', '保护检查', '非冲突', 'head 变化'), '冲突处理后的分支或文件范围检查未通过；请检查现场，未自动回退。'),
        (('authentication', 'permission denied', 'could not resolve', 'unable to access', '连接'), '远端连接或认证失败，推送结果未确认；请核对网络、权限和远端状态。'),
        (('rejected', 'non-fast-forward', '远端包含'), '远端拒绝推送或包含未整合提交；请重新核对远端，不要强制推送。'),
        (('master', '分离 head'), '目标为受保护分支或分离 HEAD，操作已阻止；请选择有效需求分支。'),
    ):
        if any(word in text for word in words):
            return detail
    return '操作执行或结果校验未完成；请检查当前 Git 状态及原始输出，勿直接重复执行。'


class OperationHistory:
    def __init__(self, filename, repository):
        self.filename = Path(filename)
        self.repository = hashlib.sha256(repository.encode()).hexdigest()
        self.lock = threading.RLock()
        self.warning = ''
        try:
            self.filename.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self.connect() as db:
                db.execute('''CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, repository TEXT, operation TEXT, started REAL,
                    finished REAL, path TEXT, branch TEXT, source TEXT, source_sha TEXT,
                    before_sha TEXT, sha TEXT, status TEXT)''')
                columns = {row[1] for row in db.execute('PRAGMA table_info(operations)')}
                for name in ('detail', 'thread_url', 'resolution_status', 'resolution_detail'):
                    if name not in columns:
                        db.execute('ALTER TABLE operations ADD COLUMN ' + name + " TEXT NOT NULL DEFAULT ''")
                db.execute('''CREATE TABLE IF NOT EXISTS task_links (
                    repository TEXT, space_id TEXT NOT NULL DEFAULT '', task_id TEXT,
                    branch_ref TEXT NOT NULL DEFAULT '', thread_url TEXT NOT NULL DEFAULT '',
                    previous_url TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (repository, space_id, task_id))''')
                columns = {row[1] for row in db.execute('PRAGMA table_info(task_links)')}
                primary = tuple(row[1] for row in sorted(db.execute('PRAGMA table_info(task_links)'), key=lambda row: row[5]) if row[5])
                if 'space_id' not in columns or primary != ('repository', 'space_id', 'task_id'):
                    db.execute('ALTER TABLE task_links RENAME TO task_links_legacy')
                    db.execute('''CREATE TABLE task_links (
                        repository TEXT, space_id TEXT NOT NULL DEFAULT '', task_id TEXT,
                        branch_ref TEXT NOT NULL DEFAULT '', thread_url TEXT NOT NULL DEFAULT '',
                        previous_url TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '',
                        PRIMARY KEY (repository, space_id, task_id))''')
                    legacy_space = 'space_id' if 'space_id' in columns else "'" + LEGACY_SPACE_ID + "'"
                    db.execute('INSERT OR IGNORE INTO task_links '
                               '(repository, space_id, task_id, branch_ref, thread_url, previous_url, status) '
                               'SELECT repository, ' + legacy_space + ', task_id, branch_ref, thread_url, previous_url, status FROM task_links_legacy')
                    db.execute('DROP TABLE task_links_legacy')
                db.execute("UPDATE operations SET status='interrupted', finished=? "
                           "WHERE repository=? AND status='running'", (time.time(), self.repository))
                db.execute("UPDATE operations SET resolution_status='interrupted' "
                           "WHERE repository=? AND resolution_status='running'", (self.repository,))
                db.execute("UPDATE task_links SET status='interrupted' WHERE repository=? AND status='running'", (self.repository,))
            self.filename.chmod(0o600)
        except (OSError, sqlite3.Error):
            self.unavailable()

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(str(self.filename), timeout=2)) as db:
            with db:
                yield db

    def unavailable(self):
        self.warning = '本地记录暂不可持久化，操作记录或任务关联可能缺失。请以 Git 实际状态为准，勿直接重复操作。'

    def start(self, operation, state, source='', source_sha=''):
        if self.warning:
            return None
        identifier = secrets.token_hex(16)
        try:
            with self.lock, self.connect() as db:
                db.execute('INSERT INTO operations (id, repository, operation, started, finished, path, branch, '
                           'source, source_sha, before_sha, sha, status) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)',
                           (identifier, self.repository, operation, time.time(), state['path'],
                            state['branch'], source, source_sha, state['head'], '', 'running'))
                db.execute('DELETE FROM operations WHERE repository=? AND id NOT IN '
                           '(SELECT id FROM operations WHERE repository=? ORDER BY started DESC LIMIT 200)',
                           (self.repository, self.repository))
            return identifier
        except (OSError, sqlite3.Error):
            self.unavailable()

    def finish(self, identifier, status, sha='', detail=''):
        if not identifier:
            return
        try:
            with self.lock, self.connect() as db:
                db.execute('UPDATE operations SET finished=?, status=?, sha=?, detail=? WHERE id=? AND repository=?',
                           (time.time(), status, sha, detail, identifier, self.repository))
        except (OSError, sqlite3.Error):
            self.unavailable()

    def resolution(self, identifier, public):
        if not identifier:
            return
        status = public.get('status', '')
        if status not in ('running', 'succeeded', 'failed'):
            return
        detail = failure_detail(public.get('summary', '')) if status == 'failed' else ''
        try:
            with self.lock, self.connect() as db:
                db.execute('UPDATE operations SET thread_url=?, resolution_status=?, resolution_detail=? '
                           'WHERE id=? AND repository=?',
                           (thread_url(public.get('url')), status, detail, identifier, self.repository))
        except (OSError, sqlite3.Error):
            self.unavailable()

    def tasks(self, space_id=LEGACY_SPACE_ID):
        if self.warning:
            return {}
        try:
            with self.lock, self.connect() as db:
                db.row_factory = sqlite3.Row
                return {row['task_id']: dict(row) for row in db.execute(
                    'SELECT task_id, branch_ref, thread_url, previous_url, status FROM task_links WHERE repository=? AND space_id=?',
                    (self.repository, space_id))}
        except (OSError, sqlite3.Error):
            self.unavailable()
            return {}

    def bind_task(self, task_id, branch_ref, expected, space_id=LEGACY_SPACE_ID):
        if self.warning:
            raise ValueError(self.warning)
        try:
            with self.lock, self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current = db.execute('SELECT branch_ref FROM task_links WHERE repository=? AND space_id=? AND task_id=?',
                                     (self.repository, space_id, task_id)).fetchone()
                if (current[0] if current else '') != expected:
                    raise ValueError('任务关联已变化，请重新打开关联窗口后确认。')
                db.execute('INSERT INTO task_links (repository, space_id, task_id, branch_ref) VALUES (?, ?, ?, ?) '
                           'ON CONFLICT(repository, space_id, task_id) DO UPDATE SET branch_ref=excluded.branch_ref',
                           (self.repository, space_id, task_id, branch_ref))
        except (OSError, sqlite3.Error):
            self.unavailable()
            raise ValueError(self.warning) from None

    def remember_development(self, public, space_id=LEGACY_SPACE_ID):
        identifier = public.get('work_item_id', '')
        status = public.get('status', '')
        if self.warning or not re.fullmatch(r'[0-9]{1,20}', identifier) or status not in ('running', 'finished', 'blocked', 'failed'):
            return
        try:
            with self.lock, self.connect() as db:
                db.execute('INSERT INTO task_links (repository, space_id, task_id, thread_url, previous_url, status) VALUES (?, ?, ?, ?, ?, ?) '
                           'ON CONFLICT(repository, space_id, task_id) DO UPDATE SET thread_url=excluded.thread_url, '
                           'previous_url=excluded.previous_url, status=excluded.status',
                           (self.repository, space_id, identifier, thread_url(public.get('url')), thread_url(public.get('previous_url')), status))
        except (OSError, sqlite3.Error):
            self.unavailable()

    def read(self):
        rows = []
        if not self.warning:
            try:
                with self.lock, self.connect() as db:
                    db.row_factory = sqlite3.Row
                    rows = [dict(row) for row in db.execute(
                        'SELECT id, operation, started, finished, path, branch, source, source_sha, '
                        'before_sha, sha, status, detail, thread_url, resolution_status, resolution_detail '
                        'FROM operations WHERE repository=? ORDER BY started DESC LIMIT 200',
                        (self.repository,))]
            except (OSError, sqlite3.Error):
                self.unavailable()
        return dict(items=rows, warning=self.warning)


def record_operation(operation, plans):
    def decorate(action):
        @wraps(action)
        def recorded(repo, identifier):
            plan = getattr(repo, plans).get(identifier) if isinstance(identifier, str) else None
            history = repo.history
            if not history or not plan:
                return action(repo, identifier)
            state = plan['state'] if operation == 'merge' else plan['context']['state']
            entry = history.start(operation, state, plan.get('ref', ''), plan.get('sha', ''))
            plan['_history_id'] = entry
            try:
                result = action(repo, identifier)
            except Exception as error:
                # A hook or a timeout may fail after Git has written; never claim rollback.
                history.finish(entry, 'unconfirmed', '', failure_detail(error))
                raise
            status = result['kind'] if operation == 'merge' else 'unchanged' if result.get('unchanged') else 'succeeded'
            detail = failure_detail(result.get('output', '')) if status == 'failed' else ''
            history.finish(entry, status, result.get('sha', result['state']['head']), detail)
            if history.warning:
                result['history_warning'] = history.warning
            return result
        return recorded
    return decorate
