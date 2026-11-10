#!/usr/bin/env python3
"""Build a local macOS app bound to this installed workbench and repository."""
import argparse
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import tempfile

APP = Path(__file__).resolve().parent.parent
ICON_SOURCE = r'''
import AppKit
let image = NSImage(size: NSSize(width: 1024, height: 1024))
image.lockFocus()
NSColor(srgbRed: 18/255, green: 48/255, blue: 68/255, alpha: 1).setFill()
NSBezierPath(roundedRect: NSRect(x: 64, y: 64, width: 896, height: 896), xRadius: 220, yRadius: 220).fill()
NSColor(srgbRed: 139/255, green: 230/255, blue: 221/255, alpha: 1).setStroke()
let lines = NSBezierPath()
lines.lineWidth = 64
lines.lineCapStyle = .round
lines.move(to: NSPoint(x: 326, y: 662))
lines.line(to: NSPoint(x: 326, y: 342))
lines.move(to: NSPoint(x: 698, y: 662))
lines.line(to: NSPoint(x: 698, y: 630))
lines.curve(to: NSPoint(x: 512, y: 456), controlPoint1: NSPoint(x: 698, y: 512), controlPoint2: NSPoint(x: 620, y: 456))
lines.line(to: NSPoint(x: 326, y: 456))
lines.stroke()
for (x,y) in [(326,750),(698,750),(326,270)] {
    let circle = NSBezierPath(ovalIn: NSRect(x: x-88, y: y-88, width: 176, height: 176))
    circle.lineWidth = 64
    circle.stroke()
}
image.unlockFocus()
let bitmap = NSBitmapImageRep(data: image.tiffRepresentation!)!
try bitmap.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
'''


def runtime_path():
    # Finder does not inherit nvm or other shell-managed command directories.
    commands = [Path(executable).absolute().parent for name in ('codex', 'node', 'npx')
                if (executable := shutil.which(name))]
    defaults = [Path.home()/'.local/bin', Path('/opt/homebrew/bin'), Path('/opt/homebrew/sbin'),
                Path('/usr/local/bin'), Path('/usr/bin'), Path('/bin'), Path('/usr/sbin'), Path('/sbin')]
    return ':'.join(dict.fromkeys(str(path) for path in commands + defaults if path.is_dir()))


def build(repo, output, python):
    from sign_local import sign_bundle

    repo = repo.expanduser().resolve(strict=True)
    python = python.expanduser().absolute()
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError('请指定可执行的 Python 3。')
    subprocess.run(['/usr/bin/git', '-C', str(repo), 'rev-parse', '--show-toplevel'],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if output.exists() or output.is_symlink():
        raise ValueError('输出已存在，保留已有应用；请选择新的输出目录。')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='branch-desk-build-', dir=output.parent) as folder:
        stage = Path(folder)
        bundle = stage / 'Branch Desk.app'
        contents = bundle / 'Contents'
        resources = contents / 'Resources'
        binaries = contents / 'MacOS'
        resources.mkdir(parents=True)
        binaries.mkdir()
        config = dict(application=str(APP), default_repo=str(repo), python=str(python), path=runtime_path())
        if os.environ.get('CODEX_HOME'):
            config['codex_home'] = str(Path(os.environ['CODEX_HOME']).expanduser().absolute())
        (resources / 'workbench.json').write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
        shutil.copy2(APP / 'macos' / 'desktop_bridge.py', resources)
        info = dict(CFBundleName='Branch Desk', CFBundleDisplayName='Branch Desk',
                    CFBundleIdentifier='local.branchdesk.desktop', CFBundleExecutable='BranchDesk',
                    CFBundlePackageType='APPL', CFBundleShortVersionString='0.2.0', CFBundleVersion='20261029.1',
                    CFBundleIconFile='AppIcon', LSMinimumSystemVersion='13.0', NSHighResolutionCapable=True,
                    NSDocumentsFolderUsageDescription='用于读取你在“文稿”中的 Branch Desk 工作台及运行状态。',
                    NSDesktopFolderUsageDescription='用于读取你选择的桌面 Git 仓库与工作区。',
                    NSPrincipalClass='NSApplication', NSAppTransportSecurity=dict(NSAllowsLocalNetworking=True,
                    NSAllowsArbitraryLoadsInWebContent=True))
        with (contents / 'Info.plist').open('wb') as stream:
            plistlib.dump(info, stream)
        target = platform.machine() + '-apple-macosx13.0'
        subprocess.run(['/usr/bin/xcrun', 'swiftc', '-swift-version', '5', '-O', '-target', target,
                        '-framework', 'AppKit', '-framework', 'WebKit', str(APP / 'macos' / 'BranchDesk.swift'),
                        '-o', str(binaries / 'BranchDesk')], check=True)
        subprocess.run([str(binaries / 'BranchDesk'), '--self-test'], check=True)
        icon_source = stage / 'Icon.swift'
        icon_source.write_text(ICON_SOURCE)
        renderer = stage / 'render-icon'
        subprocess.run(['/usr/bin/xcrun', 'swiftc', str(icon_source), '-o', str(renderer)], check=True)
        iconset = stage / 'AppIcon.iconset'
        iconset.mkdir()
        original = stage / 'icon.png'
        subprocess.run([str(renderer), str(original)], check=True)
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                name = 'icon_' + str(size) + 'x' + str(size) + ('@2x' if scale == 2 else '') + '.png'
                subprocess.run(['/usr/bin/sips', '-z', str(size * scale), str(size * scale), str(original),
                                '--out', str(iconset / name)], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(['/usr/bin/iconutil', '-c', 'icns', str(iconset), '-o', str(resources / 'AppIcon.icns')], check=True)
        sign_bundle(bundle)
        shutil.move(str(bundle), str(output))
    return output


def main():
    parser = argparse.ArgumentParser(description='构建本机 Branch Desk 独立应用，不启动服务或打开页面。')
    parser.add_argument('--repo', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--python', type=Path, default=Path(shutil.which('python3') or '/usr/bin/python3'))
    args = parser.parse_args()
    print(build(args.repo, args.output.expanduser().absolute(), args.python))


if __name__ == '__main__':
    main()
