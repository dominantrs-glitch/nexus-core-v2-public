import base64
import json
from pathlib import Path
import tempfile
import unittest
from nexus.artifacts import InsufficientContext
from nexus.context_store import ContextStore
from nexus.core_migration import prepare_core,freeze_prepared_core
from nexus.git_core import GitCore
from nexus.git_learning import stage_shared_candidate,withdraw_shared_candidate
from nexus.task import Task
from tests.test_git_core import MemoryCore,SyntheticSurface


class GitLearningTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        task=Task(self.root/'source','source','owner',owner_root=self.root/'owner',surface=SyntheticSurface())
        task.start(dict(goal='Synthetic source',acceptance={'file':['machine','contract']},constraints=['Synthetic only'],source='fixture'))
        self.file=self.root/'file.txt';self.file.write_text('incorrect')
        task.publish_output(self.file)
        for kind in ('machine','contract'):task.verify('file',kind,'fail','synthetic failure')
        task.correct('file','Synthetic correction','fixture');self.file.write_text('corrected');task.publish_output(self.file)
        for kind in ('machine','contract'):task.verify('file',kind,'pass','synthetic repaired case')
        prepare_core(task,self.root/'package');freeze_prepared_core(task,self.root/'package')
        self.private=ContextStore(self.root/'private-learning.sqlite3')
        self.remote=MemoryCore();self.store=GitCore(self.root/'route','owner','test-1',owner_root=self.root/'owner',
            surface=SyntheticSurface(),call=self.remote,route_name='native-source',learning_store=self.private)
        self.store.import_prepared(self.root/'package','import')

    def stage(self,session,**changes):
        args=dict(principle='Preserve a synthetic value.',rationale='Synthetic check.',destinations=frozenset({'target'}),required_constraints=['Synthetic only'])
        args.update(changes)
        return stage_shared_candidate(session,'shared-fixture',**args)

    def test_scoped_native_publication_and_withdrawal_keep_private_db_and_history(self):
        before=self.private.export('work-learning','owner')
        with self.store.open('source') as session:
            staged=self.stage(session)
            self.assertEqual(staged['status'],'staged-not-committed');self.assertEqual(self.remote.writes,1)
            session.commit('publish')
        self.assertEqual(self.private.export('work-learning','owner'),before)
        with self.store.open('source') as session:
            context=session.task.contexts.export('source','owner')
            record=next(r for r in context['contexts'] if r['id']==staged['id'])
            proof=next(s for s in context['sources'] if s['id']==record['source_id'])
            self.assertEqual(record['authority'],'candidate')
            self.assertEqual(json.loads(record['body'])['destinations'],['target'])
            self.assertNotIn(str(self.root),proof['body']);self.assertNotIn('Synthetic correction',proof['body'])
            self.assertEqual(withdraw_shared_candidate(session,staged['id'],1),2)
            session.commit('withdraw')
        with self.store.open('source') as session:
            rows=[r for r in session.task.contexts.export('source','owner')['contexts'] if r['id']==staged['id']]
            self.assertEqual([r['status'] for r in rows],['current','suppressed'])

    def test_unsaved_or_regressed_work_cannot_be_published(self):
        with self.store.open('source') as session:
            session.task.verify('file','machine','fail','synthetic new failure')
            with self.assertRaises(InsufficientContext):self.stage(session)
        self.assertEqual(self.remote.writes,1)

    def test_unbounded_destinations_or_implicit_broadening_are_rejected(self):
        with self.store.open('source') as session:
            for destinations in (frozenset(),frozenset({'*'}),{'target'}):
                with self.assertRaises(ValueError):self.stage(session,destinations=destinations)
            with self.assertRaises(ValueError):self.stage(session,reuse_scope='general')
        self.assertEqual(self.remote.writes,1)


if __name__=='__main__':unittest.main()
