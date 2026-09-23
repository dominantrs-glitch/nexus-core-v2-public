"""All projects and native confirmations here are synthetic fixtures."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from nexus.artifacts import AccessDenied, InsufficientContext
from nexus.core_backup import export_core, restore_core
from nexus.core_freeze import TABLES, archived, freeze_core, inspect_core
from nexus.owner import ApprovalLedger
from nexus.task import Task


class SyntheticSurface:
    def confirm(self, request):
        return True


class CoreFreezeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.task = self.make_task('selected')
        self.draft = dict(goal='Synthetic freeze test', acceptance={'file': ['machine']},
                          constraints=['Local synthetic only'], source='test fixture')
        self.task.start(self.draft)
        self.file = self.root / 'out.txt'
        self.file.write_text('synthetic bytes', encoding='utf-8')
        self.task.publish_output(self.file)
        self.paths = dict(main=self.task.projects.database, ctx=self.task.contexts.database,
                          asset=self.task.artifacts.database, approval=self.task.adapter.approvals.database)

    def make_task(self, project):
        return Task(self.root / 'task', project, 'owner', owner_root=self.root / 'owner',
                    surface=SyntheticSurface())

    def freeze(self):
        return freeze_core(self.task, inspect_core(self.task)['source_digest'])

    def assert_unfenced(self):
        for path in self.paths.values():
            self.assertFalse(archived(path, self.task.project))

    def test_exact_state_retry_and_existing_clients_blocked_other_project_works(self):
        old_client = self.make_task('selected')
        other = self.make_task('other')
        other.start(self.draft)
        before = self.task.projects.export('selected', 'owner')
        inspected = inspect_core(self.task)
        self.assertNotIn('synthetic bytes', str(inspected))
        frozen = self.freeze()
        self.assertEqual(frozen, self.freeze())
        for path in self.paths.values():
            with closing(sqlite3.connect(path)) as db:
                row = db.execute('SELECT generation,source_digest FROM nexus_core_fences WHERE project=?', ('selected',)).fetchone()
                self.assertEqual(row, (frozen['generation'], frozen['source_digest']))
        with self.assertRaisesRegex(InsufficientContext, 'archived'):
            old_client.read('implement')
        # Old code can bypass Task guards but still cannot mutate SQL ledgers.
        for schema, tables in TABLES.items():
            with closing(sqlite3.connect(self.paths[schema])) as db:
                for table, expr in tables.items():
                    predicate = expr.format(r=table) + "='selected'"
                    row = db.execute(f'SELECT * FROM {table} WHERE {predicate} LIMIT 1').fetchone()
                    self.assertIsNotNone(row, table)
                    columns = [r[1] for r in db.execute(f'PRAGMA table_info({table})')]
                    for sql, args in [
                        (f'DELETE FROM {table} WHERE {predicate}', ()),
                        (f'UPDATE {table} SET {columns[0]}={columns[0]} WHERE {predicate}', ()),
                        (f'INSERT INTO {table} VALUES({",".join("?" for _ in row)})', row),
                    ]:
                        with self.subTest(table=table, sql=sql):
                            with self.assertRaisesRegex(sqlite3.IntegrityError, 'core_frozen'):
                                db.execute(sql, args)
                            db.rollback()
        self.assertEqual(before, self.task.projects.export('selected', 'owner'))
        other.publish_output(self.file)
        other.verify('file', 'machine', 'pass', 'fixture')
        self.assertEqual(other.snapshot()['project']['revision'], 1)

    def test_same_contract_revision_event_change_invalidates_review(self):
        inspected = inspect_core(self.task)
        self.task.verify('file', 'machine', 'pass', 'fixture')
        self.assertEqual(inspected['contract_revision'], inspect_core(self.task)['contract_revision'])
        with self.assertRaisesRegex(ValueError, 'source changed'):
            freeze_core(self.task, inspected['source_digest'])
        self.assert_unfenced()

    def test_wal_refused_without_changing_journal_or_partial_fence(self):
        with closing(sqlite3.connect(self.paths['approval'])) as db:
            self.assertEqual(db.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
        with self.assertRaisesRegex(ValueError, 'DELETE journals'):
            inspect_core(self.task)
        self.assert_unfenced()

    def test_failure_in_last_ledger_rolls_back_all_fences(self):
        inspected = inspect_core(self.task)
        changed = {**TABLES, 'approval': {**TABLES['approval'], 'missing_table': '{r}.project'}}
        # Fail during final-schema trigger creation, after the first three markers.
        from nexus import core_freeze
        with patch.object(core_freeze, 'TABLES', changed), patch.object(core_freeze, '_snapshot', return_value=inspected):
            with self.assertRaises(InsufficientContext):
                freeze_core(self.task, inspected['source_digest'])
        self.assert_unfenced()
        self.assertEqual(inspected, inspect_core(self.task))

    def test_wrong_owner_and_partial_fence_refused(self):
        inspected = inspect_core(self.task)
        with patch.object(self.task, 'owner', 'someone-else'):
            with self.assertRaises(AccessDenied):
                freeze_core(self.task, inspected['source_digest'])
        self.assert_unfenced()
        frozen = self.freeze()
        with closing(sqlite3.connect(self.paths['approval'])) as db:
            db.execute('DELETE FROM nexus_core_fences')
            db.commit()
        with self.assertRaisesRegex(InsufficientContext, 'partial'):
            freeze_core(self.task, frozen['source_digest'])

    def test_replace_cannot_steal_frozen_rows_with_unfrozen_project(self):
        self.make_task('other').start(self.draft)
        self.freeze()
        for schema, tables in TABLES.items():
            with closing(sqlite3.connect(self.paths[schema])) as db:
                db.row_factory = sqlite3.Row
                self.assertEqual(db.execute('PRAGMA recursive_triggers').fetchone()[0], 0)
                for table, expr in tables.items():
                    row = dict(db.execute(f'SELECT * FROM {table} WHERE {expr.format(r=table)}=? LIMIT 1', ('selected',)).fetchone())
                    keys = [r['name'] for r in db.execute(f'PRAGMA table_info({table})') if r['pk']]
                    if 'project' not in row or 'project' in keys:
                        continue
                    row['project'] = 'other'
                    with self.subTest(table=table):
                        with self.assertRaisesRegex(sqlite3.IntegrityError, 'core_frozen'):
                            db.execute(f'INSERT OR REPLACE INTO {table} VALUES({",".join("?" for _ in row)})', list(row.values()))
                        db.rollback()

        with closing(sqlite3.connect(self.paths['ctx'])) as db:
            target = db.execute("SELECT id,revision FROM sources WHERE project='selected'").fetchone()
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'core_frozen'):
                db.execute("UPDATE OR REPLACE sources SET id=?,revision=? WHERE project='other'", target)

    def test_damaged_trigger_cannot_report_successful_retry(self):
        frozen = self.freeze()
        with closing(sqlite3.connect(self.paths['asset'])) as db:
            db.execute('DROP TRIGGER nexus_freeze_grants_insert')
            db.commit()
        with self.assertRaisesRegex(InsufficientContext, 'trigger'):
            freeze_core(self.task, frozen['source_digest'])

    def test_frozen_source_export_and_independent_restore_preserve_audit_and_bytes(self):
        self.freeze()
        export = self.root / 'export'
        restored = self.root / 'restored'
        export_core(self.task.projects, self.task.contexts, self.task.artifacts, 'selected', 'owner', export,
                    approvals=self.task.adapter.approvals)
        projects, contexts, artifacts = restore_core(export, restored, 'owner')
        self.assertEqual(projects.export('selected', 'owner'), self.task.projects.export('selected', 'owner'))
        self.assertEqual(contexts.export('selected', 'owner'), self.task.contexts.export('selected', 'owner'))
        self.assertEqual(ApprovalLedger(restored / 'approvals.sqlite3').export('owner'),
                         self.task.adapter.approvals.export_project('owner', 'selected')['approvals'])
        self.assertFalse(archived(projects.database, 'selected'))
        # Restoring bytes is not automatically a production canonical cutover.
        resumed = Task(restored, 'selected', 'owner', owner_root=self.root / 'restored-owner', surface=SyntheticSurface())
        resumed.publish_output(self.file)


if __name__ == '__main__':
    unittest.main()
