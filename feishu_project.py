"""Personal Feishu Project schedules over the locally configured MCP connection."""
import hashlib
import json
import math
import os
import platform_support as platform
from pathlib import Path
import queue
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote
from urllib.request import getproxies, proxy_bypass

PROJECT = '62983005bc43adc6923d707d'
PROJECT_NAME = '平台事业部'
SHANGHAI = timezone(timedelta(hours=8))
FIELDS = ('start', 'end', 'points', 'actual')
LABELS = dict(start='开始日期', end='结束日期', points='估分', actual='实际工时')


class ScheduleError(Exception):
    pass


class MCPClient:
    def __init__(self):
        self.process = None
        self.sequence = 0
        self.messages = queue.Queue()
        self.lock = threading.Lock()

    def connect(self):
        executable = shutil.which('codex') or str(Path.home() / '.local/bin/codex')
        try:
            raw = subprocess.run(platform.windows_command([executable, 'mcp', 'get', 'FeishuProjectMcp', '--json']),
                                 capture_output=True, text=True, timeout=15, check=True)
            config = json.loads(raw.stdout)
            transport = config['transport']
            if not config.get('enabled') or transport['type'] != 'stdio':
                raise ScheduleError('请在 Codex 中启用 FeishuProjectMcp 的本地 stdio 连接。')
        except FileNotFoundError:
            raise ScheduleError('未找到 Codex CLI，请检查工作台启动路径中的 Codex 安装。') from None
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            raise ScheduleError('无法读取 Codex 的 FeishuProjectMcp 配置，请检查当前配置目录与授权。') from None
        try:
            self.allowed = config.get('enabled_tools')
            self.disabled = config.get('disabled_tools') or []
            self.messages = queue.Queue()
            command = [transport['command'], *(transport.get('args') or [])]
            if Path(command[0]).name == 'npx':
                command.insert(1, '--offline')  # Registry checks can stall an already installed MCP before its handshake.
            env = {**os.environ, **(transport.get('env') or {})}
            if not any(key.lower() in ('http_proxy', 'https_proxy', 'all_proxy') for key in env):
                proxy = getproxies().get('https')
                if proxy and not proxy_bypass('project.feishu.cn'):
                    env['HTTPS_PROXY'] = proxy
            if any(env.get(key) for key in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy')):
                env.setdefault('NODE_USE_ENV_PROXY', '1')  # Node fetch does not read macOS system proxies itself.
            self.process = subprocess.Popen(
                platform.windows_command(command),
                env=env, cwd=transport.get('cwd') or None,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', **platform.child_options())
        except FileNotFoundError:
            raise ScheduleError('未找到飞书项目连接命令，请检查 Node/npx 的安装及工作台启动路径；更新应用后需重启后台。') from None
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            raise ScheduleError('无法启动飞书项目连接，请检查 Node/npx、连接命令及授权配置。') from None
        process, messages = self.process, self.messages

        def read():
            try:
                for line in process.stdout:
                    try:
                        messages.put(json.loads(line))
                    except ValueError:
                        continue
            finally:
                messages.put(None)
        threading.Thread(target=read, daemon=True).start()
        self.rpc('initialize', {'protocolVersion': '2024-11-05', 'capabilities': {},
                              'clientInfo': {'name': 'worktree-merge', 'version': '1.0'}})
        self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized', 'params': {}})

    def send(self, value):
        self.process.stdin.write(json.dumps(value) + '\n')
        self.process.stdin.flush()

    def rpc(self, method, params):
        self.sequence += 1
        identifier = self.sequence
        self.send({'jsonrpc': '2.0', 'id': identifier, 'method': method, 'params': params})
        deadline = time.monotonic() + 45
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise queue.Empty()
            message = self.messages.get(timeout=remaining)
            if message is None:
                raise OSError('MCP disconnected')
            if message.get('id') == identifier:
                if 'error' in message:
                    raise ScheduleError('飞书项目请求被拒绝，请检查授权、字段和权限。')
                return message['result']

    def call(self, name, arguments):
        with self.lock:
            try:
                if self.process is None or self.process.poll() is not None:
                    self.close()
                    self.connect()
                if name in self.disabled or (self.allowed is not None and name not in self.allowed):
                    raise ScheduleError('此操作未在 Codex 的 FeishuProjectMcp 工具权限中启用。')
                result = self.rpc('tools/call', {'name': name, 'arguments': arguments})
                if result.get('isError'):
                    raise ScheduleError('飞书项目操作失败，请检查当前权限、必填字段或原页面状态。')
                for block in result.get('content', []):
                    if block.get('type') != 'text':
                        continue
                    text = block.get('text', '').strip()
                    if text.lower().startswith('error='):
                        raise ScheduleError('飞书项目操作失败，请刷新状态或打开原页面检查。')
                    try:
                        return json.loads(text)
                    except ValueError:
                        continue
                if name == 'update_node':
                    return None  # The write is verified by a separate read, not a success string.
                raise ScheduleError('飞书项目返回格式发生变化，请打开原工作台核对。')
            except ScheduleError:
                self.close()
                raise
            except (OSError, ValueError, KeyError, queue.Empty, subprocess.SubprocessError):
                self.close()
                raise ScheduleError('飞书项目连接中断或超时。若刚保存，请先重新读取核对，勿重复提交。') from None

    def close(self):
        process, self.process = self.process, None
        if process is not None:
            if process.poll() is None:
                try:
                    platform.terminate_tree(process.pid) if platform.IS_WINDOWS else os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=3)
                except ProcessLookupError:
                    pass
                except subprocess.TimeoutExpired:
                    platform.terminate_tree(process.pid) if platform.IS_WINDOWS else os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()


def parse_date(value):
    try:
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError()
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError()
        return parsed
    except ValueError:
        raise ScheduleError('请输入有效日期，格式为 YYYY-MM-DD。') from None


def day(value):
    if value in (None, '', 0, '0'):
        return ''
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, SHANGHAI).date().isoformat()
    text = str(value)
    if 'T' in text:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
        if parsed.tzinfo is not None:
            text = parsed.astimezone(SHANGHAI).date().isoformat()
    text = text[:10]
    return '' if text == '1970-01-01' else parse_date(text).isoformat()


def timestamp(value):
    return int(datetime.combine(parse_date(value), datetime.min.time(), SHANGHAI).timestamp() * 1000)


def number(value):
    if value is None or value == '':
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ScheduleError('估分与实际工时必须为非负数，或留空。')
    try:
        result = float(value)
    except (ValueError, OverflowError):
        raise ScheduleError('估分与实际工时必须为非负数，或留空。') from None
    if not math.isfinite(result) or result < 0:
        raise ScheduleError('估分与实际工时必须为非负数，或留空。')
    return result


def values(info):
    return dict(start=day(info.get('estimate_start_time')), end=day(info.get('estimate_finish_time')),
                points=number(info.get('points')), actual=number(info.get('actual_work_time')))


class FeishuProject:
    def __init__(self, client=None, config=None):
        self.client = client or MCPClient()
        self.config = dict(config or {})
        if config is None and not platform.IS_WINDOWS:
            self.config.update(dict(enabled=True, space_id=PROJECT, space_name=PROJECT_NAME,
                project_url='https://project.feishu.cn/platform', work_item_type='story',
                work_item_api='story', node_name='前端开发', owner_field='负责人',
                version_field='planning_version', version_type='version', version_api='version',
                release_date_field='schedule_publish_date', space_alias='platform'))
        self.config.setdefault('enabled', False)
        self.config.setdefault('owner_field', '负责人')
        if self.config['enabled']:
            keys = ('space_id', 'space_alias', 'work_item_type', 'work_item_api', 'version_field',
                    'version_type', 'version_api', 'release_date_field')
            if (not all(isinstance(self.config.get(key), str) and re.fullmatch(r'[A-Za-z0-9_-]{1,200}', self.config[key])
                        for key in keys) or
                    not all(isinstance(self.config.get(key), str) and self.config[key] and len(self.config[key]) <= 200
                            and "'" not in self.config[key] and '\\' not in self.config[key]
                            for key in ('node_name', 'owner_field')) or
                    not isinstance(self.config.get('space_name'), str) or not self.config['space_name'] or
                    self.config.get('project_url') != f'https://project.feishu.cn/{self.config["space_alias"]}'):
                raise ScheduleError('飞书关联配置不完整或含非法查询字符，请重新关联空间。')
        self.plans = {}

    @property
    def space_id(self):
        return self.config.get('space_id', '')

    @staticmethod
    def _rows(raw, key):
        rows = raw.get(key) if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            raise ScheduleError('飞书配置元数据格式不完整，请重新读取。')
        return rows

    def _field_list(self, space_id, type_key):
        rows, page = [], 1
        while True:
            raw = self.client.call('list_workitem_field_config', {
                'project_key': space_id, 'work_item_type': type_key, 'page_num': page})
            items = self._rows(raw, 'list')
            pagination = raw.get('pagination') or {}
            if pagination.get('page_num', page) != page or not isinstance(pagination.get('total', len(items)), int):
                raise ScheduleError('字段配置分页不完整，请重新读取。')
            rows.extend(items)
            total = pagination.get('total', len(items))
            if len(rows) == total:
                return rows
            if len(rows) > total or not items or page >= 50:
                raise ScheduleError('字段配置未完整读取，请重新选择。')
            page += 1

    def metadata(self, data=None):
        """Return only MCP-discovered choices; a real sample item is needed for node names."""
        data = data or {}
        requested = data['space_id'] if 'space_id' in data else self.space_id
        if not isinstance(requested, str) or len(requested) > 200:
            raise ScheduleError('空间标识无效，请从飞书可见空间中选择。')
        spaces_raw = self.client.call('search_project_info', {'project_key': requested})
        spaces = self._rows(spaces_raw, 'projects')
        spaces = [dict(project_key=str(row['project_key']), name=str(row['name']),
                       simple_name=str(row.get('simple_name') or ''),
                       project_url=(f'https://project.feishu.cn/{row["simple_name"]}'
                                    if isinstance(row.get('simple_name'), str) and
                                    re.fullmatch(r'[A-Za-z0-9_-]{1,200}', row['simple_name']) else '')) for row in spaces
                  if isinstance(row, dict) and row.get('project_key') and row.get('name')]
        pagination = (spaces_raw.get('pagination') or {})
        if pagination.get('page_num', 1) != 1 or pagination.get('has_more', False) or pagination.get('total', len(spaces)) != len(spaces):
            raise ScheduleError('可见空间未完整读取，请缩小检索范围。')
        if not spaces:
            raise ScheduleError('当前飞书身份无权读取该空间，或空间不存在。')
        matches = [space for space in spaces if requested in (space['project_key'], space['simple_name'], space['name'])]
        selected = matches[0] if requested and len(matches) == 1 else None
        if requested and selected is None:
            raise ScheduleError('飞书返回空间与所选标识不一致。')
        if selected is None:
            return dict(spaces=spaces, types=[], fields=[], node_fields=[], version_fields=[], nodes=[], selected_space=None)
        project_key = selected['project_key']
        types_raw = self.client.call('list_workitem_types', {'project_key': project_key})
        types = [dict(type_key=str(row['type_key']), name=str(row['name']), api_name=str(row.get('api_name') or row['type_key']),
                      is_disable=row.get('is_disable'))
                 for row in self._rows(types_raw, 'list') if isinstance(row, dict) and row.get('type_key') and row.get('name')]
        type_key = data.get('work_item_type') or self.config.get('work_item_type') or ''
        fields, node_fields, version_fields, nodes = [], [], [], []
        selected_type = next((row for row in types if row['type_key'] == type_key), None)
        if selected_type:
            fields = self._field_list(project_key, type_key)
            node_fields = self._rows(self.client.call('list_node_field_config', {
                'project_key': project_key, 'work_item_type': type_key}), 'list')
            version_field = data.get('version_field') or self.config.get('version_field') or ''
            relation = next((field for field in fields if field.get('field_key') == version_field), None)
            related = (relation or {}).get('related_work_item_info') or []
            version_type = data.get('version_type') or self.config.get('version_type') or (
                related[0].get('work_item_type') if related else '')
            if version_type:
                version_fields = self._field_list(project_key, version_type)
            sample = data.get('sample_work_item_id')
            if sample:
                identifier = self.identifier(sample)
                brief = self.client.call('get_workitem_brief', {'project_key': project_key, 'work_item_id': identifier})
                attributes = brief.get('work_item_attribute', {})
                if attributes.get('owned_project', {}).get('key') != project_key or attributes.get('work_item_type', {}).get('key') != type_key:
                    raise ScheduleError('样例工作项不属于所选空间与类型。')
                users = self.client.call('search_user_info', {'project_key': project_key, 'user_keys': ['current_login_user()']})
                if not isinstance(users, list) or len(users) != 1 or not users[0].get('user_key'):
                    raise ScheduleError('无法确认当前飞书身份，请检查登录授权。')
                user_key = str(users[0]['user_key'])
                nodes = [dict(node_key=node.get('basic', {}).get('node_key', ''),
                              node_uuid=node.get('basic', {}).get('node_uuid', ''),
                              name=node.get('basic', {}).get('name', ''),
                              owners=[str(owner.get('user_key')) for owner in node.get('assignees', {}).get('owners', [])])
                         for node in self.nodes(identifier, project_key)
                         if user_key in [str(owner.get('user_key')) for owner in node.get('assignees', {}).get('owners', [])]]
        return dict(spaces=spaces, types=types, fields=fields, node_fields=node_fields,
                    version_fields=version_fields, nodes=nodes, selected_space=selected)

    def validate_config(self, data):
        if not isinstance(data, dict):
            raise ScheduleError('飞书配置格式无效。')
        if type(data.get('enabled')) is not bool:
            raise ScheduleError('飞书启用状态无效。')
        if not data.get('enabled'):
            config = {**self.config, 'enabled': False}
            return config
        space_id = data.get('space_id')
        if not isinstance(space_id, str) or not space_id or len(space_id) > 200:
            raise ScheduleError('请选择有权限访问的飞书空间。')
        raw = self.client.call('search_project_info', {'project_key': space_id})
        spaces = [row for row in self._rows(raw, 'projects') if row.get('project_key') == space_id]
        if len(spaces) != 1:
            raise ScheduleError('当前飞书身份无法确认该空间。')
        alias = spaces[0].get('simple_name')
        if not isinstance(alias, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', alias):
            raise ScheduleError('飞书空间缺少可验证的链接标识。')
        project_url = f'https://project.feishu.cn/{alias}'
        if data.get('project_url') not in (None, '', project_url):
            raise ScheduleError('项目链接与所选飞书空间不一致。')
        type_key = data.get('work_item_type')
        types = self._rows(self.client.call('list_workitem_types', {'project_key': space_id}), 'list')
        work_type = next((row for row in types if row.get('type_key') == type_key and row.get('is_disable') != 1), None)
        if not work_type:
            raise ScheduleError('工作项类型不在所选空间的可用类型中。')
        fields = self._field_list(space_id, type_key)
        relation_key = data.get('version_field')
        relation = next((field for field in fields if field.get('field_key') == relation_key), None)
        if not relation or relation.get('field_type') != 'workitem_related_multi_select' or not relation.get('related_work_item_info'):
            raise ScheduleError('版本字段不是已验证的关联版本字段。')
        version_type = data.get('version_type') or relation['related_work_item_info'][0].get('work_item_type')
        if version_type not in {entry.get('work_item_type') for entry in relation['related_work_item_info']
                                if entry.get('project_key') == space_id}:
            raise ScheduleError('版本类型与所选关联字段不匹配。')
        version_work_type = next((row for row in types if row.get('type_key') == version_type and row.get('is_disable') != 1), None)
        if not version_work_type:
            raise ScheduleError('关联版本类型不在所选空间的可用类型中。')
        version_fields = self._field_list(space_id, version_type)
        release_field = data.get('release_date_field')
        if not any(field.get('field_key') == release_field and field.get('field_type') == 'date' for field in version_fields):
            raise ScheduleError('上线日期字段不属于所选版本类型。')
        node_fields = self._rows(self.client.call('list_node_field_config', {
            'project_key': space_id, 'work_item_type': type_key}), 'list')
        owner_field = data.get('owner_field')
        owner = next((field for field in node_fields if owner_field in (field.get('field_key'), field.get('field_name'))), None)
        if not owner or owner.get('field_type') != 'multi-user':
            raise ScheduleError('负责人字段不在所选类型的节点字段中。')
        node_name, sample = data.get('node_name'), data.get('sample_work_item_id')
        if not isinstance(node_name, str) or not node_name or not sample:
            raise ScheduleError('请选择从真实样例需求读取到的本人节点。')
        candidates = [node for node in self.metadata(dict(space_id=space_id, work_item_type=type_key,
            version_field=relation_key, version_type=version_type, sample_work_item_id=sample))['nodes']
            if node['name'] == node_name]
        if len(candidates) != 1:
            raise ScheduleError('所选节点不在样例工作项的本人负责节点中，或节点名称不唯一。')
        config = dict(enabled=True, space_id=space_id, space_name=spaces[0]['name'],
            space_alias=alias, project_url=project_url,
            work_item_type=type_key, work_item_api=work_type.get('api_name') or type_key,
            node_name=node_name, sample_work_item_id=self.identifier(sample), owner_field=owner['field_name'],
            owner_field_key=owner['field_key'], version_field=relation_key,
            version_type=version_type, version_api=version_work_type.get('api_name') or version_type,
            release_date_field=release_field)
        if (not all(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,200}', value)
                    for value in (config['space_id'], config['space_alias'], config['work_item_type'], config['work_item_api'],
                                  config['version_field'], config['version_type'], config['version_api'], config['release_date_field'])) or
                not all(isinstance(value, str) and value and len(value) <= 200 and "'" not in value and '\\' not in value
                        for value in (config['node_name'], config['owner_field']))):
            raise ScheduleError('飞书字段含无法安全用于查询的字符，请在飞书核对配置。')
        return config

    def ensure_enabled(self):
        if not self.config.get('enabled'):
            raise ScheduleError('此项目未关联飞书空间，飞书排期与需求功能已停用；请在项目设置中关联空间。')

    def project_url(self, type_key='', identifier=''):
        base = str(self.config.get('project_url') or '').rstrip('/')
        return f'{base}/{quote(type_key, safe="")}/detail/{quote(str(identifier), safe="")}' if identifier else f'{base}/overview'

    def close(self):
        self.client.close()

    def user(self):
        self.ensure_enabled()
        users = self.client.call('search_user_info', {'project_key': self.space_id, 'user_keys': ['current_login_user()']})
        if not isinstance(users, list) or len(users) != 1 or not users[0].get('user_key'):
            raise ScheduleError('无法确认当前飞书身份，请检查登录授权。')
        return {'key': str(users[0]['user_key']), 'name': users[0].get('name_cn') or users[0].get('name_en') or '当前用户'}

    @staticmethod
    def identifier(value):
        if not isinstance(value, str) or not value.isascii() or not value.isdigit() or len(value) > 20:
            raise ScheduleError('工作项标识不正确，请从列表重新选择。')
        return value

    def overview(self, data):
        self.ensure_enabled()
        start, end = parse_date(data.get('start')), parse_date(data.get('end'))
        if start > end or (end - start).days > 89:
            raise ScheduleError('查询范围需在 90 天以内，结束日期不能早于开始日期。')
        user = self.user()
        raw = self.client.call('list_schedule', dict(project_key=self.space_id, user_keys=[user['key']],
            start_time=start.isoformat(), end_time=end.isoformat(), work_item_type_keys=['_all']))
        people = raw.get('user_workload_list') or []
        person = next((p for p in people if str(p.get('user_info', {}).get('userKey')) == user['key']), None)
        if people and person is None:
            raise ScheduleError('排期返回人员与当前用户不一致，请重新连接。')
        person = person or {}
        rows = []
        for item in person.get('tasks') or []:
            work, state, timing = item.get('work_item_info', {}), item.get('state', {}), item.get('time', {})
            rows.append(dict(work_item_id=str(work['id']), node_id=state.get('state_id', ''),
                name=work.get('name', ''), node_name=state.get('state_name', ''),
                start=day(timing.get('start')), end=day(timing.get('end')), points=timing.get('duration'),
                passed=bool(state.get('passed')), editable=bool(state.get('state_id')),
                developable=bool(state.get('state_id')),
                url=self.project_url()))
        return dict(user=user, project=self.config['space_name'], project_url=self.config['project_url'],
                    space_id=self.space_id, work_item_type=self.config['work_item_type'], node_name=self.config['node_name'], rows=rows,
                    total_points=person.get('total_score'))

    @staticmethod
    def mql_values(entry):
        if not isinstance(entry, dict) or not isinstance(entry.get('moql_field_list'), list):
            raise ScheduleError('版本信息返回格式发生变化，请在飞书核对。')
        values = {}
        for field in entry['moql_field_list']:
            if not isinstance(field, dict) or not isinstance(field.get('key'), str) or field['key'] in values:
                raise ScheduleError('版本字段返回格式发生变化，请在飞书核对。')
            value = field.get('value')
            if value is not None and (not isinstance(value, dict) or
                    not isinstance(field.get('value_type'), str) or field['value_type'] not in value):
                raise ScheduleError('版本字段返回格式发生变化，请在飞书核对。')
            values[field['key']] = value[field['value_type']] if value is not None else None
        return values

    def release_overview(self, data):
        self.ensure_enabled()
        start, end = parse_date(data.get('start')), parse_date(data.get('end'))
        if start > end or (end - start).days > 89:
            raise ScheduleError('查询范围需在 90 天以内，结束日期不能早于开始日期。')
        user = self.user()
        release_date = f'x.`{self.config["release_date_field"]}<target:{self.space_id}::{self.config["version_type"]}>`'
        raw = self.client.call('search_by_mql', dict(project_key=self.space_id, mql=
            f"SELECT `work_item_id`, `name` FROM `{self.config['space_alias']}`.`{self.config['work_item_api']}` "
            f"WHERE array_contains(get_node_attribute('{self.config['node_name']}','{self.config['owner_field']}'), current_login_user()) "
            f"AND any_relation_match(`{self.config['version_field']}`, x -> {release_date} >= '{start}' AND {release_date} <= '{end}')"))
        if not isinstance(raw, dict) or not isinstance(raw.get('data'), dict):
            raise ScheduleError('上线需求返回格式发生变化，请重新读取。')
        groups = raw.get('list')
        stories = {}
        if groups is None and not raw['data']:
            return dict(user=user, project=self.config['space_name'], project_url=self.config['project_url'],
                        space_id=self.space_id, work_item_type=self.config['work_item_type'], node_name=self.config['node_name'], rows=[])
        if (not isinstance(groups, list) or len(groups) != 1 or not isinstance(groups[0], dict) or
                type(groups[0].get('count')) is not int or groups[0]['count'] < 0 or
                not isinstance(groups[0].get('group_infos'), list) or len(groups[0]['group_infos']) != 1 or
                not isinstance(groups[0]['group_infos'][0], dict) or
                not isinstance(groups[0]['group_infos'][0].get('group_id'), str) or not groups[0]['group_infos'][0]['group_id']):
            raise ScheduleError('上线需求分页信息不完整，请重新读取。')
        total, group_id, session = groups[0]['count'], groups[0]['group_infos'][0]['group_id'], raw.get('session_id')
        if total > 50 and (not isinstance(session, str) or not session):
            raise ScheduleError('上线需求分页凭证缺失，请重新读取。')
        for page in range(1, max(1, (total + 49) // 50) + 1):
            if page > 1:
                raw = self.client.call('search_by_mql', dict(project_key=self.space_id, session_id=session,
                    group_pagination_list=[dict(group_id=group_id, page_num=page)]))
            if (not isinstance(raw, dict) or not isinstance(raw.get('data'), dict) or
                    set(raw['data']) - {group_id} or raw.get('list') not in (None, groups) or
                    raw.get('session_id') not in (None, session)):
                raise ScheduleError('上线需求分页返回不一致，请重新读取。')
            entries = raw['data'].get(group_id, [])
            if not isinstance(entries, list) or len(entries) != min(50, total - (page - 1) * 50):
                raise ScheduleError('上线需求未完整读取，请重新读取。')
            for entry in entries:
                values = self.mql_values(entry)
                identifier = self.identifier(str(values.get('work_item_id', '')))
                if identifier in stories or not isinstance(values.get('name'), str):
                    raise ScheduleError('上线需求标识重复或名称缺失，请重新读取。')
                stories[identifier] = values['name']
        rows, identifiers = [], list(stories)
        for offset in range(0, len(identifiers), 50):
            releases = self.releases(dict(work_item_ids=identifiers[offset:offset + 50]))['items']
            for identifier, release in releases.items():
                if release['status'] == 'unavailable' or any(version['status'] == 'unavailable' for version in release['versions']):
                    raise ScheduleError('部分上线日期读取失败，请重新读取后再选择合并。')
                versions = [version for version in release['versions'] if str(start) <= version['date'] <= str(end)]
                if versions:
                    rows.append(dict(work_item_id=identifier, name=stories[identifier], versions=versions,
                        url=self.project_url(self.config['work_item_api'], identifier)))
        return dict(user=user, project=self.config['space_name'], project_url=self.config['project_url'],
                    space_id=self.space_id, work_item_type=self.config['work_item_type'], node_name=self.config['node_name'], rows=rows)

    def release_rows(self, kind, fields, identifiers):
        api = self.config['work_item_api'] if kind == self.config['work_item_type'] else self.config['version_api']
        raw = self.client.call('search_by_mql', dict(project_key=self.space_id,
            mql=f'SELECT work_item_id, {fields} FROM `{self.config["space_alias"]}`.`{api}` WHERE work_item_id IN ({", ".join(identifiers)})'))
        if not isinstance(raw, dict) or not isinstance(raw.get('data'), dict):
            raise ScheduleError('版本信息返回格式发生变化，请在飞书核对。')
        groups = raw.get('list')
        if groups is None and not raw['data']:
            return {}
        if not isinstance(groups, list) or any(not isinstance(g, dict) or
                not isinstance(g.get('count'), int) or g['count'] < 0 for g in groups) or any(
                not isinstance(group, list) for group in raw['data'].values()):
            raise ScheduleError('版本信息数量无法核对，请在飞书查看。')
        entries = [entry for group in raw['data'].values() for entry in group]
        if len(entries) != sum(g['count'] for g in groups) or len(entries) > 50:
            raise ScheduleError('版本信息未完整读取，请重试或在飞书查看。')
        result = {}
        for entry in entries:
            values = self.mql_values(entry)
            identifier = str(values.get('work_item_id', ''))
            if identifier not in identifiers or identifier in result:
                raise ScheduleError('版本信息与请求不一致，请重新读取。')
            result[identifier] = values
        return result

    def releases(self, data):
        self.ensure_enabled()
        identifiers = data.get('work_item_ids')
        if not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 50:
            raise ScheduleError('每次最多读取 50 个需求的版本信息。')
        identifiers = list(dict.fromkeys(self.identifier(value) for value in identifiers))
        stories = self.release_rows(self.config['work_item_type'], self.config['version_field'], identifiers)
        items = {identifier: dict(status='unavailable', versions=[]) for identifier in identifiers}
        versions = {}
        for identifier, story in stories.items():
            if self.config['version_field'] not in story:
                continue
            related = story[self.config['version_field']]
            if related is None or related == []:
                items[identifier]['status'] = 'unassigned'
                continue
            if not isinstance(related, list) or any(not isinstance(v, dict) or
                    not isinstance(v.get('key'), str) or not v['key'].isascii() or
                    not v['key'].isdigit() or len(v['key']) > 20 or
                    not isinstance(v.get('label'), str) for v in related):
                continue
            items[identifier] = dict(status='ready', versions=[])
            for version in related:
                version_id = version['key']
                if any(v['id'] == version_id for v in items[identifier]['versions']):
                    continue
                item = dict(id=version_id, name=version['label'], date='', status='unavailable')
                items[identifier]['versions'].append(item)
                versions.setdefault(version_id, []).append(item)
        version_ids = list(versions)
        for offset in range(0, len(version_ids), 50):
            try:
                rows = self.release_rows(self.config['version_type'], f'name, {self.config["release_date_field"]}', version_ids[offset:offset + 50])
            except ScheduleError:
                continue  # Keep the linked version visible when its date cannot be read.
            for identifier, version in rows.items():
                if self.config['release_date_field'] not in version:
                    continue
                try:
                    release_date = day(version[self.config['release_date_field']])
                except (ScheduleError, ValueError, TypeError, OverflowError):
                    continue
                for item in versions[identifier]:
                    if isinstance(version.get('name'), str) and version['name']:
                        item['name'] = version['name']
                    item.update(date=release_date, status='ready' if release_date else 'undated')
        return dict(items=items)

    def nodes(self, identifier, space_id=None):
        nodes, page = [], 1
        while True:
            raw = self.client.call('get_node_detail', dict(project_key=space_id or self.space_id, work_item_id=identifier,
                                   node_id_list=['_all'], need_sub_task=False, page_num=page))
            if not isinstance(raw.get('list'), list):
                raise ScheduleError('节点列表不完整，请重新读取。')
            nodes.extend(raw['list'])
            pagination = raw.get('pagination') or {}
            if type(pagination.get('has_more')) is not bool:
                raise ScheduleError('节点分页信息缺失，请重新读取。')
            if 'page_num' in pagination and pagination['page_num'] != page:
                raise ScheduleError('节点页码不一致，请重新读取。')
            keys = [node.get('basic', {}).get('node_key') for node in nodes]
            if any(not isinstance(key, str) or not key for key in keys) or len(set(keys)) != len(keys):
                raise ScheduleError('节点标识缺失或重复，请重新读取。')
            if not pagination['has_more']:
                if 'total' in pagination and (type(pagination['total']) is not int or pagination['total'] != len(nodes)):
                    raise ScheduleError('节点分页数量不完整，请重新读取。')
                break
            page += 1
            if page > 50:
                raise ScheduleError('节点数量过多，请打开原工作台操作。')
        return nodes

    def context(self, data):
        identifier = self.identifier(data.get('work_item_id'))
        node_id = data.get('node_id')
        if not isinstance(node_id, str) or not node_id or len(node_id) > 200:
            raise ScheduleError('请从列表选择有效的节点。')
        user = self.user()
        nodes = self.nodes(identifier)
        matched = [n for n in nodes if node_id in (n['basic'].get('node_key'), n['basic'].get('node_uuid'))]
        if len(matched) != 1:
            raise ScheduleError('节点已变化或无法唯一匹配，请刷新列表。')
        node = matched[0]
        owners = [str(o['user_key']) for o in node.get('assignees', {}).get('owners', [])]
        if user['key'] not in owners:
            raise ScheduleError('仅允许编辑当前飞书用户本人负责节点的个人排期。')
        personal = [p for p in node.get('assignee_schedule_list') or []
                    if str(p.get('assignee', {}).get('user_key')) == user['key']]
        if len(personal) > 1:
            raise ScheduleError('本人排期存在多条记录，请在原工作台核对。')
        # A shared aggregate without a personal record cannot safely be edited for one person.
        if not personal and len(owners) > 1:
            raise ScheduleError('该节点为多人共享排期，未找到独立的本人记录，请在原工作台操作。')
        info = personal[0]['schedule_info'] if personal else node.get('schedule', {})
        others = [p for p in node.get('assignee_schedule_list') or []
                  if str(p.get('assignee', {}).get('user_key')) != user['key']]
        fingerprint = hashlib.sha256(json.dumps(dict(node=node['basic'], owners=owners,
            mode=node.get('assignees', {}).get('node_owner_mode'), personal=info, others=others), sort_keys=True).encode()).hexdigest()
        return dict(user=user, work_item_id=identifier, node_id=node['basic']['node_key'],
                    node_name=node['basic']['name'], before=values(info), fingerprint=fingerprint,
                    planned_construction_period=info.get('planned_construction_period'), others=others)

    def detail(self, data):
        item = self.context(data)
        result = {k: item[k] for k in ('user', 'work_item_id', 'node_id', 'node_name', 'before', 'fingerprint')}
        result.update(self.link(data))
        return result

    def workitem(self, data):
        self.ensure_enabled()
        identifier = self.identifier(data.get('work_item_id'))
        raw = self.client.call('get_workitem_brief', dict(project_key=self.space_id, work_item_id=identifier))
        attributes = raw.get('work_item_attribute', {})
        if attributes.get('owned_project', {}).get('key') != self.space_id:
            raise ScheduleError('该工作项不属于当前关联的飞书空间。')
        return attributes

    def link(self, data):
        identifier = self.identifier(data.get('work_item_id'))
        attributes = self.workitem(data)
        kind = attributes.get('work_item_type', {}).get('key')
        if not isinstance(kind, str) or not kind:
            raise ScheduleError('未能获取工作项类型，请打开原工作台。')
        return {'url': self.project_url(self.config['work_item_api'] if kind == self.config['work_item_type'] else kind, identifier)}

    def requirement(self, data):
        identifier = self.identifier(data.get('work_item_id'))
        attributes = self.workitem(data)
        if attributes.get('work_item_type', {}).get('key') != self.config['work_item_type']:
            raise ScheduleError('该工作项不是当前项目配置的开发需求类型。')
        title = attributes.get('work_item_name')
        if not isinstance(title, str) or not title.strip():
            raise ScheduleError('未能获取正式需求名称，未启动开发任务。')
        return dict(work_item_id=identifier, title=title,
                    url=self.project_url(self.config['work_item_api'], identifier))

    def preview(self, data):
        item = self.context(data)
        if data.get('fingerprint') != item['fingerprint']:
            raise ScheduleError('打开表单后排期已变化，请取消并重新打开编辑。')
        inputs = data.get('values')
        if not isinstance(inputs, dict) or set(inputs) != set(FIELDS):
            raise ScheduleError('排期表单字段不完整，请重新打开编辑。')
        after = dict(start=parse_date(inputs['start']).isoformat() if inputs['start'] != '' else '',
                     end=parse_date(inputs['end']).isoformat() if inputs['end'] != '' else '',
                     points=number(inputs['points']), actual=number(inputs['actual']))
        if bool(after['start']) != bool(after['end']) or (after['start'] and after['start'] > after['end']):
            raise ScheduleError('起止日期需同时填写或同时清空，结束日期不能早于开始日期。')
        changes = [dict(field=key, label=LABELS[key], before=item['before'][key], after=after[key])
                   for key in FIELDS if item['before'][key] != after[key]]
        if not changes:
            raise ScheduleError('没有需要保存的改动。')
        clear = any(change['after'] in (None, '') for change in changes)
        mapping = dict(start='estimate_start_date', end='estimate_end_date', points='points', actual='actual_work_time')
        schedule = dict(owners=[item['user']['key']], clear_schedule=clear, is_auto=False)
        for key in FIELDS:
            if after[key] not in (None, '') and (clear or item['before'][key] != after[key]):
                schedule[mapping[key]] = timestamp(after[key]) if key in ('start', 'end') else after[key]
        if clear and item['planned_construction_period'] is not None:
            schedule['planned_construction_period'] = item['planned_construction_period']
        self.plans = {k: v for k, v in self.plans.items() if v['expires'] > time.monotonic()}
        if len(self.plans) >= 100:
            raise ScheduleError('待保存预览过多，请稍后重试。')
        identifier = secrets.token_urlsafe(24)
        self.plans[identifier] = dict(**item, space_id=self.space_id, after=after, schedule=schedule, expires=time.monotonic() + 300)
        return dict(plan=identifier, changes=changes, before=item['before'], after=after,
                    node_name=item['node_name'], user=item['user'])

    def save(self, data):
        identifier = data.get('plan')
        if not isinstance(identifier, str):
            raise ScheduleError('请先预览排期修改。')
        plan = self.plans.pop(identifier, None)
        if not plan or plan['expires'] <= time.monotonic() or plan['space_id'] != self.space_id:
            raise ScheduleError('预览已失效，请重新预览。')
        current = self.context(plan)
        if current['user']['key'] != plan['user']['key'] or current['fingerprint'] != plan['fingerprint']:
            raise ScheduleError('飞书排期或负责人已发生变化，请重新打开编辑，避免覆盖新修改。')
        try:
            self.client.call('update_node', dict(project_key=self.space_id, work_item_id=plan['work_item_id'],
                             node_id=plan['node_id'], schedules=[plan['schedule']]))
            latest = self.context(plan)
        except ScheduleError:
            raise ScheduleError('保存结果未确认，请先重新读取排期或打开飞书核对，勿重复提交。') from None
        if latest['before'] != plan['after']:
            raise ScheduleError('已发送保存，但回读与预期不一致，请先刷新或打开飞书核对，勿重复提交。')
        if latest['others'] != plan['others']:
            raise ScheduleError('保存后其他人员排期发生变化，请先打开飞书核对，不自动回退或重复提交。')
        return dict(saved=True, before=latest['before'], message='已保存，并从飞书重新读取核验。')

    def handle(self, route, data):
        handlers = {'metadata': self.metadata, 'overview': self.overview, 'release-overview': self.release_overview,
                    'releases': self.releases, 'detail': self.detail, 'link': self.link,
                    'preview': self.preview, 'save': self.save}
        if route not in handlers:
            raise ScheduleError('排期接口不存在。')
        return handlers[route](data)
