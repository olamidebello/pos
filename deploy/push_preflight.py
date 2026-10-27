"""Reject accidental branch, remote, staged-file or credential publication."""
import pathlib
import subprocess
import sys


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def check(root, branch, remote, url, paths):
    root = pathlib.Path(root).resolve(strict=True)
    if git(root, 'rev-parse', '--show-toplevel') != str(root):
        raise ValueError('Use the repository root')
    if git(root, 'branch', '--show-current') != branch:
        raise ValueError('Checkout the requested branch first; this script never switches branches')
    subprocess.run(['git', 'check-ref-format', '--branch', branch], check=True, capture_output=True)
    fetch = git(root, 'remote', 'get-url', '--all', remote).splitlines()
    push = git(root, 'remote', 'get-url', '--push', '--all', remote).splitlines()
    if fetch != [url] or push != [url]:
        raise ValueError('Remote URL mismatch or multiple push URLs')
    if url not in ('https://github.com/olamidebello/pos.git', 'git@github.com:olamidebello/pos.git'):
        raise ValueError('Only the POS GitHub repository is supported')
    selected = []
    for item in paths:
        p = pathlib.PurePosixPath(item)
        if p.is_absolute() or '..' in p.parts or item in ('.', '') or item.startswith('-'):
            raise ValueError('Select explicit repository-relative files or directories')
        selected.append(str(p))
    def included(name):
        return any(name == p or name.startswith(p + '/') for p in selected)
    staged = git(root, 'diff', '--cached', '--name-only', '-z').split('\0')
    if any(name and not included(name) for name in staged):
        raise ValueError('Unrelated staged files exist; unstage them first')
    candidates = git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard', '--', *selected).split('\0')
    forbidden = {'.env', 'id_rsa', 'id_ed25519', 'settings.yml', 'vars.yml', 'vault.yml'}
    for name in filter(None, candidates):
        p = root / name
        if p.name in forbidden or p.suffix.lower() in ('.key', '.pem', '.p12', '.pfx', '.db', '.sqlite', '.jks', '.keystore'):
            raise ValueError('Do not publish credential, runtime database, or private settings files: ' + name)
        if p.is_symlink():
            raise ValueError('Review symlinks manually before publishing: ' + name)
        if p.is_file():
            content = p.read_bytes()
            if any(marker in content for marker in (b'-----BEGIN ' + b'PRIVATE KEY-----', b'-----BEGIN RSA ' + b'PRIVATE KEY-----', b'-----BEGIN OPENSSH ' + b'PRIVATE KEY-----')):
                raise ValueError('Private key material detected: ' + name)
    return selected


if __name__ == '__main__':
    try:
        check(*sys.argv[1:5], sys.argv[5:])
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        sys.exit(str(exc))
