"""Project registration validation and persistence use temporary local Git repositories."""
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
import json
import sys
from unittest.mock import Mock, patch

from platform_config import ProjectRegistry, history_file, pinned_file, read_pinned, write_pinned
from server import Handler
from macos.desktop_bridge import connect as desktop_connect


class ProjectRegistryChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo with spaces'
        self.repo.mkdir()
        import subprocess
        subprocess.run(['/usr/bin/git', '-C', str(self.repo), 'init', '-q'], check=True)
        self.config = self.root / 'Application Support'
        self.config.mkdir(mode=0o700)
        self.registry = ProjectRegistry()
        self.patch = patch('platform_config.ROOT', self.config), patch('platform_config.FILE', self.config / 'projects.json')
        for item in self.patch:
            item.start()
            self.addCleanup(item.stop)

    def test_validates_git_roots_and_keeps_registry_outside_project(self):
        entry = self.registry.add(str(self.repo), ' Test project ')
        current = self.registry.list(str(self.repo))
        self.assertEqual(current['current_id'], entry['id'])
        self.assertEqual(current['projects'][0]['name'], 'Test project')
        self.assertFalse((self.repo / 'projects.json').exists())
        self.assertEqual((self.config / 'projects.json').stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(ValueError, 'Git 仓库'):
            self.registry.add(str(self.root))

    def test_settings_and_default_are_isolated_by_common_git_directory(self):
        first = self.registry.add(str(self.repo))
        second = self.root / 'second'
        second.mkdir()
        import subprocess
        subprocess.run(['/usr/bin/git', '-C', str(second), 'init', '-q'], check=True)
        other = self.registry.add(str(second))
        self.registry.update(first['id'], {'name': 'First', 'default': True, 'service': {'mode': 'test'}})
        self.assertEqual(self.registry.get(first['id'])['service'], {'mode': 'test'})
        with self.assertRaisesRegex(ValueError, '先切换'):
            self.registry.remove(first['id'], str(self.repo))
        self.registry.remove(other['id'], str(self.repo))
        self.assertEqual(self.registry.list(str(second))['default_id'], first['id'])
        other = self.registry.add(str(second))
        self.registry.update(other['id'], {'default': True})
        self.registry.remove(other['id'], str(self.repo))
        self.assertEqual(self.registry.list(str(self.repo))['default_id'], first['id'])

    def test_pinned_branches_persist_per_git_directory_without_project_registry(self):
        common = str((self.repo / '.git').resolve())
        other = str(self.root / 'other' / '.git')
        self.assertIsNone(read_pinned(common))
        self.assertEqual(write_pinned(common, ['refs/heads/pre', 'refs/heads/中文分支']),
                         ['refs/heads/pre', 'refs/heads/中文分支'])
        self.assertEqual(read_pinned(common), ['refs/heads/pre', 'refs/heads/中文分支'])
        self.assertIsNone(read_pinned(other))
        self.assertEqual(pinned_file(common).stat().st_mode & 0o777, 0o600)
        self.assertEqual(write_pinned(common, []), [])
        self.assertEqual(read_pinned(common), [])

    def test_invalid_pinned_refs_and_storage_errors_preserve_previous_choice(self):
        common = str((self.repo / '.git').resolve())
        write_pinned(common, ['refs/heads/pre'])
        for invalid in (None, ['refs/heads/pre'] * 101, ['refs/heads/pre'] * 2,
                        ['refs/heads/a..b'], ['refs/tags/v1'], [123]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                write_pinned(common, invalid)
            self.assertEqual(read_pinned(common), ['refs/heads/pre'])
        pinned_file(common).chmod(0o644)
        with self.assertRaisesRegex(ValueError, '权限异常'):
            write_pinned(common, ['refs/heads/test'])
        self.assertEqual(json.loads(pinned_file(common).read_text())['pinned'], ['refs/heads/pre'])

    def test_project_api_connects_only_registered_git_identity_and_remove_keeps_code(self):
        first = self.registry.add(str(self.repo))
        second = self.root / 'second'
        second.mkdir()
        import subprocess
        subprocess.run(['/usr/bin/git', '-C', str(second), 'init', '-q'], check=True)
        other = self.registry.add(str(second))
        repo = SimpleNamespace(root=str(self.repo), common=str(self.repo / '.git'), batch=SimpleNamespace(guard=Mock()))
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(projects=self.registry)
        handler.reply = Mock(side_effect=lambda code, data: (code, data))
        handler.path = '/api/projects/connect'
        with patch('launch_codex.launch', return_value={'status': 'not_running'}) as launch:
            self.assertEqual(handler.project_action(repo, {'id': other['id'], 'start': False})[1]['status'], 'not_running')
            launch.assert_called_once_with(str(second.resolve()), str((second / '.git').resolve()), False)
            with self.assertRaisesRegex(Exception, '未登记'):
                handler.project_action(repo, {'id': 'a' * 64, 'start': True})
            launch.assert_called_once()
        handler.path = '/api/projects/remove'
        self.assertEqual(handler.project_action(repo, {'id': other['id']})[0], 200)
        self.assertTrue((second / '.git').is_dir())
        self.assertIsNone(self.registry.get(other['id']))

    def test_history_symlink_does_not_redirect_writes(self):
        external = self.root / 'external.sqlite3'
        external.write_text('preserve')
        (self.config / 'operations.sqlite3').symlink_to(external)
        with self.assertRaisesRegex(ValueError, '历史文件权限异常'):
            history_file(self.root)
        self.assertEqual(external.read_text(), 'preserve')

    def test_desktop_resolves_project_id_before_opening_local_runtime(self):
        entry = self.registry.add(str(self.repo))
        config = dict(application=str(Path(__file__).resolve().parent), default_repo=str(self.repo),
                      python=sys.executable, path='/usr/bin:/bin')
        result = SimpleNamespace(stdout=json.dumps({'status': 'not_running'}), returncode=0)
        with patch('platform_config.repository', return_value=(entry['path'], str(Path(entry['path']) / '.git'))), \
             patch('macos.desktop_bridge.subprocess.run', return_value=result) as launched:
            self.assertEqual(desktop_connect(config, project_id=entry['id'])['status'], 'not_running')
            self.assertEqual(launched.call_args.args[0][-2:], ['--repo', entry['path']])
            with self.assertRaisesRegex(ValueError, '项目标识无效'):
                desktop_connect(config, project_id='../other')
            launched.assert_called_once()


if __name__ == '__main__':
    unittest.main()
