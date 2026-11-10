"""Sign local builds with one persistent, dedicated identity; never change trust."""
import fcntl
import hashlib
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import tempfile

SIGNING = Path.home() / 'Library/Application Support/Branch Desk/Signing'
IDENTIFIER = 'local.branchdesk.desktop'
CERTIFICATE_NAME = 'Branch Desk Local Signing'
CERTIFICATE_CONFIG = '''[req]
prompt=no
distinguished_name=dn
x509_extensions=extensions
[dn]
CN=Branch Desk Local Signing
[extensions]
basicConstraints=critical,CA:false
keyUsage=critical,digitalSignature
extendedKeyUsage=critical,codeSigning
subjectKeyIdentifier=hash
'''


def _run(arguments, **kwargs):
    # security takes passwords as arguments; never include its argv in errors.
    try:
        result = subprocess.run(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                check=False, timeout=90, **kwargs)
    except (OSError, subprocess.SubprocessError):
        raise ValueError('本地专用签名命令无法完成；请保留 Signing 目录后重试。') from None
    if result.returncode:
        operation = Path(arguments[0]).name
        raise ValueError(f'本地专用签名失败（{operation}）；已保留签名身份，不会改用其他证书。')
    return result.stdout


def _write_private(path, content):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as stream:
        stream.write(content)


def _identity():
    keychain = SIGNING / 'branch-desk.keychain-db'
    password_file = SIGNING / 'keychain-password'
    certificate = SIGNING / 'certificate.pem'
    files = (keychain, password_file, certificate)
    if not any(path.exists() or path.is_symlink() for path in files):
        password = secrets.token_urlsafe(48)
        _write_private(password_file, password.encode())
        with tempfile.TemporaryDirectory(prefix='create-', dir=SIGNING) as folder:
            temporary = Path(folder)
            config = temporary / 'certificate.cnf'
            config.write_text(CERTIFICATE_CONFIG)
            private_key, public_key, archive = (temporary / name for name in ('key.pem', 'certificate.pem', 'identity.p12'))
            _run(['/usr/bin/openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-sha256', '-days', '7300',
                  '-config', str(config), '-keyout', str(private_key), '-out', str(public_key)])
            private_key.chmod(0o600)
            environment = os.environ.copy()
            environment['BRANCH_DESK_SIGNING_PASSWORD'] = password
            _run(['/usr/bin/openssl', 'pkcs12', '-export', '-inkey', str(private_key), '-in', str(public_key),
                  '-out', str(archive), '-passout', 'env:BRANCH_DESK_SIGNING_PASSWORD'], env=environment)
            archive.chmod(0o600)
            try:
                _run(['/usr/bin/security', 'create-keychain', '-p', password, str(keychain)])
            finally:
                # create-keychain adds a search entry; this identity is only used explicitly.
                search = shlex.split(_run(['/usr/bin/security', 'list-keychains', '-d', 'user']).decode())
                if str(keychain) in search:
                    _run(['/usr/bin/security', 'list-keychains', '-d', 'user', '-s',
                          *(value for value in search if value != str(keychain))])
            keychain.chmod(0o600)
            _run(['/usr/bin/security', 'unlock-keychain', '-p', password, str(keychain)])
            _run(['/usr/bin/security', 'set-keychain-settings', '-l', '-u', '-t', '300', str(keychain)])
            _run(['/usr/bin/security', 'import', str(archive), '-k', str(keychain), '-P', password,
                  '-T', '/usr/bin/codesign', '-x'])
            _write_private(certificate, public_key.read_bytes())
    for path in files:
        if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid():
            raise ValueError('本地签名身份不完整或路径不安全；请保留 Signing 目录，勿重新生成身份。')
        path.chmod(0o600)
    password = password_file.read_text()
    if len(password) < 32 or any(character.isspace() for character in password):
        raise ValueError('本地签名密码文件无效；已保留原身份，不会重新生成。')
    digest = hashlib.sha1(_run(['/usr/bin/openssl', 'x509', '-in', str(certificate), '-outform', 'der'])).hexdigest().upper()
    identities = _run(['/usr/bin/security', 'find-identity', '-p', 'codesigning', str(keychain)]).decode()
    if f'{digest} "{CERTIFICATE_NAME}"' not in identities:
        raise ValueError('专用钥匙串中的签名证书与保存的身份不一致；不会使用其他证书。')
    return keychain, password, digest


def sign_bundle(bundle):
    SIGNING.mkdir(parents=True, exist_ok=True, mode=0o700)
    if SIGNING.is_symlink() or SIGNING.stat().st_uid != os.getuid():
        raise ValueError('本地签名目录不安全，请检查 Application Support/Branch Desk/Signing。')
    SIGNING.chmod(0o700)
    with os.fdopen(os.open(SIGNING / '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600), 'rb') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        keychain = SIGNING / 'branch-desk.keychain-db'
        try:
            keychain, password, digest = _identity()
            requirement = f'identifier "{IDENTIFIER}" and certificate leaf = H"{digest}"'
            _run(['/usr/bin/security', 'unlock-keychain', '-p', password, str(keychain)])
            _run(['/usr/bin/codesign', '--force', '--timestamp=none', '--keychain', str(keychain),
                  '--sign', digest, '--identifier', IDENTIFIER, '--requirements', '=designated => ' + requirement,
                  str(bundle)])
            _run(['/usr/bin/codesign', '--verify', '--strict', '-R', '=' + requirement, str(bundle)])
        finally:
            if keychain.is_file():
                _run(['/usr/bin/security', 'lock-keychain', str(keychain)])
