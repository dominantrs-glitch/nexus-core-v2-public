"""Protected owner-local environment snapshots and isolated restoration.

No network, service launch, live overwrite or automatic approval. Plans enumerate
exact data roots, configuration files and Git refs. Every payload (including Git
and configuration) is sealed with DPAPI or an explicit portable recovery key.
Portable data restore does not transfer Windows authentication or activate services.
"""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone

def DpapiProtector():
    # Portable restoration must bootstrap without the local owner subsystem.
    from nexus.owner import DpapiProtector as WindowsProtector
    return WindowsProtector()

ENTROPY = b'nexus-environment-recovery-v1'
MAX_FILE = 256 * 1024 * 1024
MAX_TOTAL = 8 * 1024 * 1024 * 1024


def digest(data):
    return hashlib.sha256(data).hexdigest()


def relative(value):
    if not isinstance(value, str) or '\\' in value or ':' in value:
        raise ValueError('invalid recovery path')
    p = PurePosixPath(value)
    if not value or p.is_absolute() or str(p) != value or any(x in {'.', '..'} or x.endswith((' ', '.')) or
            re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', x) for x in p.parts):
        raise ValueError('invalid recovery path')
    return p


def safe_file(path, root):
    path, root = Path(path), Path(root).resolve(strict=True)
    if not path.resolve(strict=True).is_relative_to(root):
        raise ValueError('recovery input outside selected root')
    for node in [path, *path.parents]:
        if node.is_symlink() or (hasattr(node, 'is_junction') and node.is_junction()):
            raise ValueError('linked recovery input')
        if node == root:
            break
    if not path.is_file() or path.stat().st_size > MAX_FILE:
        raise ValueError('invalid or oversized recovery file')
    return path


def _git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True,
                          timeout=90).stdout


def _sqlite_bytes(path):
    with tempfile.TemporaryDirectory(prefix='nexus-recovery-db-') as tmp:
        target = Path(tmp) / 'snapshot.sqlite3'
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as source:
            with closing(sqlite3.connect(target)) as copy:
                source.backup(copy)
                if copy.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('database snapshot integrity failed')
                if copy.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('database snapshot reference check failed')
        return target.read_bytes()


def create(plan, destination, protector=None):
    protector = protector or DpapiProtector()
    destination = Path(destination).resolve()
    protection = getattr(protector, 'protection', 'current-windows-user-dpapi')
    portability = getattr(protector, 'credential_portability', 'same Windows user only; reauthorize on another identity')
    if hasattr(protector, 'key_file') and protector.key_file.is_relative_to(destination):
        raise ValueError('recovery key must be stored outside the backup package')
    if plan.get('schema') != 1 or set(plan) - {'schema', 'roots', 'files', 'repositories', 'activation'}:
        raise ValueError('explicit recovery plan required')
    candidates, names, repos, exclusions = [], set(), [], []
    for entry in plan.get('roots', []):
        name, root = str(relative(entry['name'])), Path(entry['path']).resolve(strict=True)
        if destination.is_relative_to(root) or not root.is_dir():
            raise ValueError('snapshot must be outside scanned roots')
        excluded = [str(relative(p)).casefold() for p in entry.get('exclude', [])]
        if excluded and not str(entry.get('exclude_reason', '')).strip():
            raise ValueError('explicit recovery exclusions need a reason')
        if excluded:
            exclusions.append(dict(root=name, paths=entry['exclude'], reason=entry['exclude_reason']))
        for path in sorted(root.rglob('*')):
            key = path.relative_to(root).as_posix().casefold()
            if any(key == skip or key.startswith(skip + '/') for skip in excluded):
                continue
            if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
                raise ValueError('linked recovery input')
            if not path.is_file() or path.name.endswith(('-wal', '-shm', '-journal')):
                continue
            candidates.append((f'roots/{name}/{path.relative_to(root).as_posix()}', safe_file(path, root)))
    for entry in plan.get('files', []):
        path = Path(entry['path']).absolute()
        candidates.append(('files/' + str(relative(entry['name'])), safe_file(path, path.parent)))
    for key, _ in candidates:
        if key.casefold() in names:
            raise ValueError('duplicate recovery destination')
        names.add(key.casefold())
    for entry in plan.get('repositories', []):
        name, repo, ref = str(relative(entry['name'])), Path(entry['path']).resolve(strict=True), entry['ref']
        if not isinstance(ref, str) or ref.startswith('-'):
            raise ValueError('invalid repository ref')
        sha = _git(repo, 'rev-parse', '--verify', ref + '^{commit}').decode().strip()
        if not re.fullmatch('[a-f0-9]{40}', sha):
            raise ValueError('invalid repository checkpoint')
        key = f'repositories/{name}.bundle'
        if key.casefold() in names:
            raise ValueError('duplicate recovery destination')
        names.add(key.casefold())
        repos.append((key, repo, ref, sha))
    if not candidates and not repos:
        raise ValueError('empty recovery plan')
    destination.mkdir(parents=True, exist_ok=False)
    records, total = [], 0

    def seal(key, data, kind, source):
        nonlocal total
        total += len(data)
        if len(data) > MAX_FILE or total > MAX_TOTAL:
            raise ValueError('recovery budget exceeded')
        sealed = protector.protect(data, ENTROPY)
        if protector.unprotect(sealed, ENTROPY) != data:
            raise ValueError('protected recovery readback failed')
        capsule = f'{len(records):06d}.sealed'
        (destination / capsule).write_bytes(sealed)
        records.append(dict(path=key, capsule=capsule, sha256=digest(data), bytes=len(data), kind=kind,
                            sealed_sha256=digest(sealed), source=source))

    for key, source in candidates:
        with source.open('rb') as probe:
            is_sqlite = probe.read(16) == b'SQLite format 3\0'
        if is_sqlite or source.suffix.lower() in {'.sqlite3', '.sqlite'}:
            seal(key, _sqlite_bytes(source), 'sqlite', str(source))
        else:
            data = source.read_bytes()
            if digest(data) != digest(source.read_bytes()):
                raise ValueError('source changed during recovery snapshot')
            seal(key, data, 'file', str(source))
    for key, repo, ref, sha in repos:
        with tempfile.TemporaryDirectory(prefix='nexus-recovery-git-') as tmp:
            bundle = Path(tmp) / 'repository.bundle'
            _git(repo, 'bundle', 'create', str(bundle), ref)
            _git(repo, 'bundle', 'verify', str(bundle))
            if _git(repo, 'rev-parse', ref + '^{commit}').decode().strip() != sha:
                raise ValueError('repository changed during snapshot')
            seal(key, bundle.read_bytes(), 'git_bundle', dict(path=str(repo), ref=ref, commit=sha))
    manifest = dict(schema=1, format='nexus.environment-recovery.v1', protection=protection,
                    captured_at=datetime.now(timezone.utc).isoformat(),
                    consistency='per-database snapshots and exact Git checkpoints; live cutover requires a quiescent final capture',
                    records=records, exclusions=exclusions, activation=plan.get('activation', []), external_upload=False,
                    activation_performed=False, credential_portability=portability)
    # The manifest includes private locations. Protect it too; the outer file has
    # only bounded counts and the checksum needed to detect package corruption.
    data = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode('utf-8')
    capsule = protector.protect(data, ENTROPY)
    (destination / 'manifest.sealed').write_bytes(capsule)
    summary = dict(schema=1, format=manifest['format'], files=len(records), bytes=total,
                   manifest_sha256=digest(capsule), protection=manifest['protection'],
                   activation_performed=False, external_upload=False)
    (destination / 'recovery.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    return summary


def restore(source, destination, protector=None):
    protector = protector or DpapiProtector()
    source, destination = Path(source).resolve(strict=True), Path(destination).resolve()
    if destination == source or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError('restore must use a separate fresh directory')
    summary = json.loads(safe_file(source / 'recovery.json', source).read_text(encoding='utf-8'))
    if summary.get('protection') != getattr(protector, 'protection', 'current-windows-user-dpapi'):
        raise ValueError('use the matching recovery key or original Windows owner; no decryption fallback')
    sealed = safe_file(source / 'manifest.sealed', source).read_bytes()
    if summary.get('format') != 'nexus.environment-recovery.v1' or digest(sealed) != summary['manifest_sha256']:
        raise ValueError('recovery manifest integrity failed')
    manifest = json.loads(protector.unprotect(sealed, ENTROPY))
    if (manifest.get('format') != summary['format'] or len(manifest['records']) != summary['files'] or
            manifest.get('protection') != summary['protection']):
        raise ValueError('invalid protected recovery manifest')
    folded = set()
    for r in manifest['records']:
        key = str(relative(r['path']))
        if key.casefold() in folded or r['kind'] not in {'sqlite', 'file', 'git_bundle'}:
            raise ValueError('invalid recovery entry')
        folded.add(key.casefold())
        if not re.fullmatch(r'\d{6}\.sealed', r['capsule']):
            raise ValueError('invalid protected file')
        capsule = safe_file(source / r['capsule'], source).read_bytes()
        if digest(capsule) != r['sealed_sha256']:
            raise ValueError('recovery payload integrity failed')
    destination.mkdir(parents=True, exist_ok=False)
    total = 0
    for r in manifest['records']:
        data = protector.unprotect((source / r['capsule']).read_bytes(), ENTROPY)
        total += len(data)
        if len(data) != r['bytes'] or digest(data) != r['sha256'] or total > MAX_TOTAL:
            raise ValueError('decrypted recovery integrity failed')
        target = destination.joinpath(*relative(r['path']).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if r['kind'] == 'sqlite':
            with closing(sqlite3.connect(target.as_uri() + '?mode=ro', uri=True)) as db:
                if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('restored database integrity failed')
        elif r['kind'] == 'git_bundle':
            clone = target.with_suffix('.restored.git')
            # Remote-tracking checkpoints may have no refs/heads/main. Mirror
            # all advertised refs so an origin/main-only bundle is not empty.
            subprocess.run(['git', 'clone', '--mirror', str(target), str(clone)], check=True, capture_output=True, timeout=90)
            _git(clone, 'fsck', '--full')
            _git(clone, 'cat-file', '-e', r['source']['commit'] + '^{commit}')
    blobs = verify_artifact_blobs(destination)
    (destination / 'restore-map.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    report = dict(schema=1, files=len(manifest['records']), bytes=total, artifact_blobs=blobs,
                  status='isolated_restore_verified', services_started=False, live_data_overwritten=False,
                  activation_required=manifest['activation'], exclusions=manifest.get('exclusions', []),
                  credential_portability=manifest['credential_portability'])
    (destination / 'restore-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def verify_artifact_blobs(root):
    checked = 0
    for path in Path(root).rglob('*.sqlite3'):
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'artifacts', 'revisions', 'grants'} <= tables:
                continue
            for locator, expected, size in db.execute('SELECT locator,digest,size FROM revisions'):
                if not re.fullmatch('[a-f0-9]{64}', locator):
                    raise ValueError('nonlocal artifact provider needs explicit recovery adapter')
                blob = safe_file(path.parent / 'blobs' / locator, path.parent)
                if blob.stat().st_size != size or digest(blob.read_bytes()) != expected:
                    raise ValueError('restored artifact bytes unavailable or changed')
                checked += 1
    return checked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    create_parser = sub.add_parser('create')
    create_parser.add_argument('plan', type=Path)
    create_parser.add_argument('destination', type=Path)
    restore_parser = sub.add_parser('restore')
    restore_parser.add_argument('source', type=Path)
    restore_parser.add_argument('destination', type=Path)
    for command in (create_parser, restore_parser):
        command.add_argument('--key-file', type=Path, help='Existing separate portable recovery key; omit for DPAPI')
    key_parser = sub.add_parser('keygen')
    key_parser.add_argument('destination', type=Path)
    a = parser.parse_args()
    try:
        from nexus.recovery_crypto import PortableProtector, new_key
        if a.operation == 'keygen':
            result = new_key(a.destination)
        else:
            protector = PortableProtector(a.key_file) if a.key_file else None
            result = create(json.loads(a.plan.read_text(encoding='utf-8')), a.destination, protector) if a.operation == 'create' else restore(a.source, a.destination, protector)
        print(json.dumps(result, ensure_ascii=False))
    except Exception:
        # Never print credentials, private plan content or subprocess stderr.
        print(json.dumps(dict(status='recovery_incomplete',live_data_overwritten=False,
                              instruction='Inspect the protected package and selected paths locally; do not activate incomplete restoration.')))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
