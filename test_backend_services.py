"""Backend lifecycle regressions with fake processes: no service or network is started."""
import hashlib
import io
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend_services import BackendServices, backend_options, launch_spec, validate_backend_config, DEFAULTS
from worktree_services import service_options, validate_service_config


class BackendServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'build.gradle').write_text("apply plugin: 'org.springframework.boot'")
        (self.root / 'gradlew').write_text('# fixture, never executed')
        src = self.root / 'src/main/java/example'
        src.mkdir(parents=True)
        (src / 'TestApplication.java').write_text('package example;\n@SpringBootApplication\nclass TestApplication { public static void main(String[] args) {} }')
        self.path = str(self.root)
        self.tree = dict(path=self.path, branch='feat/backend', missing=False)
        self.snapshot = dict(branch='feat/backend', head='a' * 40, operations=[], conflicts=[], status='')
        self.repo = SimpleNamespace(root=self.path, common='', worktrees=Mock(return_value=[self.tree]),
                                    target=Mock(return_value=self.path), resolver=SimpleNamespace(guard=Mock()),
                                    snapshot=Mock(side_effect=lambda path: dict(self.snapshot)))
        self.manager = BackendServices(self.repo)
        self.data = dict(path=self.path, branch=self.tree['branch'])
        self.status = dict(items={self.path: dict(status='stopped', ports=[])}, error='')
        self.status_patch = patch.object(self.manager, 'status', return_value=self.status)
        self.status_mock = self.status_patch.start()
        self.addCleanup(self.status_patch.stop)
        self.java_patch = patch('backend_services.shutil.which', return_value='/fixture/java')
        self.java_patch.start()
        self.addCleanup(self.java_patch.stop)

    def test_detects_unique_spring_entry_and_keeps_frontend_defaults_independent(self):
        options = service_options(self.path)
        self.assertEqual(options['kind'], 'backend')
        self.assertEqual(options['main_class'], 'example.TestApplication')
        self.assertEqual(options['defaults']['port'], 8185)
        self.assertEqual(validate_service_config(self.path, {}), DEFAULTS)
        other = self.root / 'src/main/java/example/OtherApplication.java'
        other.write_text('package example;\n@SpringBootApplication\nclass OtherApplication { public static void main(String[] args) {} }')
        self.assertIsNone(backend_options(self.path))

    def test_config_and_launch_are_shell_free_with_explicit_local_profile(self):
        config = self.root / 'test overrides.properties'
        config.write_text('fixture=true')
        settings = validate_backend_config(self.path, dict(config_file=str(config)))
        with patch.dict(os.environ, {'SPRING_PROFILES_ACTIVE': 'prod', 'SERVER_PORT': '9000',
                                     'JAVA_TOOL_OPTIONS': '-Dspring.profiles.active=prod'}):
            command, env = launch_spec(self.path, settings)
        self.assertEqual(command, ['/bin/sh', self.path + '/gradlew', '--no-daemon', 'bootRun'])
        self.assertEqual(env['SPRING_PROFILES_ACTIVE'], 'test')
        self.assertEqual(env['SERVER_PORT'], '8185')
        self.assertEqual(env['SERVER_ADDRESS'], '127.0.0.1')
        self.assertEqual(env['SPRING_CLOUD_NACOS_DISCOVERY_REGISTER_ENABLED'], 'false')
        self.assertEqual(env['SPRING_CONFIG_ADDITIONAL_LOCATION'], config.as_uri())
        self.assertNotIn('JAVA_TOOL_OPTIONS', env)
        for invalid in (dict(script='bootRun;rm'), dict(mode='test --evil'), dict(port_strategy='auto'),
                        dict(port=True), dict(port=80), dict(config_file='relative.properties')):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_backend_config(self.path, invalid)

    def test_preview_is_read_only_and_checks_missing_jdk_or_config(self):
        with patch('backend_services.subprocess.Popen') as spawn:
            preview = self.manager.preview_start(self.data)
            self.assertEqual(preview['settings']['mode'], 'test')
            spawn.assert_not_called()
        self.manager.config = dict(java_home='/missing/jdk')
        with self.assertRaisesRegex(ValueError, 'Java Home'):
            self.manager.preview_start(self.data)
        self.manager.config = dict(config_file='/missing/settings.properties')
        with self.assertRaisesRegex(ValueError, '存在'):
            self.manager.preview_start(self.data)

    def test_existing_backend_in_another_worktree_blocks_start_and_update(self):
        for state in ('running', 'starting', 'stopping', 'unknown'):
            self.status['items']['/another/tree'] = dict(status=state, ports=[])
            with self.subTest(state=state), self.assertRaisesRegex(ValueError, '已有共用后端'):
                self.manager.preview_start(self.data)
            with self.assertRaises(ValueError):
                self.manager.guard_update(self.path)

    def test_stale_preview_or_missing_confirmation_never_launches(self):
        preview = self.manager.preview_start(self.data)
        with patch('backend_services.subprocess.Popen') as spawn:
            with self.assertRaises(ValueError):
                self.manager.start(dict(self.data, plan=preview['plan']))
            preview = self.manager.preview_start(self.data)
            self.snapshot['head'] = 'b' * 40
            with self.assertRaisesRegex(ValueError, '已变化'):
                self.manager.start(dict(self.data, plan=preview['plan'], confirmed=True))
            spawn.assert_not_called()

    def test_confirmed_start_creates_one_persistent_record_and_no_shell(self):
        preview = self.manager.preview_start(self.data)
        process = Mock(pid=4321)
        process.poll.return_value = None
        with patch('backend_services.socket.socket'), patch('backend_services.directory'), \
                patch('backend_services.checked', return_value=123), \
                patch('backend_services.os.fdopen', return_value=io.BytesIO()), \
                patch('backend_services.subprocess.Popen', return_value=process) as spawn, \
                patch('backend_services.process_identities', return_value=[dict(identity='owned wrapper')]), \
                patch.object(self.manager, 'save_records') as save:
            self.manager.start(dict(self.data, plan=preview['plan'], confirmed=True))
            spawn.assert_called_once()
            self.assertTrue(spawn.call_args.kwargs['start_new_session'])
            self.assertNotIn('shell', spawn.call_args.kwargs)
            self.assertEqual(self.manager.records[self.path]['kind'], 'backend')
            self.assertEqual(self.manager.records[self.path]['profile'], 'test')
            save.assert_called_once()
            with self.assertRaisesRegex(ValueError, '预览'):
                self.manager.start(dict(self.data, plan=preview['plan'], confirmed=True))
            spawn.assert_called_once()

    def test_port_conflict_never_reassigns_backend_port(self):
        preview = self.manager.preview_start(self.data)
        with patch('backend_services.socket.socket') as socket, patch('backend_services.subprocess.Popen') as spawn:
            socket.return_value.__enter__.return_value.bind.side_effect = OSError('occupied')
            with self.assertRaisesRegex(ValueError, '固定端口已占用'):
                self.manager.start(dict(self.data, plan=preview['plan'], confirmed=True))
            spawn.assert_not_called()

    def test_recovered_launcher_identity_must_match_and_build_cannot_be_stopped_as_app(self):
        self.manager.records[self.path] = dict(kind='backend', pid=4321, identity=hashlib.sha256(b'owned').hexdigest(), identity_format='creation-time')
        with patch('backend_services.process_inventory', return_value=[]), \
                patch('backend_services.process_identities', return_value=[dict(identity='reused PID')]):
            self.assertEqual(self.manager.inventory(), [])
        process = dict(pid=4321, cwd=self.path, ports=[], identity='owned')
        with patch.object(self.manager, 'inventory', return_value=[process]), \
                patch('worktree_services.process_identities', return_value=[process]):
            with self.assertRaisesRegex(ValueError, '仍在构建'):
                self.manager.stop_context(self.path, self.tree['branch'])

    def test_config_content_changes_invalidate_start_preview(self):
        config = self.root / 'local.properties'
        config.write_text('fixture=test')
        self.manager.config = dict(config_file=str(config))
        preview = self.manager.preview_start(self.data)
        config.write_text('fixture=changed')
        with patch('backend_services.subprocess.Popen') as spawn, self.assertRaisesRegex(ValueError, '已变化'):
            self.manager.start(dict(self.data, plan=preview['plan'], confirmed=True))
        spawn.assert_not_called()

    def test_reused_pid_cannot_claim_previous_launch_configuration(self):
        self.manager.records[self.path] = dict(kind='backend', pid=4321, cwd=self.path,
                                               identity=hashlib.sha256(b'old').hexdigest())
        rows = self.manager.evidence(self.path, [dict(pid=4321, cwd=self.path, ports=[8185])], {4321: 'new'}, {})
        self.assertFalse(rows[0]['managed_launcher'])

    def test_backend_update_api_checks_running_service_before_git_and_blocks_master(self):
        import json
        from server import Handler
        self.repo.batch = SimpleNamespace(guard=Mock())
        self.repo.update_branch = Mock()
        for branch, active in (('feat/backend', True), ('master', False), ('feat/backend', False)):
            self.status['items'][self.path]['status'] = 'running' if active else 'stopped'
            handler = Handler.__new__(Handler)
            body = json.dumps(dict(self.data, branch=branch, head='a' * 40, confirmed=True)).encode()
            handler.path = '/api/branch/update'
            handler.headers = {'Host': '127.0.0.1:8123', 'X-Session-Token': 'fixture',
                               'Content-Type': 'application/json', 'Content-Length': str(len(body))}
            handler.rfile = io.BytesIO(body)
            handler.server = SimpleNamespace(server_port=8123, token='fixture', repo=self.repo,
                                             services=self.manager, development=SimpleNamespace(status=lambda: {}))
            handler.reply = Mock()
            self.repo.update_branch.reset_mock()
            handler.do_POST()
            if active or branch == 'master':
                self.repo.update_branch.assert_not_called()
                self.assertEqual(handler.reply.call_args.args[0], 400)
            else:
                self.repo.update_branch.assert_called_once_with(self.path, branch, 'a' * 40, True)
                self.assertEqual(handler.reply.call_args.args[0], 200)

    def test_stopping_uses_verified_group_only_and_never_all_java(self):
        with patch('backend_services.os.kill') as kill, patch('backend_services.os.killpg') as killpg:
            self.manager.signal_process(dict(pid=4321, group=4321))
            killpg.assert_called_once_with(4321, 15)
            kill.assert_not_called()
            self.manager.signal_process(dict(pid=5678))
            kill.assert_called_once_with(5678, 15)

    def test_status_reports_wrong_port_as_starting_not_ready(self):
        self.status_patch.stop()
        record = dict(kind='backend', pid=4321, profile='test', port=8185, log_path='/log')
        self.manager.records[self.path] = record
        with patch('worktree_services.WorktreeServices.status', return_value=dict(items={self.path: dict(
                status='running', pids=[4321, 5678], ports=[5005], processes=[dict(managed_launcher=True)])}, error='')):
            result = self.manager.status()['items'][self.path]
        self.assertEqual(result['status'], 'starting')
        self.assertEqual(result['profile'], 'test')

    def test_listening_child_jvm_is_running_even_when_wrapper_identity_changed(self):
        self.status_patch.stop()
        child = dict(pid=200, cwd=self.path, ports=[8185])
        self.manager.jobs[self.path] = dict(process=Mock(pid=100, poll=Mock(return_value=None)),
            backend='http://127.0.0.1:8185', mode='project', log_path='/fixture/backend.log', port=8185, started=0)
        with patch.object(self.manager, 'inventory', return_value=[child]), \
             patch('worktree_services.process_identities', return_value=[dict(child, identity='birth command')]), \
             patch('worktree_services.service_options', return_value={'backend_override': False}):
            result = self.manager.status()['items'][self.path]
        self.assertEqual(result['status'], 'running')
        self.assertEqual(result['ports'], [8185])
        self.assertNotIn('error', result)

    def test_windows_java_home_uses_exe_and_bash_receives_literal_wrapper(self):
        home = self.root / 'JDK & tools'
        (home / 'bin').mkdir(parents=True)
        (home / 'bin/java.exe').write_text('fixture')
        (home / 'bin/java.exe').chmod(0o755)
        settings = validate_backend_config(self.path, {'java_home': str(home)})
        with patch('backend_services.platform.IS_WINDOWS', True), \
             patch('backend_services.platform.bash_binary', return_value='/fixture/Git/bin/bash.exe'):
            command, env = launch_spec(self.path, settings)
        self.assertEqual(command, ['/fixture/Git/bin/bash.exe', (self.root / 'gradlew').as_posix(), '--no-daemon', 'bootRun'])
        self.assertEqual(env['JAVA_HOME'], home.as_posix())

    def test_launcher_record_survives_exec_command_change_but_not_pid_reuse(self):
        from backend_services import launcher_identity
        with patch('backend_services.platform.IS_WINDOWS', False):
            before = launcher_identity('Wed Nov 4 10:05:00 2026 /bin/sh gradlew')
            after = launcher_identity('Wed Nov 4 10:05:00 2026 /path/java GradleWrapperMain')
            reused = launcher_identity('Wed Nov 4 10:06:00 2026 /path/java GradleWrapperMain')
        self.assertEqual(before, after)
        self.assertNotEqual(before, reused)


    def test_unverifiable_legacy_launcher_blocks_duplicate_start(self):
        self.manager.records[self.path] = dict(kind='backend', pid=123, cwd=self.path, identity='0' * 64)
        with patch('backend_services.process_inventory', return_value=[]), \
             patch('backend_services.process_identities', return_value=[dict(identity='Wed Nov 4 10:00:00 2026 java')]):
            with self.assertRaisesRegex(ValueError, '旧版后端启动记录'):
                self.manager.inventory()
        self.manager.records[self.path]['identity_format'] = 'creation-time'
        with patch('backend_services.process_inventory', return_value=[]), \
             patch('backend_services.process_identities', return_value=[dict(identity='Wed Nov 4 10:00:00 2026 java')]):
            self.assertEqual(self.manager.inventory(), [])


if __name__ == '__main__':
    unittest.main()
