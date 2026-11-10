"""Discover worktree services and explicitly launch Vue/Vite development servers."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import platform_support as platform
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import shutil
import socket
import subprocess
import threading
import tempfile
import time
from urllib.parse import urlsplit

from workbench_runtime import RUNTIME, RuntimeError as RuntimeFileError, checked, directory, process_alive


def backend_origin(value):
    if not isinstance(value, str) or len(value) > 2048 or re.search(r'[\s\\\x00-\x1f\x7f]', value):
        raise ValueError('后端地址须为 HTTP/HTTPS 地址，例如 http://192.168.1.10:8185。')
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None or
                parsed.password is not None or parsed.path not in ('', '/') or parsed.query or parsed.fragment or
                port == 0 or not re.fullmatch(r'[A-Za-z0-9.\-:\[\]]+', parsed.hostname)):
            raise ValueError()
    except ValueError:
        raise ValueError('后端地址只填写协议、主机和端口，不包含账号、密码、路径、查询参数或片段。') from None
    return value.rstrip('/')


def lsof_records(output):
    """NUL fields preserve worktree names containing spaces and newlines."""
    records, current = [], None
    for field in output.split('\0'):
        field = field.lstrip('\n')
        if field.startswith('p') and field[1:].isdigit():
            current = {'pid': int(field[1:]), 'names': []}
            records.append(current)
        elif current is not None and field.startswith('n'):
            current['names'].append(field[1:])
        elif current is not None and field.startswith('c'):
            current['command'] = field[1:]
    return records


def process_inventory(java_main=None):
    if platform.IS_WINDOWS:
        return platform.service_inventory(java_main)
    lsof = shutil.which('lsof')
    if not lsof:
        raise ValueError('无法检查服务状态：缺少 lsof，未启动服务。')
    try:
        cwd = subprocess.run([lsof, '-nP', '-a', '-d', 'cwd', '-F0pn'], capture_output=True, text=True, timeout=8)
        listeners = subprocess.run([lsof, '-nP', '-iTCP', '-sTCP:LISTEN', '-F0pcn'], capture_output=True, text=True, timeout=8)
        commands = subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True, timeout=5)
        if cwd.returncode or commands.returncode or listeners.returncode not in (0, 1) or cwd.stderr.strip() or listeners.stderr.strip():
            raise ValueError('进程信息读取不完整')
    except (OSError, subprocess.SubprocessError, ValueError):
        raise ValueError('无法确认服务状态，请重新检查；未启动新服务。') from None
    command_map = dict(line.strip().split(None, 1) for line in commands.stdout.splitlines() if len(line.strip().split(None, 1)) == 2)
    ports, bindings = {}, {}
    for record in lsof_records(listeners.stdout):
        ports[record['pid']] = sorted({int(match[1]) for name in record['names'] if (match := re.search(r':(\d+)$', name))})
        bindings[record['pid']] = [dict(host=match[1], port=int(match[2])) for name in record['names']
                                   if (match := re.fullmatch(r'(.+):(\d+)', name))]
    result = []
    directories = {record['pid']: record['names'] for record in lsof_records(cwd.stdout)}
    for pid, command in command_map.items():
        # Match Vite or the selected Java entrypoint, excluding unrelated listeners.
        vite_match = re.match(r'\S*(?:node|nodejs)\s+(?:--\S+\s+)*(?:(?!\s--).)*?/(?:vite/bin/vite\.js|\.bin/vite)(?:\s|$)', command)
        java_match = java_main and re.match(r'\S*java(?:\s|$)', command) and re.search(r'(?:^|\s)' + re.escape(java_main) + r'(?:\s|$)', command)
        if not (java_match if java_main else vite_match):
            continue
        names = directories.get(int(pid))
        if not names or not names[0].startswith('/'):
            raise ValueError('无法确认开发服务进程的工作区，请重新检查。')
        mode = re.search(r'(?:^|\s)--mode(?:=|\s+)([A-Za-z0-9_.-]+)(?=\s|$)', command)
        result.append(dict(pid=int(pid), cwd=str(Path(names[0]).resolve()), ports=ports.get(int(pid), []),
                           bindings=bindings.get(int(pid), []), mode=mode[1] if mode else 'development'))
    return result


def group_processes(trees, processes):
    roots = sorted(((Path(tree['path']).resolve(), tree['path']) for tree in trees), key=lambda item: len(item[0].parts), reverse=True)
    result = {tree['path']: [] for tree in trees}
    for process in processes:
        cwd = Path(process['cwd'])
        owner = next((key for root, key in roots if cwd == root or root in cwd.parents), None)
        if owner is not None:
            result[owner].append(process)
    return result


def process_identities(processes):
    if platform.IS_WINDOWS:
        return platform.process_identities(processes)
    pids = sorted(process['pid'] for process in processes)
    try:
        result = subprocess.run(['ps', '-p', ','.join(map(str, pids)), '-o', 'pid=,lstart=,command='],
                                capture_output=True, text=True, timeout=5, env={**os.environ, 'LC_ALL': 'C'})
        identities = {}
        for line in result.stdout.splitlines():
            fields = line.strip().split(None, 6)
            if len(fields) != 7 or not fields[0].isdigit():
                raise ValueError()
            identities[int(fields[0])] = ' '.join(fields[1:])
        if result.returncode or result.stderr.strip() or set(identities) != set(pids):
            raise ValueError()
    except (OSError, subprocess.SubprocessError, ValueError):
        raise ValueError('服务进程已变化或身份无法确认，请重新检查后再停止。') from None
    return [dict(process, identity=identities[process['pid']]) for process in sorted(processes, key=lambda item: item['pid'])]


SERVICE_DEFAULTS = dict(script='', mode='test', port_strategy='auto', port=8080, test_backend='')
PROXY_ROUTE = '/BIMgntServ-abroad/'


def project_details(path):
    root = Path(path)
    try:
        package = json.loads((root / 'package.json').read_text())
        if not isinstance(package, dict):
            raise ValueError()
    except (OSError, ValueError):
        return {}, '', False
    config_path = next((root / name for name in ('vite.config.ts', 'vite.config.js', 'vite.config.mts', 'vite.config.mjs')
                        if (root / name).is_file()), None)
    try:
        config = config_path.read_text() if config_path else ''
    except OSError:
        config = ''
    bibase = package.get('name') == 'bim-web' and all(key in config for key in ('viteEnv', 'VITE_PROXY_TARGET', 'VITE_BASE_API'))
    return package, config, bibase


def vite_script(command):
    if not isinstance(command, str) or len(command) > 4096 or re.search(r'[;&|`$<>\n\r]', command):
        return None
    try:
        args = shlex.split(command)
    except ValueError:
        return None
    if not args or args[0] != 'vite':
        return None
    index = 1
    while index < len(args):
        value = args[index]
        key, _, inline = value.partition('=')
        if key not in ('--mode', '--host', '--port', '--strictPort'):
            return None
        if key == '--strictPort':
            if inline:
                return None
            index += 1
            continue
        if not inline and (index + 1 == len(args) or args[index + 1].startswith('-')):
            if key == '--host':
                index += 1
                continue
            return None
        index += 1 if inline else 2
    return args


def service_options(path):
    from backend_services import backend_options
    backend = backend_options(path)
    if backend:
        return backend
    package, config, bibase = project_details(path)
    scripts = package.get('scripts', {})
    scripts = scripts if isinstance(scripts, dict) else {}
    choices = [dict(name=name, command=command) for name, command in scripts.items()
               if isinstance(name, str) and vite_script(command) is not None]
    installed = [value for value in (package.get('dependencies'), package.get('devDependencies')) if isinstance(value, dict)]
    dependencies = {key for value in installed for key in value}
    supported = bibase or bool(choices and 'vue' in dependencies and 'vite' in dependencies)
    defaults = dict(SERVICE_DEFAULTS)
    if bibase:
        defaults.update(mode='dev', test_backend='http://127.0.0.1:8185')
    elif choices:
        defaults['script'] = choices[0]['name']
    backend_override = supported and 'VITE_PROXY_TARGET' in config
    return dict(supported=supported, preset='bibase' if bibase else 'vite' if supported else 'unsupported',
                scripts=choices, defaults=defaults, backend_override=backend_override,
                reason='' if supported else '自动启动目前支持 Vue/Vite 的直接 vite 脚本；其他项目仍可管理分支和 worktree。')


def validate_service_config(path, config):
    from backend_services import backend_options, validate_backend_config
    if backend_options(path):
        return validate_backend_config(path, config)
    if not isinstance(config, dict) or set(config) - set(SERVICE_DEFAULTS):
        raise ValueError('开发服务设置字段无效。')
    result = {**SERVICE_DEFAULTS, **config}
    if (not isinstance(result['script'], str) or len(result['script']) > 200 or
            not isinstance(result['mode'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', result['mode']) or
            result['mode'] == 'local' or result['port_strategy'] not in ('auto', 'fixed') or
            type(result['port']) is not int or not 1024 <= result['port'] <= 65535):
        raise ValueError('请选择有效脚本、模式和端口策略；固定端口范围为 1024–65535。')
    options = service_options(path)
    if result['script'] and result['script'] not in {item['name'] for item in options['scripts']}:
        raise ValueError('启动脚本已变化或不受支持，请重新读取 package.json 后选择。')
    if result['test_backend'] != '':
        result['test_backend'] = backend_origin(result['test_backend'])
        if not options['backend_override']:
            raise ValueError('此项目未声明 VITE_PROXY_TARGET，无法安全覆盖后端；请保留项目自己的环境配置。')
    return result


def configured_backend(path, mode, required=False):
    _, _, bibase = project_details(path)
    root = Path(path) / ('viteEnv' if bibase else '')
    if required and not (root / ('.env.' + mode)).is_file():
        raise ValueError('未识别到当前项目的测试环境配置，请检查 viteEnv/.env.test。')
    values = {}
    for name in ('.env', '.env.local', '.env.' + mode, '.env.' + mode + '.local'):
        try:
            lines = (root / name).read_text().splitlines()
        except FileNotFoundError:
            continue
        for line in lines:
            match = re.fullmatch(r'\s*(VITE_BASE_API|VITE_PROXY_TARGET)\s*=\s*(.*?)\s*', line)
            if match:
                values[match[1]] = match[2].strip('\'"')
    value = values.get('VITE_PROXY_TARGET') or (values.get('VITE_BASE_API', '') if bibase else '')
    return backend_origin(value) if value else ''


def vite_binary(path):
    root = Path(path)
    node = shutil.which('node')
    vite = next((parent / 'node_modules/vite/bin/vite.js' for parent in (root, *root.parents)
                 if (parent / 'node_modules/vite/bin/vite.js').is_file()), None)
    if not node or vite is None:
        raise ValueError('缺少 Node 或当前 worktree 可访问的 Vite 依赖，请先按项目约定安装依赖。')
    return node, str(vite)


def launch_config(path):
    if not project_details(path)[2]:
        raise ValueError('未识别到当前项目的测试环境配置，请检查 package.json、vite.config.ts 和 viteEnv/.env.test。')
    backend = configured_backend(path, 'test', required=True)
    if not backend:
        raise ValueError('未识别到当前项目的测试环境配置，请检查 viteEnv/.env.test。')
    return (*vite_binary(path), backend)


def probe_backend(port):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
    try:
        connection.request('HEAD', PROXY_ROUTE)
        response = connection.getresponse()
        value = response.getheader('x-real-url') or ''
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError()
        backend = backend_origin(parsed.scheme + '://' + parsed.netloc)
        return dict(backend=backend, source='runtime', label='运行时识别 · BIM 代理',
                    port=port, route=PROXY_ROUTE, http_status=response.status)
    except (OSError, ValueError, http.client.HTTPException):
        return dict(port=port, error='BIM 代理未返回可确认的 x-real-url；未跟随重定向，未验证其他代理。')
    finally:
        connection.close()


def lan_ipv4_addresses():
    """Read local interfaces without making an outbound connection."""
    try:
        if platform.IS_WINDOWS:
            psutil = platform.windows_psutil()
            stats = psutil.net_if_stats()
            candidates = [address.address for name, values in psutil.net_if_addrs().items()
                          if name in stats and stats[name].isup for address in values
                          if address.family == socket.AF_INET]
        else:
            command = shutil.which('ifconfig') or '/sbin/ifconfig'
            result = subprocess.run([command, '-a'], capture_output=True, text=True, timeout=3)
            if result.returncode:
                return []
            candidates, active = [], False
            for line in result.stdout.splitlines():
                if line and not line[0].isspace():
                    active = bool(re.search(r'<[^>]*\bUP\b', line))
                match = re.match(r'\s+inet\s+(\S+)', line)
                if active and match:
                    candidates.append(match[1])
        addresses = set()
        for value in candidates:
            address = ipaddress.IPv4Address(value)
            if any(address in ipaddress.IPv4Network(network) for network in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
                addresses.add(str(address))
        return sorted(addresses)
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def lan_service_urls(processes, addresses):
    urls = {}
    for process in processes:
        for binding in process.get('bindings', []):
            host, port = binding['host'], binding['port']
            hosts = addresses if host in ('*', '0.0.0.0') else [host] if host in addresses else []
            for address in hosts:
                urls.setdefault(port, set()).add(f'http://{address}:{port}/')
    return {port: sorted(values) for port, values in urls.items()}


def free_port(used):
    for port in range(8080, 8180):
        if port in used:
            continue
        with socket.socket() as probe:
            try:
                probe.bind(('0.0.0.0', port))
                return port
            except OSError:
                pass
    raise ValueError('8080–8179 没有空闲端口，请先自行处理端口占用。')


class WorktreeServices:
    def __init__(self, repo, config=None):
        self.repo = repo
        self.config = dict(config or {})
        self.jobs = {}
        self.stop_plans = {}
        self.stopping = {}
        self.lock = threading.RLock()
        self.records = {}
        self.record_error = ''
        self.probes = {}
        common = getattr(repo, 'common', '')
        self.record_path = RUNTIME / (hashlib.sha256(common.encode()).hexdigest() + '.services') if common else None
        self.read_records()

    def launch_observed(self, processes, job):
        return any(process['pid'] == job['process'].pid for process in processes)

    def inventory(self):
        return process_inventory()

    def signal_process(self, process):
        if platform.IS_WINDOWS:
            platform.terminate_tree(process['pid'])
            return
        os.kill(process['pid'], signal.SIGTERM)

    def read_records(self):
        if self.record_path is None:
            return
        try:
            with os.fdopen(checked(self.record_path, os.O_RDONLY)) as source:
                records = json.loads(source.read(1024 * 1024 + 1))
            if not isinstance(records, dict):
                raise ValueError()
            for path, record in records.items():
                if (not Path(path).is_absolute() or not isinstance(record, dict) or
                        type(record.get('pid')) is not int or record['pid'] <= 1 or
                        not isinstance(record.get('identity'), str) or not re.fullmatch(r'[0-9a-f]{64}', record['identity']) or
                        record.get('cwd') != path or type(record.get('port')) is not int or not 1 <= record['port'] <= 65535 or
                        record.get('mode') not in ('test', 'custom', 'project') or not isinstance(record.get('log_path'), str)):
                    raise ValueError()
                if record.get('backend'):
                    backend_origin(record['backend'])
            self.records = records
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, RuntimeFileError):
            self.record_error = '服务启动记录无法安全读取；已有服务仍可检查，修复记录前不启动新服务。'

    def save_records(self):
        if self.record_path is None:
            return
        directory(create=True)
        fd, temporary = tempfile.mkstemp(prefix='.service-', dir=RUNTIME)
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump(self.records, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.record_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def evidence(self, path, processes, identities, probes):
        rows = []
        record = self.records.get(path)
        for process in processes:
            row = dict(pid=process['pid'], ports=list(process['ports']), backends=[])
            identity = identities.get(process['pid'])
            matching = (record and identity and record['pid'] == process['pid'] and record['cwd'] == process['cwd'] and
                        record['identity'] == hashlib.sha256(identity.encode()).hexdigest() and
                        (not process['ports'] or record['port'] in process['ports']))
            for port in process['ports']:
                probe = probes.get((process['pid'], port))
                if probe and probe.get('backend'):
                    row['backends'].append(probe)
                elif probe:
                    row['backend_error'] = probe['error']
            if matching:
                row['launch'] = dict(mode=record['mode'], log_path=record['log_path'])
                if record.get('backend'):
                    row['backends'].append(dict(backend=record['backend'], source='launch', label='已核验启动记录', port=record['port']))
            if not row['backends']:
                try:
                    backend = configured_backend(process['cwd'], process.get('mode', 'development'))
                    if backend:
                        row['backends'].append(dict(backend=backend, source='config', label='配置推断 · 非运行证据'))
                except (OSError, ValueError):
                    row['backend_error'] = '当前环境配置不能用于推断后端地址。'
            if not row['backends'] and not row.get('backend_error'):
                row['backend_error'] = '未找到可验证的启动记录；此项目暂无受支持的运行时后端探测。'
            rows.append(row)
        return rows

    def status(self, force=False):
        with self.lock:
            trees = self.repo.worktrees()
            try:
                grouped = group_processes(trees, self.inventory())
            except ValueError as error:
                return dict(items={tree['path']: dict(status='unknown', ports=[], error=str(error)) for tree in trees}, error=str(error))
            all_processes = [process for values in grouped.values() for process in values]
            try:
                identities = {process['pid']: process['identity'] for process in process_identities(all_processes)} if all_processes else {}
            except ValueError:
                identities = {}
            probes, active_keys, pending = {}, set(), []
            for processes in grouped.values():
                for process in processes:
                    if not identities.get(process['pid']) or not project_details(process['cwd'])[2]:
                        continue
                    for port in process['ports']:
                        key = (process['pid'], identities.get(process['pid'], ''), port)
                        active_keys.add(key)
                        saved = self.probes.get(key)
                        if not force and key[1] and saved and time.monotonic() - saved[0] < 30:
                            probes[(process['pid'], port)] = saved[1]
                        else:
                            pending.append(key)
            if pending:
                with ThreadPoolExecutor(max_workers=4) as workers:
                    for key, result in zip(pending, workers.map(lambda key: probe_backend(key[2]), pending)):
                        self.probes[key] = (time.monotonic(), result)
                        probes[(key[0], key[2])] = result
                try:
                    rechecked = {process['pid']: process['identity'] for process in process_identities(all_processes)}
                except ValueError:
                    rechecked = {}
                for key in pending:
                    if rechecked.get(key[0]) != key[1]:
                        probes.pop((key[0], key[2]), None)
                        self.probes.pop(key, None)
            self.probes = {key: value for key, value in self.probes.items() if key in active_keys}
            addresses = lan_ipv4_addresses() if all_processes else []
            items = {}
            for tree in trees:
                path = tree['path']
                processes = grouped[path]
                ports = sorted({port for process in processes for port in process['ports']})
                item = dict(status='running' if ports else 'starting' if processes else 'stopped', ports=ports,
                            pids=[process['pid'] for process in processes], lan_urls=lan_service_urls(processes, addresses))
                job = self.jobs.get(path)
                if job:
                    code = job['process'].poll()
                    owned = self.launch_observed(processes, job)
                    if code is None:
                        item.update(backend=job['backend'], mode=job['mode'], log_path=job['log_path'])
                        if not ports or not owned:
                            item['status'] = 'starting'
                        if time.monotonic() - job['started'] > 90 and item['status'] == 'starting':
                            item['error'] = '启动尚未确认，请检查日志。进程仍存在，不会重复启动。'
                    elif not processes:
                        item.update(status='failed', error=f'服务已退出（退出码 {code}），请检查日志后重试。', log_path=job['log_path'])
                rows = self.evidence(path, processes, identities, probes)
                item['processes'] = rows
                preferred = []
                for row in rows:
                    evidence = row['backends']
                    preferred.extend([value for value in evidence if value['source'] == 'runtime'] or
                                     [value for value in evidence if value['source'] == 'launch'] or evidence)
                addresses = {value['backend'] for value in preferred}
                if len(addresses) == 1 and all(row['backends'] for row in rows):
                    item.update(backend=next(iter(addresses)), backend_source=preferred[0]['source'], backend_label=preferred[0]['label'])
                    if preferred[0]['source'] != 'launch':
                        item['mode'] = preferred[0]['source']
                    elif not item.get('mode'):
                        item['mode'] = next(row['launch']['mode'] for row in rows if row.get('launch'))
                    if any(row.get('launch') for row in rows):
                        item['log_path'] = next(row['launch']['log_path'] for row in rows if row.get('launch'))
                elif len(addresses) > 1 or (rows and not all(row['backends'] for row in rows)):
                    for field in ('backend', 'mode', 'backend_source'):
                        item.pop(field, None)
                    item['backend_label'] = '多个后端，见进程明细' if len(addresses) > 1 else '部分后端未确认'
                pending = self.stopping.get(path)
                if pending:
                    remaining = {pid for pid in pending['pids'] if process_alive(pid)}
                    if remaining:
                        item['status'] = 'stopping'
                        if time.monotonic() - pending['started'] > 15:
                            item['error'] = '停止请求已发送，但进程尚未退出；请检查对应终端或日志，不会强制结束。'
                    else:
                        self.stopping.pop(path, None)
                        job = self.jobs.get(path)
                        if job and job['process'].poll() is not None:
                            self.jobs.pop(path, None)
                        if not processes:
                            item = dict(status='stopped', ports=[], pids=[])
                items[path] = item
            return dict(items=items, error='', warning=self.record_error)

    def start(self, data):
        with self.lock:
            path = self.repo.target(data.get('path'))
            expected = data.get('branch')
            tree = next((tree for tree in self.repo.worktrees() if str(Path(tree['path']).resolve()) == path), None)
            if not tree or not expected or tree['branch'] != expected or tree.get('missing') or tree.get('locked') or tree.get('prunable'):
                raise ValueError('分支或 worktree 状态已变化，请刷新列表后重新启动。')
            self.repo.resolver.guard()
            snapshot = self.repo.snapshot(path)
            if snapshot['branch'] != expected:
                raise ValueError('工作区分支已切换，请刷新列表后重新启动。')
            if snapshot['operations'] or snapshot['conflicts']:
                raise ValueError('工作区有进行中的 Git 操作或冲突，请处理完成后再启动服务。')
            backend = data.get('backend', '')
            if backend != '':
                backend = backend_origin(backend)
            current = self.status()
            if current['error']:
                raise ValueError(current['error'])
            item = current['items'].get(tree['path'])
            if not item or item['status'] not in ('stopped', 'failed'):
                return current
            if self.record_error:
                raise ValueError(self.record_error)
            if not self.config:
                if not project_details(path)[2]:
                    raise ValueError('请先在项目设置中核实启动配置。')
                node, vite = vite_binary(path)
                test_backend = 'http://127.0.0.1:8185'
                settings, bibase, override = {**SERVICE_DEFAULTS, 'mode': 'dev'}, True, True
            else:
                settings = validate_service_config(path, self.config)
                options = service_options(path)
                if not options['supported']:
                    raise ValueError(options['reason'])
                if not settings['script'] and options['preset'] != 'bibase':
                    raise ValueError('请先在项目设置中选择 Vite 启动脚本。')
                node, vite = vite_binary(path)
                bibase, override = options['preset'] == 'bibase', options['backend_override']
                test_backend = settings['test_backend'] or ('http://127.0.0.1:8185' if bibase and settings['mode'] in ('dev', 'test') else '') or (configured_backend(
                    path, 'test' if bibase and settings['mode'] == 'dev' else settings['mode'],
                    required=bibase and settings['mode'] in ('dev', 'test')) if override else '')
                if bibase and settings['mode'] in ('dev', 'test') and not test_backend:
                    raise ValueError('未识别到当前项目的测试环境配置，请检查 viteEnv/.env.test。')
            if backend and not override:
                raise ValueError('此项目未声明 VITE_PROXY_TARGET，不支持临时后端覆盖。')
            backend = backend or test_backend
            used = {job['port'] for job in self.jobs.values() if job['process'].poll() is None}
            if settings['port_strategy'] == 'fixed':
                port = settings['port']
                if port in used:
                    raise ValueError('设置的固定端口已被工作台服务占用，请调整设置。')
                try:
                    with socket.socket() as probe:
                        probe.bind(('0.0.0.0', port))
                except OSError:
                    raise ValueError('设置的固定端口已被占用，请调整设置。') from None
            else:
                port = free_port(used)
            env = {key: value for key, value in os.environ.items() if not key.startswith(('VITE_', 'GIT_')) and key != 'NODE_ENV'}
            if bibase:
                env.update(VITE_BASE_API='', VITE_BIM_API_BASE='', VITE_USE_HTTPS='', VITE_PUBLIC_PATH='/',
                           VITE_LOCAL_SSO_PROXY_REWRITE='true')
            if backend:
                env['VITE_PROXY_TARGET'] = backend
            log_path = RUNTIME / (hashlib.sha256(path.encode()).hexdigest() + '.service.log')
            try:
                directory(create=True)
                with os.fdopen(checked(log_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC), 'ab') as log:
                    process = subprocess.Popen([node, vite, '--mode', settings['mode'], '--host', '0.0.0.0', '--port', str(port), '--strictPort'],
                                               cwd=path, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                               close_fds=True, **platform.child_options())
            except (OSError, subprocess.SubprocessError, RuntimeFileError):
                raise ValueError('服务未能启动，请检查 Node、依赖和工作台日志目录权限。') from None
            self.jobs[path] = dict(process=process, backend=backend, mode='custom' if data.get('backend') else 'test' if settings['mode'] == 'test' or (bibase and settings['mode'] == 'dev') else 'project',
                                   port=port, log_path=str(log_path), started=time.monotonic())
            if self.record_path is not None:
                try:
                    identity = process_identities([dict(pid=process.pid, cwd=path, ports=[])])[0]['identity']
                    self.records[path] = dict(pid=process.pid, cwd=path, identity=hashlib.sha256(identity.encode()).hexdigest(),
                                              port=port, backend=backend, mode=self.jobs[path]['mode'], log_path=str(log_path))
                    self.save_records()
                except (OSError, ValueError, RuntimeFileError):
                    self.record_error = '服务已启动，但启动记录未能保存或进程身份尚未确认；重启后将重新检测，勿重复启动。'
                    current['warning'] = self.record_error
            current['items'][tree['path']] = dict(status='starting', ports=[], pids=[process.pid], backend=backend,
                                                 mode=self.jobs[path]['mode'], log_path=str(log_path))
            return current

    def stop_context(self, path, branch):
        path = self.repo.target(path)
        trees = self.repo.worktrees()
        tree = next((tree for tree in trees if str(Path(tree['path']).resolve()) == path), None)
        if not tree or not branch or tree['branch'] != branch or tree.get('missing'):
            raise ValueError('工作区分支已变化，请刷新后重新检查。')
        self.repo.resolver.guard()
        processes = group_processes(trees, self.inventory())[tree['path']]
        if not processes:
            raise ValueError('没有可停止的开发服务，请重新检查状态。')
        return dict(path=path, branch=branch, processes=process_identities(processes))

    def preview_stop(self, data):
        with self.lock:
            context = self.stop_context(data.get('path'), data.get('branch'))
            pending = self.stopping.get(context['path'])
            if pending and any(process_alive(pid) for pid in pending['pids']):
                raise ValueError('停止请求已发送，请先重新检查状态，避免重复操作。')
            self.stop_plans = {key: value for key, value in self.stop_plans.items() if time.monotonic() - value['created'] < 600}
            if len(self.stop_plans) >= 100:
                self.stop_plans.clear()
            token = secrets.token_urlsafe(24)
            self.stop_plans[token] = dict(context=context, created=time.monotonic())
            return dict(plan=token, path=context['path'], branch=context['branch'],
                        termination='force-tree' if platform.IS_WINDOWS else 'signal',
                        processes=[dict(pid=item['pid'], ports=item['ports']) for item in context['processes']])

    def stop(self, data):
        with self.lock:
            if data.get('confirmed') is not True:
                raise ValueError('请先核对服务并确认停止。')
            token = data.get('plan')
            plan = self.stop_plans.pop(token, None) if isinstance(token, str) else None
            if not plan or time.monotonic() - plan['created'] >= 600:
                raise ValueError('停止预览已失效，请重新检查。')
            context = plan['context']
            if self.stop_context(context['path'], context['branch']) != context:
                raise ValueError('服务进程、端口或工作区已变化，请重新检查后再停止。')
            pids = []
            for process in context['processes']:
                # The service implementation only signals rechecked owned processes.
                try:
                    self.signal_process(process)
                    pids.append(process['pid'])
                except ProcessLookupError:
                    continue
                except OSError:
                    if pids:
                        self.stopping[context['path']] = dict(pids=pids, started=time.monotonic())
                    raise ValueError('部分服务停止请求未能发送，请重新检查当前状态；不会强制结束或自动重试。') from None
            self.stopping[context['path']] = dict(pids=pids, started=time.monotonic())
            return self.status()
