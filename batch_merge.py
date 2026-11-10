"""Ordered local merge queues; reuse the single-merge guards, resolver and hooks."""
import copy
import hashlib
from pathlib import Path
import re
import secrets
import threading
import time


class BatchMerger:
    def __init__(self, repo):
        self.repo = repo
        self.lock = threading.RLock()
        self.plans = {}
        self.push_plans = {}
        self.job = None
        self.worker = None
        self.stop = threading.Event()
        self.recovery = None
        self.resume_plans = {}

    def status(self):
        with self.lock:
            if self.recovery:
                repair = self.repo.resolver.status(self.recovery['path'])
                if repair and repair.get('kind') == 'repair':
                    self.job['rows'][self.recovery['index']]['repair'] = repair
            return copy.deepcopy(self.job)

    def guard(self):
        with self.lock:
            if self.job and (self.job['status'] == 'running' or (self.job.get('push') or {}).get('status') == 'running'):
                raise ValueError('批量任务正在执行，请等待结束或停止后续任务。')
            if self.recovery:
                repair = self.repo.resolver.status(self.recovery['path'])
                if repair and repair.get('kind') == 'repair' and repair['status'] == 'running':
                    raise ValueError('Codex 正在修复本次失败，请等待结束后检查。')

    def _targets(self, refs):
        branches = {b['ref']: b for b in self.repo.branches() if b['ref']}
        targets = []
        for ref in refs:
            item = branches.get(ref)
            if not item or not item['branch'] or item['missing']:
                raise ValueError('目标必须是当前仓库可用的已命名分支。')
            if any(t['branch'] == item['branch'] for t in targets):
                raise ValueError('同一目标分支只能选择一次。')
            state = self.repo.snapshot(item['path']) if item['path'] else None
            if state and (state['blocked'] or state['branch'] != item['branch']):
                raise ValueError(item['branch'] + '：' + (state['blocked'] or '工作区分支已变化'))
            targets.append(dict(ref=ref, branch=item['branch'], path=item['path'],
                                sha=self.repo.source(ref), state=state))
        return targets

    def preview(self, data):
        self.guard()
        self.repo.resolver.guard()
        mode, sources, targets = (data.get(k) for k in ('mode', 'sources', 'targets'))
        if (mode not in ('all-in', 'sync') or not all(isinstance(v, list) and v and
                len(v) <= 30 and all(isinstance(r, str) for r in v) and len(set(v)) == len(v)
                for v in (sources, targets)) or
                (mode == 'all-in' and len(targets) != 1) or (mode == 'sync' and len(sources) != 1)):
            raise ValueError('梭哈模式为多个来源 → 一个目标；同步模式为一个来源 → 多个目标，最多 30 项。')
        targets = self._targets(targets)
        tips = {ref: self.repo.source(ref) for ref in sources}
        rows = []
        for target in targets:
            for ref in sources:
                if ref in (target['ref'], 'refs/heads/' + target['branch']):
                    raise ValueError('来源和目标不能是同一分支。')
                sha = tips[ref]
                code, base = self.repo_git('merge-base', target['sha'], sha, allowed=(0, 1))
                if code:
                    raise ValueError('来源与 ' + target['branch'] + ' 没有共同祖先。')
                rows.append(dict(source=ref, source_sha=sha, target=target['branch'], target_ref=target['ref'],
                    path=target['path'], count=int(self.repo_git('rev-list', '--count', target['sha'] + '..' + sha)[1]),
                    stat=self.repo_git('diff', '--no-ext-diff', '--no-textconv', '--stat', base.strip(), sha, '--', display=True)[1],
                    status='pending', summary='等待执行', commit='', resolution=None))
        identifier = secrets.token_urlsafe(24)
        self.plans = {k: v for k, v in self.plans.items() if time.monotonic() - v['time'] < 600}
        if len(self.plans) >= 10:
            self.plans.clear()
        self.plans[identifier] = dict(mode=mode, sources=tips, targets=targets, rows=rows, time=time.monotonic())
        return dict(plan=identifier, mode=mode, rows=copy.deepcopy(rows))

    def repo_git(self, *args, **kwargs):
        return self.repo.resolver.git(self.repo.root, *args, **kwargs)

    def start(self, identifier, auto_commit):
        with self.lock:
            self.guard()
            self.repo.resolver.guard()
            if auto_commit is not True:
                raise ValueError('请确认允许按顺序创建本地合并提交；不会自动推送。')
            plan = self.plans.pop(identifier, None) if isinstance(identifier, str) else None
            if not plan or time.monotonic() - plan['time'] >= 600:
                raise ValueError('批量预览已失效，请重新预览。')
            if self._targets([t['ref'] for t in plan['targets']]) != plan['targets'] or any(
                    self.repo.source(ref) != sha for ref, sha in plan['sources'].items()):
                raise ValueError('预览后分支或工作区已变化，请重新预览。')
            self.stop.clear()
            self.recovery = None
            self.resume_plans.clear()
            self.job = dict(id=secrets.token_urlsafe(24), mode=plan['mode'], status='running',
                            summary='正在准备批量合并…', rows=copy.deepcopy(plan['rows']))
            self.worker = threading.Thread(target=self._run, args=(plan,), daemon=True)
            try:
                self.worker.start()
            except RuntimeError:
                self.job.update(status='failed', summary='无法启动批量队列，未执行合并；请重新预览。')
                raise ValueError(self.job['summary']) from None
            return self.status()

    def cancel(self, identifier):
        with self.lock:
            if not self.job or self.job['id'] != identifier:
                raise ValueError('批量任务已变化，请刷新状态。')
            active = self.job.get('push') if (self.job.get('push') or {}).get('status') == 'running' else self.job
            if active['status'] == 'running':
                self.stop.set()
                active['summary'] = '正在停止后续任务；已完成的提交、推送及当前命令现场会保留。'
            return self.status()

    def close(self):
        self.stop.set()

    def _check_stop(self):
        if self.stop.is_set():
            raise ValueError('已停止后续任务，已完成的提交、推送及当前现场均已保留。')

    def _update(self, row=None, **values):
        with self.lock:
            (row if row is not None else self.job).update(values)

    def _run(self, plan, start=0, expected=None, resumed=False):
        row = None
        expected = {} if expected is None else expected
        stage = ''
        scope = None
        try:
            for index in range(start, len(self.job['rows'])):
                row = self.job['rows'][index]
                stage = 'commit' if resumed and index == start else ''
                scope = None
                if stage:
                    state = resumed['state']
                self._check_stop()
                if any(self.repo.source(ref) != sha for ref, sha in plan['sources'].items()):
                    raise ValueError('执行期间来源引用已变化，已停止后续任务，请重新预览。')
                target = next(t for t in plan['targets'] if t['ref'] == row['target_ref'])
                if resumed and index == start:
                    if self.repo.commit_context(row['path']) != resumed:
                        raise ValueError('确认后待提交合并已变化，未继续提交，请重新检查修复。')
                    state = self.repo.snapshot(row['path'])
                elif target['ref'] not in expected:
                    if self._targets([target['ref']]) != [target]:
                        raise ValueError('目标分支或工作区已变化，请重新预览。')
                    state = self.repo.prepare_target(target['ref'])
                    if state['head'] != target['sha'] or state['branch'] != target['branch']:
                        raise ValueError('准备工作区期间目标分支已变化，未执行合并。')
                else:
                    state = self.repo.snapshot(row['path'] or target['path'])
                    if state != expected[target['ref']]:
                        raise ValueError('上一轮完成后目标工作区已变化，已停止后续任务。')
                path = state['path']
                target['path'] = path
                if state['blocked'] and not (resumed and index == start):
                    raise ValueError(state['blocked'])
                self._update(row, path=path, status='running', summary='正在合并…')
                self._update(summary=row['source'].removeprefix('refs/heads/').removeprefix('refs/remotes/') + ' → ' + row['target'])
                if resumed and index == start:
                    result = dict(kind='pending', state=state)
                else:
                    preview = self.repo.preview(path, row['source'])
                    if preview['sourceSha'] != row['source_sha'] or preview['state'] != state:
                        raise ValueError('执行前来源或目标状态已变化，已停止。')
                    self._check_stop()
                    result = self.repo.merge(preview['plan'])
                if result['kind'] in ('conflicts', 'pending'):
                    stage = 'resolve' if result['kind'] == 'conflicts' else 'commit'
                    files = set(self.repo.resolver.git(path, 'diff', '--cached', '--name-only', '-z', 'HEAD', '--')[1].split('\0')) - {''}
                    scope = dict(files=sorted(files), protected=self.repo.resolver.protected(path, files))
                if result['kind'] == 'conflicts':
                    self._update(row, summary='Codex 正在解决冲突…')
                    while True:
                        self._check_stop()
                        resolution = self.repo.resolver.status(path)
                        self._update(row, resolution=resolution)
                        if not resolution or resolution['status'] != 'running':
                            break
                        self.stop.wait(.5)
                    if not resolution or resolution['status'] != 'succeeded':
                        raise ValueError((resolution or {}).get('summary') or '冲突处理未完成，现场已保留。')
                elif result['kind'] not in ('pending', 'unchanged'):
                    raise ValueError(result['output'] or '合并失败，现场已保留。')
                self._check_stop()
                if result['kind'] == 'unchanged':
                    after = self.repo.snapshot(path)
                    if any(after[key] != state[key] for key in state if key != 'resolution'):
                        raise ValueError('合并后工作区状态意外变化，请人工检查。')
                    self._update(row, status='skipped', summary='来源已包含，无需合并')
                else:
                    commit = self.repo.preview_commit(path)
                    context = self.repo.commit_plans[commit['plan']]['context']
                    if result['kind'] == 'pending' and context['state'] != result['state']:
                        raise ValueError('合并后工作区状态已变化，未自动提交；请检查新增改动。')
                    if (commit['head'] != state['head'] or commit['branch'] != target['branch'] or
                            context['parents'] != [row['source_sha']]):
                        raise ValueError('待提交合并的分支、HEAD 或来源不符，未自动提交。')
                    self._check_stop()
                    stage = 'commit'
                    committed = self.repo.commit_merge(commit['plan'])
                    after = committed['state']
                    self._update(row, status='succeeded', summary='已合并并提交到本地', commit=committed['sha'])
                    if after['blocked']:
                        raise ValueError('提交后工作区仍有改动，已停止后续任务，请人工检查。')
                expected[target['ref']] = after
                self._update(row, head=after['head'])
                self.recovery = None
            self._update(status='succeeded', summary='全部批量合并已完成，本地提交已保留，未推送。')
        except Exception as error:
            stopped = self.stop.is_set()
            if row is not None and row['status'] not in ('succeeded', 'skipped'):
                self._update(row, status='stopped' if stopped else 'failed', summary=str(error))
                if stage in ('commit', 'resolve'):
                    self._remember_failure(plan, expected, index, state, stage, str(error), scope)
            self._update(status='stopped' if stopped else 'failed', summary=str(error))

    def _remember_failure(self, plan, expected, index, before, stage, error, scope):
        row = self.job['rows'][index]
        path = row['path']
        try:
            state = self.repo.snapshot(path)
            identity = self.repo.resolver.identity(path)
            if (state['branch'] != row['target'] or state['head'] != before['head'] or
                    not state['branch'] or not state['upstream_ok'] or
                    state['operations'] != ['MERGE_HEAD'] or identity[3].split() != [row['source_sha']]):
                return
            if scope is None:
                scope = self.recovery
            if not scope or self.repo.resolver.protected(path, scope['files']) != scope['protected']:
                return
            self.recovery = dict(plan=plan, expected=copy.deepcopy(expected), index=index, path=path,
                identity=identity, files=scope['files'], stage=stage, error=error, protected=scope['protected'])
            self._update(row, recoverable=True)
        except Exception:
            # An uncertain Git result must never become an automatic retry.
            self.recovery = None

    def _recoverable(self, identifier):
        self.guard()
        self.repo.resolver.guard()
        failure = self.recovery
        if not failure or not self.job or self.job['id'] != identifier or self.job['status'] not in ('failed', 'stopped'):
            raise ValueError('此失败没有可安全续跑的现场，请重新读取当前工作区。')
        path = failure['path']
        row = self.job['rows'][failure['index']]
        state = self.repo.snapshot(path)
        if state['operations'] != ['MERGE_HEAD']:
            raise ValueError('原待提交合并现场已结束或改变（可能已提交或中止），不能继续旧批次；'
                             '请刷新进度，重新选择尚未完成的分支并预览。')
        if (state['branch'] != row['target'] or state['head'] != failure['identity'][2].strip() or
                not state['upstream_ok'] or self.repo.resolver.identity(path) != failure['identity'] or
                self.repo.resolver.protected(path, failure['files']) != failure['protected']):
            raise ValueError('分支、合并来源、配置或修复范围外的内容已变化，未继续；请人工检查。')
        if any(self.repo.source(ref) != sha for ref, sha in failure['plan']['sources'].items()):
            raise ValueError('来源引用已变化，不能继续旧批次，请重新预览。')
        for target in failure['plan']['targets']:
            if target['ref'] == row['target_ref']:
                continue
            expected = failure['expected'].get(target['ref'])
            if (self.repo.snapshot(target['path']) != expected if expected else self._targets([target['ref']]) != [target]):
                raise ValueError('其他目标分支或工作区已变化，不能继续旧批次，请重新预览。')
        return failure

    def repair(self, identifier, index):
        with self.lock:
            failure = self._recoverable(identifier)
            if type(index) is not int or index != failure['index']:
                raise ValueError('失败项已变化，请刷新批量结果。')
            row = self.job['rows'][index]
            state = self.repo.snapshot(failure['path'])
            repair = self.repo.resolver.start_repair(failure['path'], state,
                dict(stage=failure['stage'], error=failure['error'], source=row['source'], source_sha=row['source_sha']))
            self.resume_plans.clear()
            self._update(row, repair=repair)
            return self.status()

    def preview_resume(self, identifier):
        with self.lock:
            failure = self._recoverable(identifier)
            path = failure['path']
            for name in failure['files']:
                file = Path(path) / name
                if file.is_file() and not file.is_symlink() and re.search(
                        rb'^(?:<{7,} |\|{7,} |={7,}\r?$|>{7,} )', file.read_bytes(), re.MULTILINE):
                    raise ValueError('仍有冲突标记，请先修复：' + name)
            command = self.repo.resolver.git
            command(path, 'diff', '--check', 'HEAD', '--')
            branch = self.job['rows'][failure['index']]['target']
            message = self.repo.merge_message(branch, path, failure['identity'][2].strip(),
                                              [self.job['rows'][failure['index']]['source_sha']])
            token = secrets.token_urlsafe(24)
            self.resume_plans = {token: dict(batch=identifier, time=time.monotonic(),
                fingerprint=self.repo.resolver.protected(path, ()), message=message,
                content=self._resume_content(path))}
            return dict(plan=token, path=path, branch=branch, head=failure['identity'][2].strip(), message=message,
                files=command(path, 'diff', '--name-status', '--no-renames', 'HEAD', '--', display=True)[1],
                stat=command(path, 'diff', '--stat', 'HEAD', '--', display=True)[1],
                patch=command(path, 'diff', '--no-ext-diff', '--no-textconv', '--no-renames', 'HEAD', '--', display=True)[1],
                remaining=len(self.job['rows']) - failure['index'])

    def _resume_content(self, path):
        patch = self.repo.resolver.git(path, 'diff', '--no-ext-diff', '--no-textconv', '--binary', 'HEAD', '--')[1]
        return hashlib.sha256(patch.encode()).hexdigest()

    def resume(self, identifier):
        with self.lock:
            reviewed = self.resume_plans.pop(identifier, None) if isinstance(identifier, str) else None
            if not reviewed or time.monotonic() - reviewed['time'] >= 600:
                raise ValueError('继续预览已失效，请重新检查修复。')
            failure = self._recoverable(reviewed['batch'])
            path = failure['path']
            if self.repo.resolver.protected(path, ()) != reviewed['fingerprint']:
                raise ValueError('预览后内容已变化，请重新检查修复；未暂存或提交。')
            paths = [':(top,literal)' + name for name in failure['files']]
            if paths:
                self.repo.resolver.git(path, 'add', '-A', '--', *paths)
            if (self.repo.resolver.protected(path, failure['files']) != failure['protected'] or
                    self._resume_content(path) != reviewed['content']):
                raise ValueError('确认暂存期间内容或范围外现场已变化，未提交，请检查后重新预览。')
            preview = self.repo.preview_commit(path, reviewed['message'])
            context = self.repo.commit_plans.pop(preview['plan'])['context']
            if (context['state']['head'] != failure['identity'][2].strip() or context['parents'] != [
                    self.job['rows'][failure['index']]['source_sha']] or
                    self.repo.resolver.protected(path, failure['files']) != failure['protected'] or
                    self._resume_content(path) != reviewed['content']):
                raise ValueError('待提交合并已变化，未继续；确认暂存的修复已保留。')
            self.stop.clear()
            self.job.update(status='running', summary='已确认修复，正在继续失败项及后续任务…')
            self._update(self.job['rows'][failure['index']], recoverable=False)
            self.worker = threading.Thread(target=self._run,
                args=(failure['plan'], failure['index'], failure['expected'], context), daemon=True)
            try:
                self.worker.start()
            except RuntimeError:
                self.job.update(status='failed', summary='无法启动继续任务，已确认的暂存内容保留，请重试检查。')
                self._update(self.job['rows'][failure['index']], recoverable=True)
                raise ValueError(self.job['summary']) from None
            return self.status()

    def _completed(self, identifier):
        self.guard()
        self.repo.resolver.guard()
        if not self.job or self.job['id'] != identifier or self.job['status'] != 'succeeded':
            raise ValueError('只有当前已全部完成的批量合并可以批量推送。')

    def _push_context(self, item, completed=()):
        context = self.repo.push_context(item['context']['state']['path'])
        config = self.repo.resolver.git(context['state']['path'], 'config', '--null', '--list')[1]
        # A first push sets upstream keys in shared config; only previous targets' keys may differ.
        ignore = {f'branch.{branch}.{key}' for branch in completed for key in ('remote', 'merge')}
        def filtered(raw):
            return [entry for entry in raw.split('\0') if entry.partition('\n')[0] not in ignore]
        expected = dict(item['context'], config=context['config'])
        if (context != expected or hashlib.sha256(config.encode()).hexdigest() != context['config'] or
                filtered(config) != filtered(item['config'])):
            raise ValueError('批量预览后分支、工作区或配置已变化，请重新预览；未推送该项。')
        return context

    def preview_push(self, identifier):
        self._completed(identifier)
        targets = {row['target']: row for row in self.job['rows']}
        items, rows = [], []
        for branch, row in targets.items():
            preview = self.repo.preview_push(row['path'], allow_unchanged=True)
            if preview['branch'] != branch or preview['head'] != row['head']:
                raise ValueError(branch + '：合并完成后 HEAD 或分支已变化，请检查后单独预览推送。')
            context = self.repo.push_plans[preview['plan']]['context']
            config = self.repo.resolver.git(row['path'], 'config', '--null', '--list')[1]
            if hashlib.sha256(config.encode()).hexdigest() != context['config']:
                raise ValueError('读取推送预览时配置已变化，请重试预览。')
            items.append(dict(plan=preview['plan'], context=copy.deepcopy(context), config=config))
            rows.append(dict(preview, status='pending', summary='远端已一致，将跳过' if preview['unchanged'] else '等待确认推送'))
        plan_id = secrets.token_urlsafe(24)
        self.push_plans = {k: v for k, v in self.push_plans.items() if time.monotonic() - v['time'] < 600}
        if len(self.push_plans) >= 10:
            self.push_plans.clear()
        self.push_plans[plan_id] = dict(batch=identifier, items=items, rows=rows, time=time.monotonic())
        return dict(plan=plan_id, rows=copy.deepcopy(rows))

    def push(self, identifier):
        with self.lock:
            self.guard()
            plan = self.push_plans.pop(identifier, None) if isinstance(identifier, str) else None
            if not plan or time.monotonic() - plan['time'] >= 600:
                raise ValueError('批量推送预览已失效，请重新预览。')
            self._completed(plan['batch'])
            for item in plan['items']:
                context = self._push_context(item)
                reviewed = self.repo.push_plans.get(item['plan'])
                if (not reviewed or time.monotonic() - reviewed['time'] >= 600 or
                        self.repo.remote_tip(context) != reviewed['tip']):
                    raise ValueError('推送预览或远端已变化，请重新预览；尚未执行批量推送。')
            self.stop.clear()
            self.job['push'] = dict(status='running', summary='正在依次推送到同名远端分支…', rows=copy.deepcopy(plan['rows']))
            self.worker = threading.Thread(target=self._run_push, args=(plan,), daemon=True)
            try:
                self.worker.start()
            except RuntimeError:
                self.job['push'].update(status='failed', summary='无法启动批量推送，请重新预览。')
                raise ValueError(self.job['push']['summary']) from None
            return self.status()

    def _run_push(self, plan):
        row, completed = None, []
        try:
            for item, row in zip(plan['items'], self.job['push']['rows']):
                self._check_stop()
                context = self._push_context(item, completed)
                reviewed = self.repo.push_plans.get(item['plan'])
                if not reviewed:
                    raise ValueError('单项推送预览已失效，请重新预览。')
                reviewed['context'] = context
                self._update(row, status='running', summary='正在核对远端并推送…')
                value = self.repo.push(item['plan'])
                if not value.get('unchanged') and not item['context']['state']['upstream']:
                    completed.append(row['branch'])
                self._update(row, status='skipped' if value.get('unchanged') else 'succeeded',
                             summary='远端已一致，已跳过' if value.get('unchanged') else '已推送并回读确认')
            self._update(self.job['push'], status='succeeded', summary='批量推送已完成，所有同名远端分支均已核对。')
        except Exception as error:
            status = 'stopped' if self.stop.is_set() else 'failed'
            if row is not None:
                self._update(row, status=status, summary=str(error))
            self._update(self.job['push'], status=status, summary=str(error))
