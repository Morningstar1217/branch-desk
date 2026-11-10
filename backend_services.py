"""One shared Gradle/Spring Boot service per Git repository; no implicit restart."""
import hashlib
import platform_support as platform
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import time

from workbench_runtime import RUNTIME, RuntimeError as RuntimeFileError, checked, directory
from worktree_services import WorktreeServices, process_inventory, process_identities


DEFAULTS = dict(script='bootRun', mode='test', port_strategy='fixed', port=8185,
                test_backend='', java_home='', config_file='')


def backend_options(path):
    root = Path(path)
    build = root / 'build.gradle'
    if not build.is_file() or not (root / 'gradlew').is_file():
        return None
    try:
        source = build.read_text()
        if 'org.springframework.boot' not in source:
            return None
        mains = []
        for file in (root / 'src/main/java').rglob('*Application.java'):
            text = file.read_text()
            package = re.search(r'^package\s+([\w.]+)\s*;', text, re.M)
            if package and '@SpringBootApplication' in text and 'static void main(' in text:
                mains.append(package[1] + '.' + file.stem)
    except (OSError, UnicodeError):
        return None
    if len(mains) != 1:
        return None
    return dict(supported=True, preset='spring-boot', kind='backend', main_class=mains[0],
                scripts=[dict(name='bootRun', command='./gradlew --no-daemon bootRun')],
                defaults=dict(DEFAULTS), backend_override=False, reason='')


def validate_backend_config(path, config):
    if not isinstance(config, dict) or set(config) - set(DEFAULTS):
        raise ValueError('后端服务设置字段无效。')
    result = {**DEFAULTS, **config}
    if (result['script'] != 'bootRun' or result['port_strategy'] != 'fixed' or
            type(result['port']) is not int or not 1024 <= result['port'] <= 65535 or
            not isinstance(result['mode'], str) or not re.fullmatch(r'[\w-]{1,64}', result['mode']) or
            result['test_backend'] != ''):
        raise ValueError('后端使用 bootRun、单一 profile 和固定端口（1024–65535）。')
    for field in ('java_home', 'config_file'):
        value = result[field]
        if not isinstance(value, str) or len(value) > 4096 or any(c in value for c in '\r\n\0'):
            raise ValueError('Java 与配置文件路径无效。')
        if value and not Path(value).is_absolute():
            raise ValueError('Java 与配置文件须填写绝对路径。')
    if not backend_options(path):
        raise ValueError('未识别到唯一 Spring Boot 入口及 Gradle wrapper，请检查项目。')
    return result


def launch_spec(path, settings):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('GIT_', 'VITE_', 'SPRING_', 'SERVER_'))}
    # Do not inherit unrelated JVM flags that can override the reviewed configuration.
    for key in ('JAVA_TOOL_OPTIONS', '_JAVA_OPTIONS', 'JDK_JAVA_OPTIONS', 'GRADLE_OPTS', 'JAVA_OPTS'):
        env.pop(key, None)
    if settings['java_home']:
        java = Path(settings['java_home']) / ('bin/java.exe' if platform.IS_WINDOWS else 'bin/java')
        if not java.is_file() or not os.access(java, os.X_OK):
            raise ValueError('Java Home 下没有可执行的 bin/java。')
        env['JAVA_HOME'] = Path(settings['java_home']).as_posix() if platform.IS_WINDOWS else settings['java_home']
        env['PATH'] = str(java.parent) + os.pathsep + env.get('PATH', '')
    elif not shutil.which('java'):
        raise ValueError('未找到 Java，请在项目设置中填写兼容的 Java Home。')
    env.update(SPRING_PROFILES_ACTIVE=settings['mode'], SERVER_PORT=str(settings['port']),
               SERVER_ADDRESS='127.0.0.1', SPRING_CLOUD_NACOS_DISCOVERY_REGISTER_ENABLED='false')
    if settings['config_file']:
        config = Path(settings['config_file'])
        if not config.is_file() or config.suffix not in ('.properties', '.yml', '.yaml'):
            raise ValueError('本地覆盖配置须为存在的 properties/yml/yaml 文件。')
        env['SPRING_CONFIG_ADDITIONAL_LOCATION'] = config.as_uri()
    # No shell expansion; the wrapper is executed by sh even when its executable bit is absent.
    wrapper = (Path(path) / 'gradlew').as_posix() if platform.IS_WINDOWS else str(Path(path) / 'gradlew')
    return [platform.bash_binary() if platform.IS_WINDOWS else '/bin/sh', wrapper, '--no-daemon', 'bootRun'], env


def launcher_identity(identity):
    birth = identity.split(' ', 1)[0] if platform.IS_WINDOWS else ' '.join(identity.split()[:5])
    return hashlib.sha256(birth.encode()).hexdigest()


def matches_launcher(record, identity):
    # Accept exact legacy records as well as the exec-stable creation-time format.
    return record.get('identity') in (launcher_identity(identity), hashlib.sha256(identity.encode()).hexdigest())


class BackendServices(WorktreeServices):
    def __init__(self, repo, config=None):
        super().__init__(repo, config)
        self.start_plans = {}

    def launch_observed(self, processes, job):
        # bootRun's HTTP listener is a child JVM, not the Gradle wrapper PID.
        return any(job['port'] in item['ports'] for item in processes)

    def inventory(self):
        options = backend_options(self.repo.root)
        if not options:
            raise ValueError('后端启动入口已变化，请重新检查项目。')
        processes = process_inventory(java_main=options['main_class'])
        uncertain = []
        # The Gradle wrapper can be alive during compilation, before Java opens a port.
        for path, record in self.records.items():
            if record.get('kind') != 'backend':
                continue
            try:
                item = dict(pid=record['pid'], cwd=path, ports=[])
                identity = process_identities([item])[0]['identity']
                if not matches_launcher(record, identity):
                    if record.get('identity_format') != 'creation-time':
                        uncertain.append(path)
                    continue
                if not any(value['pid'] == item['pid'] for value in processes):
                    processes.append(item)
            except ValueError:
                continue
        if uncertain:
            raise ValueError('旧版后端启动记录与现存进程不一致，请先核实该工作区的 Gradle/Java 进程，不会重复启动：' + '、'.join(uncertain))
        return processes

    def evidence(self, path, processes, identities, probes):
        record = self.records.get(path, {})
        rows = []
        for item in processes:
            identity = identities.get(item['pid'], '')
            managed = bool(record.get('kind') == 'backend' and record.get('pid') == item['pid'] and
                           record.get('cwd') == item['cwd'] and identity and
                           matches_launcher(record, identity))
            rows.append(dict(pid=item['pid'], ports=item['ports'], backends=[], managed_launcher=managed))
        return rows

    def status(self, force=False):
        result = super().status(force)
        for path, item in result['items'].items():
            record = self.records.get(path, {})
            managed = any(row.get('managed_launcher') for row in item.get('processes', []))
            item.update(kind='backend', backend_label=(f"本地后端 · {record.get('profile', '待确认')}" if managed
                                                      else 'Java 后端 · 配置待核实'))
            if managed:
                if item['status'] == 'running' and record['port'] not in item['ports']:
                    item['status'] = 'starting'
                item.update(log_path=record['log_path'], profile=record.get('profile'),
                            backend=f"http://127.0.0.1:{record['port']}", backend_source='launch')
            if not item.get('pids'):
                item.pop('backend', None)
            item['shared'] = True
        return result

    def start_context(self, data):
        path = self.repo.target(data.get('path'))
        tree = next((item for item in self.repo.worktrees() if str(Path(item['path']).resolve()) == path), None)
        if not tree or not data.get('branch') or tree['branch'] != data['branch'] or any(
                tree.get(key) for key in ('missing', 'locked', 'prunable')):
            raise ValueError('分支或工作区已变化，请重新检查。')
        self.repo.resolver.guard()
        state = self.repo.snapshot(path)
        if state['branch'] != data['branch'] or state['operations'] or state['conflicts']:
            raise ValueError('工作区分支已变化或存在 Git 操作/冲突，请先处理。')
        settings = validate_backend_config(path, self.config)
        launch_spec(path, settings)
        files = [Path(path) / 'build.gradle', Path(path) / 'gradlew']
        resources = Path(path) / 'src/main/resources'
        files.extend(sorted(file for file in resources.glob('application*')
                            if file.is_file() and file.suffix in ('.properties', '.yml', '.yaml')))
        if settings['config_file']:
            files.append(Path(settings['config_file']))
        try:
            fingerprints = {str(file): hashlib.sha256(file.read_bytes()).hexdigest() for file in files}
        except OSError:
            raise ValueError('无法核实后端启动配置，请检查文件权限。') from None
        return dict(path=path, branch=tree['branch'], state=state, settings=settings, fingerprints=fingerprints)

    def require_stopped(self):
        current = self.status()
        if current.get('error'):
            raise ValueError(current['error'])
        active = [path for path, item in current['items'].items() if item['status'] not in ('stopped', 'failed')]
        if active:
            raise ValueError('已有共用后端正在运行或状态未确认，请复用或先明确停止：' + '、'.join(active))
        if self.record_error:
            raise ValueError(self.record_error)

    def preview_start(self, data):
        with self.lock:
            context = self.start_context(data)
            self.require_stopped()
            token = secrets.token_urlsafe(24)
            self.start_plans = {key: value for key, value in self.start_plans.items()
                                if time.monotonic() - value['created'] < 600}
            self.start_plans[token] = dict(context=context, created=time.monotonic())
            return dict(plan=token, path=context['path'], branch=context['branch'], settings=context['settings'])

    def start(self, data):
        with self.lock:
            token = data.get('plan')
            plan = self.start_plans.pop(token, None) if isinstance(token, str) else None
            if data.get('confirmed') is not True or not plan or time.monotonic() - plan['created'] >= 600:
                raise ValueError('请先预览并确认后端启动配置。')
            context = self.start_context(data)
            if context != plan['context']:
                raise ValueError('代码或启动配置已变化，请重新预览。')
            self.require_stopped()
            path, settings = context['path'], context['settings']
            with socket.socket() as probe:
                try:
                    probe.bind(('127.0.0.1', settings['port']))
                except OSError:
                    raise ValueError('共用后端固定端口已占用，请检查原服务；不会另选端口启动第二份。') from None
            command, env = launch_spec(path, settings)
            log_path = RUNTIME / (hashlib.sha256(path.encode()).hexdigest() + '.backend.log')
            try:
                directory(create=True)
                with os.fdopen(checked(log_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC), 'ab') as log:
                    process = subprocess.Popen(command, cwd=path, env=env, stdin=subprocess.DEVNULL,
                                               stdout=log, stderr=subprocess.STDOUT, close_fds=True, **platform.child_options())
            except (OSError, subprocess.SubprocessError, RuntimeFileError):
                raise ValueError('后端未能启动，请检查 Java、Gradle wrapper 和日志目录权限。') from None
            self.jobs[path] = dict(process=process, backend=f"http://127.0.0.1:{settings['port']}", mode='project',
                                   port=settings['port'], log_path=str(log_path), started=time.monotonic())
            try:
                identity = process_identities([dict(pid=process.pid, cwd=path, ports=[])])[0]['identity']
                self.records[path] = dict(pid=process.pid, cwd=path, identity=launcher_identity(identity),
                                          port=settings['port'], backend='', mode='project', log_path=str(log_path),
                                          kind='backend', profile=settings['mode'], identity_format='creation-time')
                self.save_records()
            except (OSError, ValueError, RuntimeFileError):
                self.record_error = '后端已启动但启动记录未保存，请检查日志与状态，不要重复启动。'
            return self.status()

    def stop_context(self, path, branch):
        context = super().stop_context(path, branch)
        # A verified session leader owns its entire bootRun process group, including compilation children.
        record = self.records.get(context['path'], {})
        if record.get('kind') == 'backend' and all(item['pid'] == record.get('pid') for item in context['processes']):
            raise ValueError('后端仍在构建，尚未出现可确认的应用进程；请等待启动或构建结束并检查日志。')
        # Stop the application before its Gradle launcher.
        context['processes'].sort(key=lambda item: item['pid'] == record.get('pid'))
        for process in context['processes']:
            if (record.get('kind') == 'backend' and record.get('pid') == process['pid'] and
                    launcher_identity(process['identity']) == record.get('identity')):
                try:
                    if platform.IS_WINDOWS:
                        process['owned_tree'] = True
                    elif os.getpgid(process['pid']) == process['pid'] and os.getsid(process['pid']) == process['pid']:
                        process['group'] = process['pid']
                except ProcessLookupError:
                    raise ValueError('后端进程已变化，请重新检查。') from None
        return context

    def signal_process(self, process):
        if platform.IS_WINDOWS:
            platform.terminate_tree(process['pid'])
        elif process.get('group'):
            os.killpg(process['group'], signal.SIGTERM)
        else:
            os.kill(process['pid'], signal.SIGTERM)

    def guard_update(self, path):
        self.require_stopped()
