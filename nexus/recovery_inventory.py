"""Owner-local recovery inventory and consistent SQLite snapshots.

No credential export or cloud upload. Per-database snapshots do not constitute
a complete system backup: artifact blobs, Git, deployment and access must be
verified separately. Existing live databases are never overwritten.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3


def _database(path):
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)


def inventory(root, snapshot_dir=None):
    root = Path(root).resolve()
    destination = Path(snapshot_dir).resolve() if snapshot_dir else None
    if destination:
        if any(destination.is_relative_to(root / name) for name in ('intake', 'personal', 'workspaces')):
            raise ValueError('backup must be outside the scanned stores')
        destination.mkdir(parents=True, exist_ok=False)
    records = []
    for name in ('intake', 'personal', 'workspaces'):
        directory = root / name
        for source in sorted(directory.rglob('*.sqlite3')):
            if not source.resolve().is_relative_to(root):
                raise ValueError('database outside configured local root')
            relative = source.relative_to(root)
            record = dict(store=relative.as_posix(), source='present', integrity='unknown', backup='not_checked')
            with closing(_database(source)) as db:
                if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('source database integrity failed')
                record['integrity'] = 'passed'
                if destination:
                    target = destination / relative
                    if not target.resolve().is_relative_to(destination):
                        raise ValueError('backup outside selected directory')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    pending = target.with_suffix('.pending.sqlite3')
                    if pending.exists() or target.exists():
                        raise ValueError('backup target already exists')
                    with closing(sqlite3.connect(pending)) as copy:
                        db.backup(copy)
                    with closing(_database(pending)) as restored:
                        if restored.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                            raise ValueError('restored database integrity failed')
                    pending.rename(target)
                    with target.open('rb') as stream:
                        checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
                    record.update(backup='created_and_opened', sha256=checksum)
            records.append(record)
    result = dict(schema=1, databases=records, consistency='per_database', complete_system_restore=False,
                  remaining=['Shared Git history and deployment configuration',
                             'Original artifact blobs and their verified manifests',
                             'Owner identity and connection credentials: separate protected recovery or reauthorization',
                             'External apps and scheduled tasks still using legacy paths'],
                  secrets_exported=False, external_upload=False)
    if destination:
        (destination / 'recovery-inventory.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(os.environ.get('LOCALAPPDATA', '.')) / 'NexusCoreV2')
    parser.add_argument('--snapshot-dir', type=Path)
    args = parser.parse_args()
    print(json.dumps(inventory(args.root, args.snapshot_dir), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
