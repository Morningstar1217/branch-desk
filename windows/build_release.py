#!/usr/bin/env python3
"""Reproducible payload staging from an explicit source allowlist; never starts services."""
import argparse
import hashlib
import json
import re
from pathlib import Path
import shutil
import subprocess
import zipfile

VERSION = '2026.11.04.3'
PYTHON_ARCHIVE = 'python-3.14.10-embed-amd64.zip'
PSUTIL_ARCHIVE = 'psutil-7.2.2-cp37-abi3-win_amd64.whl'
EXPECTED = {
    PYTHON_ARCHIVE: '90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159',
    PSUTIL_ARCHIVE: 'eb7e81434c8d223ec4a219b5fc1c47d0417b12be7ea866e24fb5ad6e84b3d988',
}
MODULES = ('server.py', 'platform_config.py', 'platform_support.py', 'workbench_runtime.py',
           'worktree_services.py', 'backend_services.py', 'launch_codex.py', 'operation_history.py',
           'conflict_resolver.py', 'development.py', 'feishu_project.py', 'batch_merge.py', 'test_platform_support.py')
WEB = ('index.html', 'app.js', 'style.css', 'schedule.js', 'batch.js', 'favicon.svg',
       'vendor/vue.global.prod.js', 'vendor/VUE-LICENSE')
WINDOWS = ('Program.cs', 'BranchDesk.csproj', 'packages.lock.json', 'app.manifest', 'bridge.py',
           'installer.nsi', 'build_release.py')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def unpack(archive, destination):
    with zipfile.ZipFile(archive) as bundle:
        for entry in bundle.infolist():
            target = (destination / entry.filename).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise ValueError('Unsafe archive entry: ' + entry.filename)
        bundle.extractall(destination)


def stage(args):
    source = Path(__file__).resolve().parents[1]
    destination = args.stage.resolve()
    if destination.exists():
        raise ValueError('Choose a new empty staging path; existing files are never erased.')
    for filename, expected in EXPECTED.items():
        if digest(args.downloads / filename) != expected:
            raise ValueError('Download hash mismatch: ' + filename)
    destination.mkdir(parents=True)
    copy(args.published / 'BranchDesk.exe', destination / 'BranchDesk.exe')
    for name in MODULES:
        copy(source / name, destination / 'app' / name)
    for name in WEB:
        copy(source / 'web' / name, destination / 'app/web' / name)
    for name in WINDOWS:
        copy(source / 'windows' / name, destination / 'app/windows' / name)
    # Remove legacy organization defaults from the distributed copy only.
    for name in ('feishu_project.py', 'operation_history.py'):
        path = destination / 'app' / name
        content = path.read_text(encoding='utf-8')
        content = re.sub(r"^(PROJECT|LEGACY_SPACE_ID) = .*", r"\1 = 'unconfigured'", content, flags=re.M)
        content = re.sub(r"^PROJECT_NAME = .*", "PROJECT_NAME = '未关联空间'", content, flags=re.M)
        content = re.sub(r"project_url='https://project[.]feishu[.]cn/[^']+'", "project_url='https://project.feishu.cn/unconfigured'", content)
        content = re.sub(r"space_alias='[^']+'", "space_alias='unconfigured'", content)
        path.write_text(content, encoding='utf-8')
    unpack(args.downloads / PYTHON_ARCHIVE, destination / 'python')
    unpack(args.downloads / PSUTIL_ARCHIVE, destination / 'python/Lib/site-packages')
    (destination / 'python/python314._pth').write_text('python314.zip\n.\nLib/site-packages\n../app\nimport site\n', encoding='utf-8')
    for name in ('Windows使用与Codex排障手册.md', 'Windows使用与Codex排障手册.html', '发布验证说明.md'):
        copy(args.docs / name, destination / 'docs' / name)
    copy(destination / 'docs/Windows使用与Codex排障手册.md', destination / 'app/README.md')
    licenses = destination / 'licenses'
    nuget = args.nuget
    for source_path, name in (
        (nuget / 'microsoft.netcore.app.runtime.win-x64/10.0.12/LICENSE.TXT', 'DOTNET-LICENSE.txt'),
        (nuget / 'microsoft.netcore.app.runtime.win-x64/10.0.12/THIRD-PARTY-NOTICES.TXT', 'DOTNET-THIRD-PARTY-NOTICES.txt'),
        (nuget / 'microsoft.windowsdesktop.app.runtime.win-x64/10.0.12/LICENSE', 'WINDOWS-DESKTOP-LICENSE.txt'),
        (nuget / 'microsoft.web.webview2/1.0.4314.35/LICENSE.txt', 'WEBVIEW2-LICENSE.txt'),
        (destination / 'python/LICENSE.txt', 'PYTHON-LICENSE.txt'),
        (destination / 'app/web/vendor/VUE-LICENSE', 'VUE-LICENSE.txt'),
    ):
        copy(source_path, licenses / name)
    for license_path in (destination / 'python/Lib/site-packages').rglob('*LICENSE*'):
        if license_path.is_file():
            copy(license_path, licenses / ('PSUTIL-' + license_path.name))
    candidates = [args.makensis.resolve().parent / 'COPYING',
                  args.makensis.resolve().parents[1] / 'share/nsis/COPYING']
    for candidate in candidates:
        if candidate.is_file():
            copy(candidate, licenses / 'NSIS-COPYING.txt')
            break
    notices = ('Branch Desk Windows ' + VERSION + '\n\n'
        'Python 3.14.10: https://www.python.org/ftp/python/3.14.10/\n'
        'psutil 7.2.2: https://pypi.org/project/psutil/7.2.2/\n'
        '.NET / Windows Desktop Runtime 10.0.12: https://dotnet.microsoft.com/\n'
        'Microsoft WebView2 SDK 1.0.4314.35: https://www.nuget.org/packages/Microsoft.Web.WebView2/\n'
        'Vue: version banner in app/web/vendor/vue.global.prod.js; MIT license in licenses/.\n'
        'Installer built with NSIS 3.13: https://nsis.sourceforge.io/\n'
        'See licenses/ and Python wheel metadata for full notices. WebView2 Runtime is installed separately.\n')
    (destination / 'THIRD-PARTY-NOTICES.txt').write_text(notices, encoding='utf-8')
    manifest = {str(path.relative_to(destination)).replace('\\', '/'): digest(path)
                for path in sorted(destination.rglob('*')) if path.is_file()}
    (destination / 'install-manifest.json').write_text(json.dumps({'version': VERSION, 'sha256': manifest}, ensure_ascii=False, indent=2), encoding='utf-8')
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--downloads', type=Path, required=True, help='Official Python ZIP and psutil wheel directory')
    parser.add_argument('--published', type=Path, required=True, help='dotnet publish output directory')
    parser.add_argument('--docs', type=Path, required=True, help='Directory containing the three release documents')
    parser.add_argument('--nuget', type=Path, default=Path.home() / '.nuget/packages')
    parser.add_argument('--stage', type=Path, required=True, help='New path for staged payload')
    parser.add_argument('--makensis', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    payload = stage(args)
    prefix = '/' if __import__('os').name == 'nt' else '-'
    subprocess.run([str(args.makensis.resolve()), prefix + 'INPUTCHARSET', 'UTF8', prefix + 'V3', prefix + 'DPAYLOAD=' + str(payload),
                    prefix + 'DOUTPUT=' + str(args.output.resolve()), str(Path(__file__).with_name('installer.nsi'))], check=True)
    print('SHA256', digest(args.output), args.output.name)


if __name__ == '__main__':
    main()
