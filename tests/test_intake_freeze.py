import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from nexus.intake import Intake
from nexus.intake_freeze import freeze, frozen_snapshot, thaw, backup, restore
from nexus.git_migration import snapshot, digest, inspect, stage, receipt_path


class IntakeFreezeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Intake(self.root/'source')
        self.p = self.store.create('架空の移行', 'synthetic', 'create', remote=True)['project']
        self.args = dict(project=self.p, kind='proposal', body='風船', source='synthetic',
            evidence='model_inference', quote='', expected_revision=0, request_id='save')
        self.saved = self.store.save(**self.args)

    def fence(self):
        self.selected = snapshot(self.store.root, [self.p])
        return freeze(self.store, [self.p], digest(self.selected))['generation']

    def test_fence_blocks_loaded_clients_and_old_sql_but_other_projects_work(self):
        old = sqlite3.connect(self.store.root/'intake.sqlite3')
        self.addCleanup(old.close)
        generation = self.fence()
        for run in (
                lambda: self.store.save(**self.args),
                lambda: self.store.create('架空の移行','synthetic','create',remote=True),
                lambda: self.store.set_remote(self.p, False)):
            with self.assertRaisesRegex(ValueError, 'frozen'): run()
        for sql, values in (
                ('UPDATE projects SET revision=revision+1 WHERE id=?', (self.p,)),
                ('UPDATE notes SET body=? WHERE project=?', ('changed',self.p)),
                ('DELETE FROM notes WHERE project=?', (self.p,)),
                ('DELETE FROM requests WHERE key=?', ('save',)),
                ('INSERT INTO requests VALUES (?,?,?)', ('new','x',json.dumps(self.saved)))):
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'frozen'):
                old.execute(sql, values)
            old.rollback()
        other = self.store.create('Other','synthetic','other')['project']
        self.store.save(**(self.args | dict(project=other,request_id='other-save')))
        self.assertEqual(frozen_snapshot(self.store,generation),self.selected)
        thaw(self.store,generation)
        self.assertEqual(self.store.save(**self.args),self.saved)

    def test_changed_snapshot_and_generation_never_freeze_or_thaw_wrong_data(self):
        stale = digest(snapshot(self.store.root,[self.p]))
        self.store.save(**(self.args | dict(expected_revision=1,request_id='new')))
        with self.assertRaisesRegex(ValueError,'changed'):
            freeze(self.store,[self.p],stale)
        generation = self.fence()
        with self.assertRaisesRegex(ValueError,'unavailable'): thaw(self.store,'freeze-wrong')
        self.assertEqual(frozen_snapshot(self.store,generation),self.selected)

    def test_inflight_writer_finishes_before_freeze_and_forces_new_review(self):
        stale = digest(snapshot(self.store.root,[self.p]))
        results, entered = [], threading.Event()
        def try_freeze():
            entered.set()
            try: freeze(self.store,[self.p],stale)
            except ValueError as error: results.append(str(error))
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE projects SET title=? WHERE id=?',('Changed during review',self.p))
            worker = threading.Thread(target=try_freeze)
            worker.start()
            self.assertTrue(entered.wait(2))
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results,['snapshot changed; review again before freezing'])

    def test_scoped_backup_restore_preserves_every_record_and_receipt_but_stays_frozen(self):
        other = self.store.create('Excluded private project','private','excluded')['project']
        generation = self.fence()
        path = self.root/'backup.json'
        backup(self.store,generation,path)
        self.assertNotIn(other,path.read_text('utf-8'))
        result = restore(path,self.root/'restored')
        self.assertEqual(result['state'],'frozen')
        restored = Intake(self.root/'restored')
        self.assertEqual(frozen_snapshot(restored,generation),self.selected)
        with self.assertRaisesRegex(ValueError,'frozen'): restored.save(**self.args)
        with self.assertRaises(FileExistsError): restore(path,self.root/'restored')
        payload = json.loads(path.read_text('utf-8'))
        payload['projects'][0]['notes'][0]['body']='tampered'
        path.write_text(json.dumps(payload),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'integrity'): restore(path,self.root/'tampered')
        self.assertFalse((self.root/'tampered').exists())

    def test_receipts_are_verified_not_invented_and_staged_without_raw_keys(self):
        generation = self.fence()
        selected = frozen_snapshot(self.store,generation)
        stage(selected,'owner',self.root/'stage')
        receipt = json.loads((self.root/'stage'/receipt_path('owner','save')).read_text('utf-8'))
        self.assertEqual(receipt['result'],self.saved)
        self.assertEqual(receipt['input']['expected_revision'],0)
        self.assertEqual(receipt['input']['supersedes'],None)
        self.assertNotIn('key',receipt)
        selected[0]['requests'][0]['digest']='0'*64
        self.assertTrue(any(b['issue']=='invalid_request_receipt' for b in inspect(selected)['blockers']))
        with self.assertRaisesRegex(ValueError,'blockers'): stage(selected,'owner',self.root/'invalid')
        selected = snapshot(self.store.root,[self.p])
        selected[0]['requests']=[]
        self.assertTrue(any(b['issue']=='missing_request_receipt' for b in inspect(selected)['blockers']))

    def test_multiple_projects_have_stable_freeze_order(self):
        other = self.store.create('Other','synthetic','other')['project']
        projects = [self.p,other]
        selected = snapshot(self.store.root,projects)
        generation = freeze(self.store,list(reversed(projects)),digest(selected))['generation']
        self.assertEqual(frozen_snapshot(self.store,generation),selected)
        thaw(self.store,generation)


if __name__ == '__main__': unittest.main()
