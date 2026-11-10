"""Windows safety boundaries exercised without starting services or native UI."""
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch
import platform_support as platform
from server import Handler
from windows.bridge import main as bridge_main


class WindowsPlatformChecks(unittest.TestCase):
    def test_acl_rejects_public_grants_other_owner_and_reparse_points(self):
        sid = 'S-1-5-21-100-200-300-1001'
        cases = [
            (f'O:{sid}D:PAI(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)', True),
            (f'O:{sid}D:PAI(A;OICI;FA;;;{sid})(A;OICI;FA;;;OW)', True),
            (f'O:{sid}D:(A;;FA;;;{sid})(A;;FA;;;WD)', False),
            (f'O:BAD:(A;;FA;;;{sid})', False),
            (f'O:{sid}', False),
        ]
        with patch.object(platform, 'current_sid', return_value=sid), \
             patch.object(Path, 'lstat', return_value=SimpleNamespace(st_file_attributes=0)):
            for descriptor, expected in cases:
                with self.subTest(descriptor=descriptor), patch.object(platform, '_security_descriptor', return_value=descriptor):
                    self.assertEqual(platform.private_windows_path('fixture'), expected)
        with patch.object(Path, 'lstat', return_value=SimpleNamespace(st_file_attributes=1024)), \
             patch.object(platform, '_security_descriptor') as descriptor:
            self.assertFalse(platform.private_windows_path('fixture'))
            descriptor.assert_not_called()

    def test_cmd_shims_resolve_to_node_without_shell_expansion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'node tools & space'
            script = root / 'node_modules/@openai/codex/bin/codex.js'
            script.parent.mkdir(parents=True)
            script.write_text('// fixture')
            def which(name):
                return str(root / ('node.exe' if name == 'node' else 'codex.cmd'))
            with patch.object(platform, 'IS_WINDOWS', True), patch.object(platform.shutil, 'which', side_effect=which):
                command = platform.windows_command(['codex', 'exec', 'literal & $() 中文'])
            self.assertEqual(command, [str(root / 'node.exe'), str(script), 'exec', 'literal & $() 中文'])
            with patch.object(platform, 'IS_WINDOWS', True), patch.object(platform.shutil, 'which', return_value=str(root / 'unknown.cmd')):
                with self.assertRaisesRegex(ValueError, '受支持'):
                    platform.windows_command(['unknown', 'literal'])

    def test_process_identity_detects_worktree_change(self):
        process = Mock()
        process.cwd.return_value = str(Path('/changed').resolve())
        fake = SimpleNamespace(Process=Mock(return_value=process), NoSuchProcess=ProcessLookupError, AccessDenied=PermissionError)
        with patch.object(platform, 'windows_psutil', return_value=fake), self.assertRaisesRegex(ValueError, '工作区已变化'):
            platform.process_identities([dict(pid=10, cwd=str(Path('/expected').resolve()))])

    def test_frontend_inventory_keeps_ports_and_actual_worktree(self):
        process = Mock(pid=321)
        process.name.return_value = 'node.exe'
        process.cmdline.return_value = ['node.exe', r'C:\repo\node_modules\vite\bin\vite.js', '--mode', 'dev']
        process.cwd.return_value = str(Path('/project').resolve())
        process.net_connections.return_value = [SimpleNamespace(status='LISTEN', laddr=SimpleNamespace(ip='0.0.0.0', port=8081))]
        fake = SimpleNamespace(CONN_LISTEN='LISTEN', NoSuchProcess=ProcessLookupError, AccessDenied=PermissionError)
        with patch.object(platform, 'windows_psutil', return_value=fake), patch.object(platform, 'owned_processes', return_value=[process]):
            self.assertEqual(platform.service_inventory(), [dict(pid=321, cwd=str(Path('/project').resolve()), ports=[8081], bindings=[dict(host='0.0.0.0', port=8081)], mode='dev')])

    def test_shutdown_requires_confirmation_and_refuses_active_tasks(self):
        handler = Handler.__new__(Handler)
        handler.path = '/api/runtime/shutdown'
        handler.headers_ok = Mock()
        handler.reply = Mock()
        server = SimpleNamespace(token='test-token', shutdown=Mock(),
            repo=SimpleNamespace(batch=SimpleNamespace(guard=Mock()), resolver=SimpleNamespace(guard=Mock())),
            development=SimpleNamespace(status=Mock(return_value={'task': {'status': 'running'}})))
        handler.server = server
        for data in ({}, {'confirmed': True}):
            body = json.dumps(data).encode()
            handler.headers = {'X-Session-Token': 'test-token', 'Content-Type': 'application/json', 'Content-Length': str(len(body))}
            handler.rfile = io.BytesIO(body)
            with patch('server.threading.Thread') as thread:
                handler.do_POST()
            self.assertEqual(handler.reply.call_args.args[0], 400)
            thread.assert_not_called()
        server.development.status.return_value = {}
        body = b'{"confirmed":true}'
        handler.headers['Content-Length'] = str(len(body)); handler.rfile = io.BytesIO(body)
        with patch('server.threading.Thread') as thread:
            handler.do_POST()
        self.assertEqual(handler.reply.call_args.args, (200, {'status': 'stopping'}))
        thread.assert_called_once_with(target=server.shutdown, daemon=True)
        thread.return_value.start.assert_called_once()

    def test_bridge_listing_cannot_start_a_workbench(self):
        with patch('sys.argv', ['bridge.py', 'list']), patch('windows.bridge._read', return_value={'default_id': '', 'projects': {}}), \
             patch('windows.bridge.launch') as launch, patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(bridge_main(), 0)
            self.assertEqual(json.loads(output.getvalue())['projects'], [])
            launch.assert_not_called()

    def test_shutdown_cli_requires_explicit_confirmation(self):
        with patch('sys.argv', ['bridge.py', 'shutdown']), patch('windows.bridge.shutdown_all') as shutdown, \
             patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(bridge_main(), 1)
            shutdown.assert_not_called()
