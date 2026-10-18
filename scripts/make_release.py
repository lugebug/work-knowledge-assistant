"""Build a source installer ZIP from an explicit, data-free file allowlist."""
from pathlib import Path
import argparse
import hashlib
import json
import zipfile
from verify_release import ROOT, MANIFEST, files, scan, verify


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seal-only', action='store_true')
    parser.add_argument('--output')
    args = parser.parse_args()
    paths = files()
    scan(paths)
    version = (ROOT / 'VERSION').read_text('utf-8').strip()
    manifest = {'version': version, 'format': 1,
                'files': {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}}
    (ROOT / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    verification = verify()
    if args.seal_only:
        print(json.dumps(verification, ensure_ascii=False))
        return
    output = Path(args.output or ROOT / 'dist').resolve()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / ('work-knowledge-assistant-' + version + '-windows.zip')
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zipped:
        for path in [*paths, ROOT / MANIFEST]:
            # Stable ZIP metadata for reproducible payloads.
            info = zipfile.ZipInfo('work-knowledge-assistant/' + path.relative_to(ROOT).as_posix(), (2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zipped.writestr(info, path.read_bytes())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / (archive.name + '.sha256')).write_text(digest + '  ' + archive.name + '\n', encoding='ascii')
    print(json.dumps(dict(verification, archive=str(archive), sha256=digest, size=archive.stat().st_size), ensure_ascii=False))


if __name__ == '__main__':
    main()
