"""Check the exact release file inventory and hashes without opening user data."""
from pathlib import Path
import hashlib
import json
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = 'release-manifest.json'
ROOT_FILES = ('AGENTS.md', 'README.md', 'VERSION', 'NOTICE.md', '.gitignore', '.gitattributes',
              'install.ps1', 'start.ps1', 'doctor.ps1', 'stop.ps1', 'requirements-connectors.txt')


def files(root=ROOT):
    paths = [root / name for name in ROOT_FILES if (root / name).is_file()]
    for directory, extensions in [('app', {'.py', '.sql', '.json'}), ('app/static', {'.html', '.css', '.js'}),
                                   ('scripts', {'.py', '.ps1'}), ('tests', {'.py'}), ('docs', {'.md'}),
                                   ('.github/workflows', {'.yml', '.yaml'})]:
        folder = root / directory
        if folder.is_dir():
            paths.extend(p for p in folder.iterdir() if p.is_file() and p.suffix in extensions)
    paths.extend(root / '.tools' / name for name in ('package.json', 'package-lock.json') if (root / '.tools' / name).is_file())
    licenses = root / 'licenses'
    if licenses.is_dir():
        paths.extend(p for p in licenses.iterdir() if p.is_file() and p.suffix in ('.txt', '.md'))
    for path in paths:
        if path.is_symlink() or root.resolve() not in path.resolve().parents:
            raise ValueError('Release file escapes project directory: ' + str(path.relative_to(root)))
    return sorted(paths)


def scan(paths, root=ROOT):
    forbidden = [re.compile(r'[A-Z]:[\\/]+Users[\\/]+[^\\/\s]+', re.I),
                 re.compile(r'(?:oc|ou|cli)_[a-f0-9]{16,}'),
                 re.compile(r'wxid_[a-z0-9]{12,}'),
                 re.compile(r'gh[pousr]_[A-Za-z0-9]{30,}'),
                 re.compile(r'sk-[A-Za-z0-9_-]{30,}')]
    for path in paths:
        text = path.read_text('utf-8-sig')
        for pattern in forbidden:
            if pattern.search(text):
                raise ValueError('Possible personal identity or credential in ' + str(path.relative_to(root)))
    scope = json.loads((root / 'app' / 'work_scope.json').read_text('utf-8'))
    if scope != {'conversations': [], 'documents': []}:
        raise ValueError('Release must have an empty initial source scope.')


def verify(root=ROOT):
    manifest = json.loads((root / MANIFEST).read_text('utf-8'))
    paths = files(root)
    scan(paths, root)
    actual = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    if actual != manifest['files']:
        changed = sorted(k for k in set(actual) | set(manifest['files']) if actual.get(k) != manifest['files'].get(k))
        raise ValueError('Release files changed or are missing: ' + ', '.join(changed))
    return {'ok': True, 'version': manifest['version'], 'verified_files': len(actual),
            'business_data_included': False, 'note': 'Hashes detect changed files; this is not a code signature.'}


if __name__ == '__main__':
    try:
        print(json.dumps(verify(), ensure_ascii=False))
    except (OSError, ValueError, KeyError) as error:
        print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False))
        sys.exit(1)
