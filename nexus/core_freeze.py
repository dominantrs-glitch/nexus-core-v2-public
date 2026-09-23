"""Owner-local native migration fence. No upload, cutover, authority promotion or thaw.

SQLite triggers protect already-running/older writers too. The four local ledgers
are locked and fenced in one attached-database transaction; WAL is refused because
it cannot provide that cross-database atomicity. Binary files stay immutable and
must still be independently verified by export_core/restore_core.
"""
from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from nexus.artifacts import AccessDenied, InsufficientContext


# Each expression resolves a row's owning project, including child-table rows.
TABLES = {
    'main': {'projects': '{r}.id', 'contracts': '{r}.project', 'events': '{r}.project'},
    'ctx': {'sources': '{r}.project', 'contexts': '{r}.project', 'policies': '{r}.project',
            **{t: '(SELECT project FROM contexts WHERE id={r}.id AND revision={r}.revision)'
               for t in ('context_projects', 'context_operations', 'context_readers')}},
    'asset': {'artifacts': '{r}.project',
              **{t: '(SELECT project FROM artifacts WHERE id={r}.id)' for t in ('revisions', 'grants')}},
    'approval': {'approvals': '{r}.project'},
}


def archived(database, project):
    """Read only. Normal Task/learning callers must not use a frozen archive as live."""
    database = Path(database).resolve(strict=True)
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nexus_core_fences'").fetchone()
        return bool(exists and db.execute('SELECT 1 FROM nexus_core_fences WHERE project=?', (project,)).fetchone())


@contextmanager
def _locked(task):
    task.adapter.capability.authenticate(task.owner)
    paths = {'main': task.projects.database, 'ctx': task.contexts.database,
             'asset': task.artifacts.database, 'approval': task.adapter.approvals.database}
    paths = {k: Path(p).resolve(strict=True) for k, p in paths.items()}
    if len(set(paths.values())) != len(paths):
        raise ValueError('distinct native ledgers required')
    db = sqlite3.connect(paths['main'], isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        for schema, path in paths.items():
            if schema != 'main':
                db.execute(f'ATTACH DATABASE ? AS {schema}', (str(path),))
            mode = db.execute(f'PRAGMA {schema}.journal_mode').fetchone()[0]
            if mode != 'delete':
                raise ValueError('native freeze requires rollback DELETE journals; no automatic mode change')
            db.execute(f'PRAGMA {schema}.synchronous=FULL')
        db.execute('BEGIN IMMEDIATE')
        yield db
        db.execute('COMMIT')
    except BaseException:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise
    finally:
        db.close()


def _snapshot(db, task):
    project = db.execute('SELECT * FROM projects WHERE id=? AND owner=?', (task.project, task.owner)).fetchone()
    if project is None:
        raise AccessDenied('project unavailable')
    tables = {}
    for schema, selected in TABLES.items():
        for table, expression in selected.items():
            predicate = expression.format(r='item') + '=?'
            args = [task.project]
            if schema == 'approval':
                predicate += ' AND item.owner=?'
                args.append(task.owner)
            # Qualify parent subqueries to their attached schema (never model SQL).
            if schema == 'ctx':
                predicate = predicate.replace('FROM contexts ', 'FROM ctx.contexts ')
            if schema == 'asset':
                predicate = predicate.replace('FROM artifacts ', 'FROM asset.artifacts ')
            rows = [dict(r) for r in db.execute(f'SELECT item.* FROM {schema}.{table} AS item WHERE {predicate}', args)]
            if any('owner' in row and row['owner'] != task.owner for row in rows):
                raise AccessDenied('mixed-owner project cannot be fenced')
            tables[f'{schema}.{table}'] = sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))
    raw = json.dumps(tables, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return dict(project=task.project, contract_revision=project['revision'],
                source_digest=hashlib.sha256(raw).hexdigest(),
                counts={key: len(rows) for key, rows in tables.items()})


def inspect_core(task):
    """Metadata-only, consistent inventory; authenticates but changes no ledger rows."""
    with _locked(task) as db:
        return _snapshot(db, task)


def _triggers(db, schema, table, expression):
    keys = [r['name'] for r in db.execute(f'PRAGMA {schema}.table_info({table})') if r['pk']]
    if not keys:
        raise InsufficientContext('native table key unavailable')
    for event, rows in [('INSERT', ['NEW']), ('UPDATE', ['OLD', 'NEW']), ('DELETE', ['OLD'])]:
        conditions = []
        for row in rows:
            owner = f' AND f.owner={row}.owner' if schema == 'approval' else ''
            conditions.append(f'EXISTS(SELECT 1 FROM nexus_core_fences AS f WHERE f.project={expression.format(r=row)}{owner})')
        if event != 'DELETE':
            # REPLACE deletes conflicting rows without firing delete triggers when
            # recursive_triggers is off (the old-client default). Check the victim
            # explicitly, including UPDATE OR REPLACE with a changed primary key.
            owner = ' AND f.owner=victim.owner' if schema == 'approval' else ''
            match = ' AND '.join(f'victim.{key}=NEW.{key}' for key in keys)
            conditions.append(f'EXISTS(SELECT 1 FROM {table} AS victim JOIN nexus_core_fences AS f '
                              f'ON f.project={expression.format(r="victim")}{owner} WHERE {match})')
        name = f'nexus_freeze_{table}_{event.lower()}'
        body = f'BEFORE {event} ON {table} WHEN {" OR ".join(conditions)} BEGIN SELECT RAISE(ABORT,\'core_frozen_for_migration\'); END'
        yield name, body


def _check_triggers(db, *, create=False):
    for schema, selected in TABLES.items():
        for table, expression in selected.items():
            for name, body in _triggers(db, schema, table, expression):
                if create:
                    db.execute(f'CREATE TRIGGER IF NOT EXISTS {schema}.{name} {body}')
                row = db.execute(f'SELECT sql FROM {schema}.sqlite_master WHERE type=\'trigger\' AND name=?', (name,)).fetchone()
                if not row or row[0] != f'CREATE TRIGGER {name} {body}':
                    raise InsufficientContext('native fence trigger missing or changed; trusted recovery required')


def freeze_core(task, expected_digest):
    """Fence exactly the reviewed state; existing readers remain available for backup."""
    with _locked(task) as db:
        state = _snapshot(db, task)
        if state['source_digest'] != expected_digest:
            raise ValueError('native source changed; inspect again')
        existing = []
        for schema in TABLES:
            exists = db.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name='nexus_core_fences'").fetchone()
            row = db.execute(f'SELECT * FROM {schema}.nexus_core_fences WHERE project=? AND owner=?',
                             (task.project, task.owner)).fetchone() if exists else None
            existing.append(dict(row) if row else None)
        if any(existing):
            if not all(existing) or any(row != existing[0] for row in existing) or existing[0]['source_digest'] != expected_digest:
                raise InsufficientContext('partial or mismatched native fence; trusted recovery required')
            _check_triggers(db)
            return dict(state, generation=existing[0]['generation'], status='frozen')
        generation = 'native-freeze-' + uuid.uuid4().hex
        for schema, selected in TABLES.items():
            db.execute(f'CREATE TABLE IF NOT EXISTS {schema}.nexus_core_fences('
                       'project TEXT NOT NULL,owner TEXT NOT NULL,generation TEXT NOT NULL,source_digest TEXT NOT NULL,'
                       'PRIMARY KEY(project,owner))')
            db.execute(f'INSERT INTO {schema}.nexus_core_fences VALUES(?,?,?,?)',
                       (task.project, task.owner, generation, expected_digest))
        _check_triggers(db, create=True)
        return dict(state, generation=generation, status='frozen')


def verify_frozen_core(task, expected):
    """Verify an existing fence without installing, repairing or removing one."""
    with _locked(task) as db:
        state = _snapshot(db, task)
        if state['source_digest'] != expected['source_digest']:
            raise InsufficientContext('frozen source changed')
        for schema in TABLES:
            exists = db.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name='nexus_core_fences'").fetchone()
            row = db.execute(f'SELECT * FROM {schema}.nexus_core_fences WHERE project=? AND owner=?',
                             (task.project, task.owner)).fetchone() if exists else None
            if row is None or dict(row) != dict(project=task.project, owner=task.owner,
                    generation=expected['generation'], source_digest=expected['source_digest']):
                raise InsufficientContext('native source fence unavailable')
        _check_triggers(db)
        return state
