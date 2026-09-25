"""Prepare a reviewed, history-free source candidate. Never publishes or deploys.

The private allowlist pins every input hash and exact replacement count. It is
deliberately not included in the candidate; exported files get a separate manifest.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import zipfile


def sha(data):
    return hashlib.sha256(data).hexdigest()


def relative(value):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        raise ValueError('invalid relative path')
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(p in {'', '.', '..'} for p in path.parts):
        raise ValueError('invalid relative path')
    blocked = {'.git', '.venv', 'runtime', 'node_modules', '.wrangler', '__pycache__', 'config-backups'}
    if any(p.lower() in blocked for p in path.parts):
        raise ValueError('private or generated directory')
    if path.name.startswith(('.env', '.dev.vars')) or '.remote.' in path.name or path.suffix.lower() in {
            '.db', '.sqlite', '.sqlite3', '.pem', '.key', '.pfx', '.p12', '.bundle'}:
        raise ValueError('private file type')
    return path


def checked_file(root, name):
    path = root.joinpath(*relative(name).parts)
    for node in [path, *path.parents]:
        if node == root:
            break
        if node.is_symlink() or (hasattr(node, 'is_junction') and node.is_junction()):
            raise ValueError('linked input is not allowed')
    if not path.resolve().is_relative_to(root) or not path.is_file():
        raise ValueError('input outside source or unavailable')
    return path


def assemble(root, manifest):
    root = Path(root).resolve(strict=True)
    if manifest.get('schema') != 1 or not isinstance(manifest.get('files'), list) or not manifest['files']:
        raise ValueError('explicit nonempty allowlist required')
    output, records = {}, []
    folded = set()
    private_patterns = [re.compile(p, re.I) for p in manifest.get('private_patterns', [])]
    secret_patterns = [
        re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\s+[A-Za-z0-9+/=\r\n]{80,}'),
        re.compile(r'\bgh[pousr]_[A-Za-z0-9]{25,}\b'),
        re.compile(r'\bgithub_pat_[A-Za-z0-9_]{40,}\b'),
    ]
    for entry in manifest['files']:
        source, target = entry['source'], entry['path']
        relative(target)
        if target.casefold() in folded or target == 'release-manifest.json':
            raise ValueError('duplicate or reserved output')
        folded.add(target.casefold())
        data = checked_file(root, source).read_bytes()
        if sha(data) != entry['sha256']:
            raise ValueError('reviewed source changed: ' + source)
        if entry.get('binary'):
            if entry.get('replacements') or target != 'nexus/data/synthetic-original.png':
                raise ValueError('unreviewed binary')
        else:
            text = data.decode('utf-8-sig')
            for change in entry.get('replacements', []):
                if not change['from'] or text.count(change['from']) != change['count']:
                    raise ValueError('reviewed replacement changed: ' + source)
                text = text.replace(change['from'], change['to'])
            if any(pattern.search(text) for pattern in private_patterns + secret_patterns):
                raise ValueError('private content detected in ' + target)
            data = text.replace('\r\n', '\n').encode('utf-8')
        output[target] = data
        records.append({'path': target, 'sha256': sha(data), 'bytes': len(data)})
    license_name = manifest.get('license')
    if license_name not in {None, 'MIT'}:
        raise ValueError('unsupported release license')
    if license_name == 'MIT' and 'LICENSE' not in output:
        raise ValueError('release license file required')
    report = {'schema': 1, 'stage': 'source-release' if license_name else 'local-review-candidate',
              'publication_action': 'not_performed_by_builder',
              'license_status': license_name or 'owner_selection_pending', 'history_included': False,
              'files': sorted(records, key=lambda row: row['path'])}
    output['release-manifest.json'] = (json.dumps(report, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    return output, report


def build(root, manifest, destination):
    destination = Path(destination).absolute()
    archive = destination.with_name(destination.name + '.zip')
    if destination.exists() or archive.exists():
        raise ValueError('destination and archive must be new')
    for parent in destination.parents:
        if parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction()):
            raise ValueError('linked destination is not allowed')
    files, report = assemble(root, manifest)
    destination.mkdir(parents=False)
    for name, data in files.items():
        target = destination.joinpath(*PurePosixPath(name).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as stream:
            stream.write(data)
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            bundle.writestr(info, data)
    verify(destination)
    with zipfile.ZipFile(archive) as bundle:
        if set(bundle.namelist()) != set(files) or any(bundle.read(p) != b for p, b in files.items()):
            raise ValueError('archive verification failed')
    return {'directory': str(destination), 'archive': str(archive), 'files': len(report['files']),
            'bytes': sum(r['bytes'] for r in report['files']), 'zip_sha256': sha(archive.read_bytes()),
            'published': False, 'license_status': report['license_status']}


def verify(directory):
    directory = Path(directory).resolve(strict=True)
    manifest = json.loads(checked_file(directory, 'release-manifest.json').read_text(encoding='utf-8'))
    expected = {r['path'] for r in manifest['files']} | {'release-manifest.json'}
    actual = {p.relative_to(directory).as_posix() for p in directory.rglob('*') if p.is_file()}
    if actual != expected:
        raise ValueError('candidate has unlisted or missing files')
    for row in manifest['files']:
        data = checked_file(directory, row['path']).read_bytes()
        if len(data) != row['bytes'] or sha(data) != row['sha256']:
            raise ValueError('candidate bytes changed')
    return {'files': len(manifest['files']), 'bytes_verified': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--verify', type=Path)
    args = parser.parse_args()
    if args.verify:
        result = verify(args.verify)
    else:
        if not args.manifest or not args.destination:
            parser.error('--manifest and --destination are required')
        result = build(args.root, json.loads(args.manifest.read_text(encoding='utf-8')), args.destination)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
