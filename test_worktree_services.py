"""Service discovery and launch guards; never start a process or bind a port."""
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from worktree_services import (WorktreeServices, backend_origin, free_port, service_options,
                               validate_service_config, probe_backend, lan_ipv4_addresses, lan_service_urls,
                               group_processes, launch_config, lsof_records,
                               process_inventory, process_identities as original_process_identities)

real_fdopen = os.fdopen


def command_result(stdout='', returncode=0, stderr=''):
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


class DiscoveryTests(unittest.TestCase):
    def test_lan_interfaces_exclude_loopback_inactive_and_virtual_benchmark_addresses(self):
        output = ('lo0: flags=1<UP,LOOPBACK>\n\tinet 127.0.0.1 netmask x\n'
                  'en0: flags=1<UP,BROADCAST>\n\tinet 192.168.1.20 netmask x\n'
                  'en1: flags=1<UP,BROADCAST>\n\tinet 10.2.3.4 netmask x\n'
                  'en2: flags=0<BROADCAST>\n\tinet 172.16.0.2 netmask x\n'
                  'utun0: flags=1<UP>\n\tinet 198.18.0.1 netmask x\n')
        with patch('worktree_services.platform.IS_WINDOWS', False), \
                patch('worktree_services.subprocess.run', return_value=command_result(output)):
            self.assertEqual(lan_ipv4_addresses(), ['10.2.3.4', '192.168.1.20'])
        with patch('worktree_services.platform.IS_WINDOWS', False), \
                patch('worktree_services.subprocess.run', side_effect=OSError('unavailable')):
            self.assertEqual(lan_ipv4_addresses(), [])

    def test_lan_urls_require_actual_ipv4_listener_and_keep_ports_separate(self):
        processes = [dict(bindings=[dict(host='127.0.0.1', port=8080), dict(host='[::1]', port=8080),
                                   dict(host='0.0.0.0', port=8081), dict(host='*', port=8081),
                                   dict(host='192.168.1.20', port=8082)]), dict(ports=[8083])]
        self.assertEqual(lan_service_urls(processes, ['192.168.1.20', '10.2.3.4']), {
            8081: ['http://10.2.3.4:8081/', 'http://192.168.1.20:8081/'],
            8082: ['http://192.168.1.20:8082/'],
        })
        self.assertEqual(lan_service_urls(processes, []), {})

    def test_windows_lan_interfaces_use_existing_process_dependency_and_ignore_down_interfaces(self):
        import socket
        fake = SimpleNamespace(
            net_if_stats=lambda: {'wifi': SimpleNamespace(isup=True), 'down': SimpleNamespace(isup=False)},
            net_if_addrs=lambda: {'wifi': [SimpleNamespace(family=socket.AF_INET, address='192.168.2.5')],
                                 'down': [SimpleNamespace(family=socket.AF_INET, address='10.0.0.2')]})
        with patch('worktree_services.platform.IS_WINDOWS', True), \
                patch('worktree_services.platform.windows_psutil', return_value=fake):
            self.assertEqual(lan_ipv4_addresses(), ['192.168.2.5'])

    def inventory(self, cwd, listeners, commands):
        with patch('worktree_services.shutil.which', return_value='/usr/sbin/lsof'), \
                patch('worktree_services.subprocess.run', side_effect=[cwd, listeners, commands]) as run:
            result = process_inventory()
            self.assertTrue(all(call.kwargs.get('timeout') for call in run.call_args_list))
            return result

    def test_nul_records_preserve_spaces_chinese_and_newlines(self):
        output = 'p41\0n/tmp/中文 项目\n第二行\0\np42\0cnode\0n*:8080\0n[::1]:8081\0\n'
        self.assertEqual(lsof_records(output), [
            {'pid': 41, 'names': ['/tmp/中文 项目\n第二行']},
            {'pid': 42, 'command': 'node', 'names': ['*:8080', '[::1]:8081']},
        ])

    def test_only_node_vite_processes_are_services_and_ports_are_deduplicated(self):
        cwd = command_result(''.join(f'p{pid}\0n/tmp/project\0\n' for pid in range(11, 18)))
        listeners = command_result(
            'p11\0cnode\0n*:8080\0n[::1]:8080\0n127.0.0.1:8081\0\n'
            'p12\0cnode\0n*:3300\0\np13\0cElectron\0n*:9222\0\n')
        commands = command_result(
            '11 /opt/node /tmp/project/node_modules/vite/bin/vite.js --mode test\n'
            '12 node /tmp/project/server.js\n'
            '13 Electron /tmp/vite.js\n'
            '14 node /tmp/project/node_modules/.bin/vite --mode test\n'
            '15 node /tmp/project/vite-helper.js\n'
            '16 /bin/sh -c node /tmp/project/vite.js\n'
            '17 node /tmp/project/server.js --name vite\n')
        result = self.inventory(cwd, listeners, commands)
        self.assertEqual([(row['pid'], row['ports']) for row in result], [(11, [8080, 8081]), (14, [])])
        self.assertEqual(result[0]['bindings'], [dict(host='*', port=8080), dict(host='[::1]', port=8080),
                                               dict(host='127.0.0.1', port=8081)])

    def test_nested_worktrees_own_their_processes_without_prefix_collision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / 'repo'
            child = root / '.aidp/worktrees/中文 需求'
            trees = [{'path': str(root)}, {'path': str(child)}]
            processes = [
                dict(pid=1, cwd=str(root), ports=[8080]),
                dict(pid=2, cwd=str(child / 'src'), ports=[8081]),
                dict(pid=3, cwd=str(root.parent / 'repo-sibling'), ports=[8082]),
            ]
            grouped = group_processes(trees, processes)
            self.assertEqual([row['pid'] for row in grouped[str(root)]], [1])
            self.assertEqual([row['pid'] for row in grouped[str(child)]], [2])

    def test_partial_process_reads_fail_closed(self):
        for results in (
            [command_result(returncode=1), command_result(), command_result()],
            [command_result('p1\0n/tmp\0'), command_result(stderr='permission denied'), command_result()],
            [command_result('p1\0n/tmp\0'), command_result(), command_result(returncode=1)],
        ):
            with self.subTest(results=results), self.assertRaises(ValueError):
                self.inventory(*results)
        with patch('worktree_services.shutil.which', return_value=None), self.assertRaises(ValueError):
            process_inventory()

    def test_visible_vite_without_readable_cwd_is_unknown(self):
        with self.assertRaises(ValueError):
            self.inventory(command_result('p1\0n/tmp/other\0\n'),
                           command_result('p20\0cnode\0n127.0.0.1:8080\0\n'),
                           command_result('1 /sbin/launchd\n20 node /repo/node_modules/vite/bin/vite.js --mode test\n'))


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'repo'
        self.root.mkdir()
        (self.root / 'package.json').write_text(json.dumps({'name': 'bim-web'}))
        (self.root / 'vite.config.ts').write_text('viteEnv VITE_PROXY_TARGET VITE_BASE_API')
        env = self.root / 'viteEnv'
        env.mkdir()
        (env / '.env').write_text("VITE_BASE_API = 'https://fallback.example.test'\n")
        (env / '.env.test').write_text("VITE_PROXY_TARGET = 'https://test.example.test'\n")
        self.vite = self.root / 'node_modules/vite/bin/vite.js'
        self.vite.parent.mkdir(parents=True)
        self.vite.write_text('// fixture; never executed')

    def test_project_test_environment_overrides_base_and_uses_accessible_dependencies(self):
        self.assertEqual(service_options(self.root)['defaults']['mode'], 'dev')
        with patch('worktree_services.shutil.which', return_value='/fixture/node'):
            self.assertEqual(launch_config(self.root), ('/fixture/node', str(self.vite), 'https://test.example.test'))
        (self.root / 'viteEnv/.env.test').write_text("VITE_BIM_API_BASE = ''\n")
        with patch('worktree_services.shutil.which', return_value='/fixture/node'):
            self.assertEqual(launch_config(self.root)[2], 'https://fallback.example.test')

    def test_missing_node_vite_or_test_config_blocks_launch(self):
        with patch('worktree_services.shutil.which', return_value=None), self.assertRaisesRegex(ValueError, 'Node'):
            launch_config(self.root)
        self.vite.unlink()
        with patch('worktree_services.shutil.which', return_value='/fixture/node'), self.assertRaisesRegex(ValueError, 'Vite'):
            launch_config(self.root)
        (self.root / 'viteEnv/.env.test').unlink()
        with self.assertRaisesRegex(ValueError, '测试环境配置'):
            launch_config(self.root)

    def test_backend_url_accepts_origins_and_rejects_credentials_paths_and_invalid_ports(self):
        for source, expected in (
            ('http://192.168.1.10:8185/', 'http://192.168.1.10:8185'),
            ('https://api.example.test', 'https://api.example.test'),
            ('http://[::1]:8080/', 'http://[::1]:8080'),
        ):
            with self.subTest(source=source):
                self.assertEqual(backend_origin(source), expected)
        for value in (None, 42, '', 'api.example.test', 'file:///tmp/data', 'https://user:pass@api.example.test',
                      'https://api.example.test/path', 'https://api.example.test/?key=secret',
                      'https://api.example.test/#fragment', 'https://api.example.test:0',
                      'https://api.example.test:65536', 'http://[::1', 'http://api.example.test\\x',
                      'http://api.example.test\n', 'http://bad host', 'https://' + 'x' * 2049):
            with self.subTest(value=value), self.assertRaises(ValueError):
                backend_origin(value)

    def test_port_probe_skips_reserved_and_occupied_ports_without_real_socket(self):
        with patch('worktree_services.socket.socket') as factory:
            probe = factory.return_value.__enter__.return_value
            probe.bind.side_effect = [OSError('in use'), None]
            self.assertEqual(free_port({8080}), 8082)
            self.assertEqual([call.args[0] for call in probe.bind.call_args_list],
                             [('0.0.0.0', 8081), ('0.0.0.0', 8082)])
            probe.bind.side_effect = OSError('in use')
            with self.assertRaisesRegex(ValueError, '空闲端口'):
                free_port(set())

    def test_service_options_accept_only_direct_vite_scripts_and_validate_settings(self):
        (self.root / 'package.json').write_text(json.dumps({
            'name': 'another-vue', 'dependencies': {'vue': '3'}, 'devDependencies': {'vite': '5'},
            'scripts': {'dev': 'vite --mode dev', 'bad': 'vite --open', 'shell': 'vite; echo unsafe'}}))
        (self.root / 'vite.config.ts').write_text('VITE_PROXY_TARGET')
        options = service_options(self.root)
        self.assertEqual(options['preset'], 'vite')
        self.assertEqual([item['name'] for item in options['scripts']], ['dev'])
        self.assertEqual(validate_service_config(self.root, dict(script='dev', mode='test',
            port_strategy='fixed', port=5000, test_backend='https://api.example.test/'))['test_backend'],
            'https://api.example.test')
        for value in (dict(script='shell'), dict(mode='test; echo unsafe'), dict(port_strategy='fixed', port=0),
                      dict(test_backend='https://api.example.test/path')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_service_config(self.root, value)

    def test_runtime_probe_reads_header_without_following_redirect(self):
        with patch('worktree_services.http.client.HTTPConnection') as factory:
            response = factory.return_value.getresponse.return_value
            response.status = 404
            response.getheader.return_value = 'https://test.example.test/BIMgntServ-abroad/'
            self.assertEqual(probe_backend(8081)['backend'], 'https://test.example.test')
            factory.return_value.request.assert_called_once_with('HEAD', '/BIMgntServ-abroad/')
            response.getheader.return_value = None
            self.assertIn('error', probe_backend(8081))


class ServiceLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name).resolve() / '中文 项目')
        self.tree = dict(path=self.path, branch='feature/example', missing=False, locked=False, prunable=False)
        self.snapshot = dict(branch=self.tree['branch'], operations=[], conflicts=[], status=' M app.vue', blocked='dirty')
        self.repo = SimpleNamespace(worktrees=Mock(return_value=[self.tree]),
                                    target=Mock(return_value=self.path), resolver=SimpleNamespace(guard=Mock()),
                                    snapshot=Mock(side_effect=lambda path: dict(self.snapshot)))
        self.manager = WorktreeServices(self.repo)
        self.data = dict(path=self.path, branch=self.tree['branch'])
        self.process = Mock(pid=901)
        self.process.poll.return_value = None
        patches = {
            'lan': patch('worktree_services.lan_ipv4_addresses', return_value=['192.168.1.20']),
            'probe': patch('worktree_services.probe_backend', return_value=dict(error='Fixture has no runtime backend header.')),
            'inventory': patch('worktree_services.process_inventory', return_value=[]),
            'configuration': patch('worktree_services.vite_binary', return_value=('/fixture/node', '/fixture/vite.js')),
            'details': patch('worktree_services.project_details', return_value=({}, '', True)),
            'port': patch('worktree_services.free_port', return_value=8083),
            'runtime': patch('worktree_services.RUNTIME', Path(self.temp.name) / 'runtime'),
            'directory': patch('worktree_services.directory'),
            'checked': patch('worktree_services.checked', return_value=123),
            'fdopen': patch('worktree_services.os.fdopen', side_effect=lambda *args: io.BytesIO()),
            'popen': patch('worktree_services.subprocess.Popen', return_value=self.process),
        }
        for name, patcher in patches.items():
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def test_start_runs_dev_mode_with_shared_local_backend_and_clean_api_base(self):
        for custom in ('', 'http://192.168.1.20:8185/'):
            with self.subTest(custom=custom), patch.dict(os.environ, {
                'VITE_BASE_API': 'https://production.example.test', 'VITE_PROXY_TARGET': 'https://wrong.example.test',
                'VITE_BIM_API_BASE': 'https://production.example.test', 'VITE_OTHER': 'discard',
                'NODE_ENV': 'production', 'GIT_DIR': '/unrelated', 'SERVICE_FIXTURE': 'keep',
            }):
                self.manager = WorktreeServices(self.repo)
                result = self.manager.start(dict(self.data, backend=custom))
                call = self.popen.call_args
                self.assertEqual(call.args[0], ['/fixture/node', '/fixture/vite.js', '--mode', 'dev', '--host',
                                                '0.0.0.0', '--port', '8083', '--strictPort'])
                self.assertEqual(call.kwargs['cwd'], self.path)
                self.assertTrue(call.kwargs['start_new_session'])
                self.assertNotIn('shell', call.kwargs)
                env = call.kwargs['env']
                self.assertEqual(env['VITE_BASE_API'], '')
                self.assertEqual(env['VITE_BIM_API_BASE'], '')
                self.assertEqual(env['VITE_LOCAL_SSO_PROXY_REWRITE'], 'true')
                self.assertEqual(env['VITE_PROXY_TARGET'], custom.rstrip('/') or 'http://127.0.0.1:8185')
                self.assertEqual(env['SERVICE_FIXTURE'], 'keep')
                for key in ('VITE_OTHER', 'NODE_ENV', 'GIT_DIR'):
                    self.assertNotIn(key, env)
                self.assertEqual(os.environ['VITE_BASE_API'], 'https://production.example.test')
                self.assertEqual(result['items'][self.path]['status'], 'starting')
                self.assertEqual(self.manager.jobs[self.path]['mode'], 'custom' if custom else 'test')

    def test_bibase_saved_dev_mode_defaults_to_local_without_reading_remote_env(self):
        self.manager = WorktreeServices(self.repo, dict(mode='dev'))
        with patch('worktree_services.service_options', return_value=dict(
                supported=True, preset='bibase', backend_override=True, scripts=[])), \
                patch('worktree_services.configured_backend', return_value='https://test.example.test') as backend, \
                patch('worktree_services.vite_binary', return_value=('/fixture/node', '/fixture/vite.js')):
            self.manager.start(self.data)
        backend.assert_not_called()
        self.assertEqual(self.popen.call_args.args[0][2:4], ['--mode', 'dev'])
        self.assertEqual(self.popen.call_args.kwargs['env']['VITE_PROXY_TARGET'], 'http://127.0.0.1:8185')

    def test_bibase_saved_other_mode_keeps_its_backend(self):
        self.manager = WorktreeServices(self.repo, dict(mode='staging'))
        with patch('worktree_services.service_options', return_value=dict(
                supported=True, preset='bibase', backend_override=True, scripts=[])), \
                patch('worktree_services.configured_backend', return_value='https://staging.example.test') as backend, \
                patch('worktree_services.vite_binary', return_value=('/fixture/node', '/fixture/vite.js')):
            self.manager.start(self.data)
        backend.assert_called_once_with(self.path, 'staging', required=False)
        self.assertEqual(self.popen.call_args.args[0][2:4], ['--mode', 'staging'])
        self.assertEqual(self.popen.call_args.kwargs['env']['VITE_PROXY_TARGET'], 'https://staging.example.test')

    def test_configured_generic_vite_launch_uses_validated_mode_and_fixed_port(self):
        self.manager = WorktreeServices(self.repo, dict(script='dev', mode='test', port_strategy='fixed', port=5111))
        options = dict(supported=True, preset='vite', backend_override=False,
                       scripts=[dict(name='dev', command='vite --mode dev')], defaults={})
        with patch('worktree_services.service_options', return_value=options), \
                patch('worktree_services.vite_binary', return_value=('/fixture/node', '/fixture/vite.js')), \
                patch('worktree_services.socket.socket') as socket_factory:
            result = self.manager.start(self.data)
        self.assertEqual(result['items'][self.path]['status'], 'starting')
        self.assertEqual(self.popen.call_args.args[0], ['/fixture/node', '/fixture/vite.js', '--mode', 'test', '--host',
                                                       '0.0.0.0', '--port', '5111', '--strictPort'])
        self.assertNotIn('VITE_PROXY_TARGET', self.popen.call_args.kwargs['env'])
        self.assertEqual(socket_factory.return_value.__enter__.return_value.bind.call_args.args[0], ('0.0.0.0', 5111))
        self.configuration.assert_not_called()

    def test_generic_project_rejects_backend_override_without_declared_proxy(self):
        self.manager = WorktreeServices(self.repo, dict(script='dev', mode='test'))
        options = dict(supported=True, preset='vite', backend_override=False,
                       scripts=[dict(name='dev', command='vite')], defaults={})
        with patch('worktree_services.service_options', return_value=options), \
                patch('worktree_services.vite_binary', return_value=('/fixture/node', '/fixture/vite.js')):
            with self.assertRaisesRegex(ValueError, 'VITE_PROXY_TARGET'):
                self.manager.start(dict(self.data, backend='https://api.example.test'))
        self.popen.assert_not_called()

    def test_unknown_inventory_does_not_start_and_returns_unknown_status(self):
        self.inventory.side_effect = ValueError('无法确认服务状态')
        self.assertEqual(self.manager.status()['items'][self.path]['status'], 'unknown')
        with self.assertRaisesRegex(ValueError, '无法确认'):
            self.manager.start(self.data)
        self.popen.assert_not_called()

    def test_status_publishes_lan_addresses_only_for_observed_non_loopback_ports(self):
        self.inventory.return_value = [dict(pid=901, cwd=self.path, ports=[8080, 8081], bindings=[
            dict(host='127.0.0.1', port=8080), dict(host='0.0.0.0', port=8081)])]
        with patch('worktree_services.process_identities', return_value=[dict(pid=901, identity='fixture')]):
            item = self.manager.status()['items'][self.path]
        self.assertEqual(item['status'], 'running')
        self.assertEqual(item['lan_urls'], {8081: ['http://192.168.1.20:8081/']})

    def test_duplicate_and_concurrent_clicks_start_only_one_process(self):
        barrier = threading.Barrier(3)
        errors = []
        def click():
            barrier.wait()
            try:
                self.manager.start(self.data)
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=click) for _ in range(2)]
        for worker in threads:
            worker.start()
        barrier.wait()
        for worker in threads:
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.manager.start(self.data)
        self.popen.assert_called_once()

    def test_restart_discovers_external_vite_and_never_restarts_it(self):
        self.inventory.return_value = [dict(pid=400, cwd=self.path, ports=[8088, 8087])]
        restarted = WorktreeServices(self.repo)
        with patch('worktree_services.process_identities', return_value=[dict(pid=400, identity='identity')]):
            status = restarted.start(self.data)['items'][self.path]
        self.assertEqual(status['status'], 'running')
        self.assertEqual(status['ports'], [8087, 8088])
        self.assertEqual(status['pids'], [400])
        self.assertNotIn('backend', status)
        self.popen.assert_not_called()

    def test_restart_restores_only_matching_process_identity_and_keeps_multiple_backends_separate(self):
        self.repo.common = self.path + '/.git'
        (Path(self.temp.name) / 'runtime').mkdir()
        self.directory.side_effect = None
        self.fdopen.side_effect = real_fdopen
        self.checked.side_effect = lambda path, flags: os.open(path, flags)
        with patch('worktree_services.RUNTIME', Path(self.temp.name) / 'runtime'), \
                patch('worktree_services.process_identities', return_value=[dict(pid=901, identity='original')]):
            manager = WorktreeServices(self.repo)
            manager.records[self.path] = dict(pid=901, cwd=self.path,
                identity=__import__('hashlib').sha256(b'original').hexdigest(), port=8081,
                backend='https://record.example.test', mode='test', log_path='/tmp/service.log')
            manager.save_records()
            self.inventory.return_value = [dict(pid=901, cwd=self.path, ports=[8081])]
            restored = WorktreeServices(self.repo)
            self.assertEqual(restored.status()['items'][self.path]['backend_source'], 'launch')
            self.assertEqual(restored.status()['items'][self.path]['backend'], 'https://record.example.test')
            with patch('worktree_services.process_identities', return_value=[dict(pid=901, identity='reused')]):
                state = restored.status(force=True)['items'][self.path]
                self.assertNotEqual(state.get('backend_source'), 'launch')
            self.inventory.return_value.append(dict(pid=902, cwd=self.path, ports=[8082]))
            with patch('worktree_services.process_identities', return_value=[dict(pid=901, identity='original'), dict(pid=902, identity='other')]), \
                    patch('worktree_services.configured_backend', side_effect=lambda path, mode: 'https://one.example.test' if mode == 'one' else 'https://two.example.test'):
                self.inventory.return_value[0]['mode'] = 'one'
                self.inventory.return_value[1]['mode'] = 'two'
                mixed = restored.status(force=True)['items'][self.path]
                self.assertNotIn('backend', mixed)

    def test_exit_and_launch_failure_are_visible_and_owned_starting_job_is_not_duplicated(self):
        self.manager.start(self.data)
        self.process.poll.return_value = 1
        status = self.manager.status()['items'][self.path]
        self.assertEqual(status['status'], 'failed')
        self.assertIn('退出码 1', status['error'])
        self.assertTrue(status['log_path'].endswith('.service.log'))
        self.popen.side_effect = OSError('cannot execute')
        with self.assertRaisesRegex(ValueError, '未能启动'):
            self.manager.start(self.data)
        self.popen.side_effect = None
        self.process.poll.return_value = None
        self.manager.jobs[self.path]['started'] -= 91
        self.assertIn('不会重复启动', self.manager.start(self.data)['items'][self.path]['error'])
        self.assertEqual(self.popen.call_count, 2)

    def test_invalid_url_changed_branch_conflicts_and_missing_dependencies_never_spawn(self):
        with self.assertRaises(ValueError):
            self.manager.start(dict(self.data, backend='https://api.example.test/path'))
        with self.assertRaises(ValueError):
            self.manager.start(dict(self.data, branch='different'))
        self.snapshot['operations'] = ['MERGE_HEAD']
        with self.assertRaises(ValueError):
            self.manager.start(self.data)
        self.snapshot['operations'] = []
        self.snapshot['conflicts'] = ['conflict.vue']
        with self.assertRaises(ValueError):
            self.manager.start(self.data)
        self.snapshot['conflicts'] = []
        self.configuration.side_effect = ValueError('缺少 Vite 依赖')
        with self.assertRaisesRegex(ValueError, '依赖'):
            self.manager.start(self.data)
        self.configuration.side_effect = None
        self.port.side_effect = ValueError('没有空闲端口')
        with self.assertRaisesRegex(ValueError, '空闲端口'):
            self.manager.start(self.data)
        self.popen.assert_not_called()


class ServiceStopTests(unittest.TestCase):
    def setUp(self):
        ServiceLifecycleTests.setUp(self)
        self.inventory.return_value = [dict(pid=400, cwd=self.path, ports=[8081])]
        for name, patcher in {
            'identities': patch('worktree_services.process_identities', side_effect=lambda rows: [dict(row, identity='start-time command') for row in rows]),
            'signal': patch('worktree_services.os.kill'),
            'alive': patch('worktree_services.process_alive', return_value=True),
        }.items():
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def test_external_preview_cancel_and_confirm_recheck_then_signal_only_vite(self):
        preview = self.manager.preview_stop(self.data)
        self.assertEqual(preview['processes'], [dict(pid=400, ports=[8081])])
        self.signal.assert_not_called()
        with self.assertRaisesRegex(ValueError, '确认'):
            self.manager.stop(dict(plan=preview['plan']))
        self.signal.assert_not_called()
        result = self.manager.stop(dict(plan=preview['plan'], confirmed=True))
        import signal
        self.signal.assert_called_once_with(400, signal.SIGTERM)
        self.assertEqual(result['items'][self.path]['status'], 'stopping')
        with self.assertRaisesRegex(ValueError, '失效'):
            self.manager.stop(dict(plan=preview['plan'], confirmed=True))
        self.inventory.return_value = []
        self.assertEqual(self.manager.status()['items'][self.path]['status'], 'stopping', 'Wait for PID exit as well as listener disappearance')
        self.alive.return_value = False
        self.assertEqual(self.manager.status()['items'][self.path]['status'], 'stopped')
        self.assertEqual(self.manager.status()['items'][self.path]['status'], 'stopped')
        self.popen.assert_not_called()

    def test_changed_identity_port_branch_and_expired_preview_never_signal(self):
        for changed in ('identity', 'port', 'branch', 'expired'):
            with self.subTest(changed=changed):
                self.inventory.return_value = [dict(pid=400, cwd=self.path, ports=[8081])]
                self.tree['branch'] = self.data['branch']
                self.identities.side_effect = lambda rows: [dict(row, identity='start-time command') for row in rows]
                preview = self.manager.preview_stop(self.data)
                if changed == 'identity':
                    self.identities.side_effect = lambda rows: [dict(row, identity='reused PID') for row in rows]
                elif changed == 'port':
                    self.inventory.return_value[0]['ports'] = [8082]
                elif changed == 'branch':
                    self.tree['branch'] = 'other'
                else:
                    self.manager.stop_plans[preview['plan']]['created'] -= 601
                with self.assertRaises(ValueError):
                    self.manager.stop(dict(plan=preview['plan'], confirmed=True))
                self.signal.assert_not_called()

    def test_partial_signal_failure_keeps_stopping_first_process_and_does_not_retry(self):
        self.inventory.return_value.append(dict(pid=401, cwd=self.path, ports=[8082]))
        preview = self.manager.preview_stop(self.data)
        self.signal.side_effect = [None, PermissionError()]
        with self.assertRaisesRegex(ValueError, '部分服务'):
            self.manager.stop(dict(plan=preview['plan'], confirmed=True))
        self.assertEqual(self.manager.stopping[self.path]['pids'], [400])
        self.assertEqual(self.signal.call_count, 2)
        self.manager.status()
        self.assertEqual(self.signal.call_count, 2)

    def test_stopping_timeout_never_escalates_or_starts_another_process(self):
        preview = self.manager.preview_stop(self.data)
        self.manager.stop(dict(plan=preview['plan'], confirmed=True))
        self.manager.stopping[self.path]['started'] -= 16
        state = self.manager.start(self.data)['items'][self.path]
        self.assertEqual(state['status'], 'stopping')
        self.assertIn('不会强制结束', state['error'])
        self.signal.assert_called_once()
        self.popen.assert_not_called()

    def test_managed_intentional_stop_does_not_become_failed_after_exit(self):
        self.inventory.return_value = []
        self.manager.start(self.data)
        self.inventory.return_value = [dict(pid=901, cwd=self.path, ports=[8083])]
        preview = self.manager.preview_stop(self.data)
        self.manager.stop(dict(plan=preview['plan'], confirmed=True))
        self.process.poll.return_value = -15
        self.inventory.return_value = []
        self.alive.return_value = False
        self.assertEqual(self.manager.status()['items'][self.path]['status'], 'stopped')
        self.assertNotIn(self.path, self.manager.jobs)
        self.assertEqual(self.manager.status()['items'][self.path]['status'], 'stopped')

    def test_identity_read_uses_start_time_and_fails_closed(self):
        # Exercise the original helper, with ps mocked; never inspect or signal a real process.
        with patch('worktree_services.subprocess.run', return_value=command_result('400 Thu Oct 29 08:53:22 2026 node /repo/node_modules/vite/bin/vite.js\n')):
            # The module helper is patched by this fixture; use the function imported before patching.
            result = original_process_identities([dict(pid=400, cwd=self.path, ports=[8081])])
            self.assertIn('2026', result[0]['identity'])
        with patch('worktree_services.subprocess.run', return_value=command_result('', returncode=1)), self.assertRaises(ValueError):
            original_process_identities([dict(pid=400, cwd=self.path, ports=[8081])])


if __name__ == '__main__':
    unittest.main(verbosity=2)
