"""Trusted local migration writer fence. No upload, remote tool or owner approval.

SQLite triggers also fence already-running old clients. Freezing obtains the same
write lock as Intake.save, so an in-flight transaction finishes before the final
snapshot is taken. Only selected intake projects and their draft receipts freeze.
"""
import argparse
import json
from pathlib import Path
import sys
import uuid


def install(db):
    db.execute('''CREATE TABLE IF NOT EXISTS intake_fences (
        project TEXT PRIMARY KEY REFERENCES projects(id), generation TEXT NOT NULL,
        snapshot_sha256 TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    db.execute('''CREATE TABLE IF NOT EXISTS intake_routes (
        project TEXT PRIMARY KEY REFERENCES intake_fences(project),
        generation TEXT NOT NULL, config_root TEXT NOT NULL)''')
    # DB enforcement is intentional: an old service need not have loaded new Python.
    for table, project_expr in (('projects', '{row}.id'), ('notes', '{row}.project'),
            ('requests', "json_extract({row}.result, '$.project')")):
        for operation, rows in (('INSERT', ('NEW',)), ('UPDATE', ('OLD','NEW')), ('DELETE', ('OLD',))):
            if table == 'projects' and operation == 'INSERT':
                continue
            condition = ' OR '.join('EXISTS (SELECT 1 FROM intake_fences WHERE project = '+
                project_expr.format(row=row)+')' for row in rows)
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS fence_{table}_{operation.lower()}
                BEFORE {operation} ON {table} WHEN {condition}
                BEGIN SELECT RAISE(ABORT, 'project frozen for migration'); END''')


def writable(db, project):
    if db.execute('SELECT 1 FROM intake_fences WHERE project=?', (project,)).fetchone():
        raise ValueError('project frozen for migration; writes paused')


def freeze(store, projects, expected_snapshot):
    from nexus.git_migration import snapshot_db, digest, inspect
    if not projects or len(set(projects)) != len(projects):
        raise ValueError('explicit unique project selection required')
    with store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        for project in projects:
            store._project(db, project, False)
            writable(db, project)
        selected = snapshot_db(db, projects)
        checksum = digest(selected)
        if checksum != expected_snapshot:
            raise ValueError('snapshot changed; review again before freezing')
        if inspect(selected)['blockers']:
            raise ValueError('migration audit has blockers')
        generation = 'freeze-' + uuid.uuid4().hex
        db.executemany('INSERT INTO intake_fences(project,generation,snapshot_sha256) VALUES (?,?,?)',
                       [(p,generation,checksum) for p in projects])
    return dict(generation=generation, source_snapshot_sha256=checksum, projects=projects, state='frozen')


def frozen_snapshot(store, generation):
    from nexus.git_migration import snapshot_db, digest
    with store.db() as db:
        db.execute('BEGIN')
        fences = db.execute('SELECT * FROM intake_fences WHERE generation=? ORDER BY project', (generation,)).fetchall()
        if not fences:
            raise ValueError('freeze generation unavailable')
        if db.execute('SELECT 1 FROM intake_routes WHERE project IN (SELECT project FROM intake_fences WHERE generation=?)',
                      (generation,)).fetchone():
            raise ValueError('cutover already routed; reconcile destination before any rollback')
        selected = snapshot_db(db, [r['project'] for r in fences])
        # Hash independent of the original caller's project order.
        expected = {r['snapshot_sha256'] for r in fences}
        if len(expected) != 1 or digest(selected) not in expected:
            raise ValueError('frozen source changed; do not export or thaw')
        return selected


def thaw(store, generation):
    """Cancel a PRE-CUTOVER freeze. Never use after a destination accepts writes."""
    from nexus.git_migration import snapshot_db, digest
    with store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        fences = db.execute('SELECT * FROM intake_fences WHERE generation=? ORDER BY project', (generation,)).fetchall()
        if not fences:
            raise ValueError('freeze generation unavailable')
        if db.execute('SELECT 1 FROM intake_routes WHERE project IN (SELECT project FROM intake_fences WHERE generation=?)',
                      (generation,)).fetchone():
            raise ValueError('cutover already routed; reconcile destination before any rollback')
        selected = snapshot_db(db, [r['project'] for r in fences])
        if {r['snapshot_sha256'] for r in fences} != {digest(selected)}:
            raise ValueError('frozen source changed; do not thaw')
        db.execute('DELETE FROM intake_fences WHERE generation=?', (generation,))
    return dict(state='local-writable', projects=[r['project'] for r in fences])


def backup(store, generation, target):
    """Project-scoped local backup, excluding every native owner/credential DB."""
    from nexus.git_migration import encoded, digest
    selected = frozen_snapshot(store, generation)
    payload = dict(format='nexus-intake-frozen-v1', generation=generation,
                   source_snapshot_sha256=digest(selected), projects=selected)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('xb') as stream:
        stream.write(encoded(payload))
    return dict(path=str(target), projects=len(selected), source_snapshot_sha256=digest(selected))


def restore(source, target):
    """Restore into a NEW local root and keep it fenced against split writers."""
    from nexus.intake import Intake, identifier
    from nexus.git_migration import digest, inspect
    source, target = Path(source), Path(target)
    if source.stat().st_size > 64 * 1024 * 1024:
        raise ValueError('backup size limit')
    payload = json.loads(source.read_text(encoding='utf-8'))
    if (set(payload) != {'format','generation','source_snapshot_sha256','projects'} or
            payload['format'] != 'nexus-intake-frozen-v1'):
        raise ValueError('unsupported backup')
    identifier(payload['generation'])
    selected = payload['projects']
    if not isinstance(selected, list) or not selected or digest(selected) != payload['source_snapshot_sha256']:
        raise ValueError('backup integrity mismatch')
    if inspect(selected)['blockers']:
        raise ValueError('backup audit has blockers')
    projects = [identifier(item['project']['id']) for item in selected]
    if len(set(projects)) != len(projects):
        raise ValueError('duplicate backup projects')
    target.mkdir(parents=True, exist_ok=False)
    store = Intake(target)
    with store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        for item in selected:
            p = item['project']
            db.execute('INSERT INTO projects VALUES (?,?,?,?,?)', tuple(p[k] for k in ('id','title','revision','remote','source')))
            for n in item['notes']:
                fields = ('id','project','revision','kind','body','source','evidence','quote','supersedes','created')
                db.execute('INSERT INTO notes VALUES (?,?,?,?,?,?,?,?,?,?)', tuple(n[k] for k in fields))
            for r in item['requests']:
                db.execute('INSERT INTO requests VALUES (?,?,?)', (r['key'],r['digest'],json.dumps(r['result'])))
        db.executemany('INSERT INTO intake_fences(project,generation,snapshot_sha256) VALUES (?,?,?)',
            [(p,payload['generation'],payload['source_snapshot_sha256']) for p in projects])
    # Read back with the same integrity check; never silently enable restored writes.
    if frozen_snapshot(store, payload['generation']) != selected:
        raise ValueError('restore verification failed')
    return dict(path=str(target), projects=len(projects), state='frozen',
                source_snapshot_sha256=payload['source_snapshot_sha256'])


def main():
    from nexus.intake import Intake, default_root
    from nexus.git_migration import stage
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=default_root())
    commands = parser.add_subparsers(dest='command', required=True)
    f = commands.add_parser('freeze')
    f.add_argument('--project', action='append', required=True)
    f.add_argument('--expected-snapshot', required=True)
    e = commands.add_parser('export')
    e.add_argument('--generation', required=True)
    e.add_argument('--owner', required=True)
    e.add_argument('--output', type=Path, required=True)
    t = commands.add_parser('thaw-before-cutover')
    t.add_argument('--generation', required=True)
    b = commands.add_parser('backup')
    b.add_argument('--generation', required=True)
    b.add_argument('--output', type=Path, required=True)
    r = commands.add_parser('restore')
    r.add_argument('--source', type=Path, required=True)
    r.add_argument('--output', type=Path, required=True)
    a = parser.parse_args()
    if a.command == 'restore':
        print(json.dumps(restore(a.source, a.output), ensure_ascii=False, indent=2))
        return
    if not (a.root/'intake.sqlite3').is_file():
        parser.error('existing intake database required')
    store = Intake(a.root)
    if a.command == 'freeze':
        result = freeze(store, sorted(a.project), a.expected_snapshot)
    elif a.command == 'export':
        result = stage(frozen_snapshot(store, a.generation), a.owner, a.output)
    elif a.command == 'backup':
        result = backup(store, a.generation, a.output)
    else:
        result = thaw(store, a.generation)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__': main()
