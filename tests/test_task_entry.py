"""Synthetic native route/cutover tests; no real owner confirmations or network."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nexus.artifacts import InsufficientContext
from nexus.core_migration import prepare_core, freeze_prepared_core
from nexus.git_core import GitCore
from nexus.task import Task
from nexus.task_entry import TaskEntry, activate, route_path
from tests.test_git_core import MemoryCore, SyntheticSurface

class TaskEntryTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.surface=SyntheticSurface();self.owner=self.root/'owner'
        self.task=Task(self.root/'source','example','owner',owner_root=self.owner,surface=self.surface)
        self.task.start(dict(goal='Synthetic ordinary route',acceptance={'file':['machine','contract','uat']},constraints=['synthetic'],source='fictional'))
        self.file=self.root/'original';self.file.write_text('synthetic',encoding='utf-8');self.task.publish_output(self.file)
        self.package=self.root/'package';prepare_core(self.task,self.package)
        self.remote=MemoryCore();self.route=self.root/'route';self.route.mkdir()
        (self.route/'canonical-git.json').write_text(json.dumps(dict(native_owner='owner',generation='test-1')),encoding='utf-8')
        self.core=GitCore(self.route,'owner','test-1',owner_root=self.owner,surface=self.surface,call=self.remote,route_name='synthetic')
        self.entry=TaskEntry(self.task.root,'example','owner',owner_root=self.owner,surface=self.surface,call=self.remote)

    def activate(self):
        freeze_prepared_core(self.task,self.package);self.core.import_prepared(self.package,'import')
        return activate(self.task,self.core,self.package)

    def test_cutover_then_ordinary_read_write_preserves_frozen_original_and_other_project(self):
        before=self.task.snapshot();receipt=self.activate()
        self.assertEqual(activate(self.task,self.core,self.package),receipt)
        self.assertEqual(self.entry.snapshot(),before)
        result=self.entry.run('classify-work',dict(kinds=['writing'],source='synthetic'),request_id='classification')
        self.assertEqual(result['receipt']['revision'],2)
        self.assertEqual(self.entry.run('classify-work',dict(kinds=['writing'],source='synthetic'),request_id='classification'),result)
        self.assertEqual(self.remote.writes,2)
        self.assertEqual(self.task.projects.export('example','owner'),before)
        other=TaskEntry(self.task.root,'other','owner',owner_root=self.owner,surface=self.surface)
        other.run('start',dict(goal='Other',acceptance={'x':['machine']},constraints=[],source='synthetic'))
        self.assertEqual(other.read('implement')['contract']['goal'],'Other')
        self.assertEqual(self.entry.read('implement')['work_classification']['payload']['work_types'],['writing'])

    def test_no_activation_before_freeze_or_when_destination_is_unavailable(self):
        with self.assertRaises(FileNotFoundError): activate(self.task,self.core,self.package)
        self.assertFalse(route_path(self.task.root,'example','owner').exists())
        freeze_prepared_core(self.task,self.package)
        with self.assertRaisesRegex(ValueError,'unavailable'): activate(self.task,self.core,self.package)
        self.assertFalse(route_path(self.task.root,'example','owner').exists())
        with self.assertRaisesRegex(InsufficientContext,'archived'): self.entry.read('implement')

    def test_lost_reply_retry_does_not_repeat_confirmation_or_require_original_file(self):
        self.activate();self.remote.lose_reply=True
        payload=dict(acceptance='file',kind='uat',status='pass',source='synthetic',review_file=str(self.file))
        with self.assertRaisesRegex(ValueError,'response lost'): self.entry.run('verify',payload,request_id='uat-once')
        count=len(self.surface.requests);self.file.unlink()
        result=self.entry.retry('uat-once')
        self.assertEqual(result['receipt']['revision'],2);self.assertEqual(self.remote.writes,2)
        self.assertEqual(count,len(self.surface.requests))
        with self.assertRaisesRegex(ValueError,'different command'):
            self.entry.run('verify',{**payload,'status':'fail'},request_id='uat-once')

    def test_failed_or_changed_route_never_falls_back_to_old_source(self):
        self.activate();self.remote.unavailable=True
        with self.assertRaisesRegex(ValueError,'unavailable'): self.entry.read('implement')
        self.remote.unavailable=False;self.remote.state='frozen'
        with self.assertRaisesRegex(InsufficientContext,'frozen'): self.entry.read('implement')
        self.remote.state='active';config=self.route/'canonical-git.json'
        config.write_text(json.dumps(dict(native_owner='owner',generation='different')),encoding='utf-8')
        with self.assertRaisesRegex(InsufficientContext,'configuration changed'): self.entry.read('implement')
        route_path(self.task.root,'example','owner').unlink()
        with self.assertRaisesRegex(InsufficientContext,'archived'): self.entry.read('implement')

    def test_interruption_before_outbox_never_reexecutes_owner_confirmation(self):
        self.activate()
        payload=dict(acceptance='file',kind='uat',status='pass',source='synthetic',review_file=str(self.file))
        with patch('nexus.git_core.GitCoreSession.commit',side_effect=OSError('synthetic interrupted')):
            with self.assertRaises(OSError): self.entry.run('verify',payload,request_id='interrupted')
        count=len(self.surface.requests)
        with self.assertRaisesRegex(InsufficientContext,'before outbox'): self.entry.retry('interrupted')
        self.assertEqual(count,len(self.surface.requests));self.assertEqual(self.remote.writes,1)

    def test_changed_origin_or_missing_freeze_trigger_blocks_current_route(self):
        self.activate();origin=deepcopy(self.remote.document['origin']);self.remote.document['origin']['generation']='wrong'
        with self.assertRaisesRegex(InsufficientContext,'source changed'): self.entry.snapshot()
        self.remote.document['origin']=origin
        with self.task.projects._db() as db: db.execute('DROP TRIGGER nexus_freeze_events_insert')
        with self.assertRaisesRegex(InsufficientContext,'trigger missing'): self.entry.read('implement')

    def test_mutation_requires_request_before_confirmation_and_decline_never_commits(self):
        self.activate();count=len(self.surface.requests)
        payload=dict(acceptance='file',kind='uat',status='pass',source='synthetic',review_file=str(self.file))
        with self.assertRaisesRegex(ValueError,'request id'): self.entry.run('verify',payload)
        self.assertEqual(count,len(self.surface.requests))
        from nexus.owner import ApprovalDeclined
        self.surface.answer=False
        with self.assertRaises(ApprovalDeclined): self.entry.run('verify',payload,request_id='decline')
        self.assertEqual(self.remote.writes,1)
        with self.assertRaisesRegex(InsufficientContext,'before result'): self.entry.retry('decline')

    def test_previously_observed_revision_cannot_be_rolled_back(self):
        self.activate();old=deepcopy(self.remote.document)
        self.entry.run('classify-work',dict(kinds=['writing'],source='synthetic'),request_id='newer')
        self.remote.document=old
        with self.assertRaisesRegex(InsufficientContext,'rolled back'): self.entry.read('implement')

    def test_private_learning_writes_stay_local_and_replay_without_native_commit(self):
        from nexus.context_store import ContextStore
        self.task.correct('file','Keep the exact value','fixture')
        self.task.publish_output(self.file)
        for kind in ('machine','contract'):self.task.verify('file',kind,'pass','fixture')
        self.package=self.root/'repaired-package';prepare_core(self.task,self.package)
        self.activate()
        private=ContextStore(self.root/'private-learning.sqlite3')
        entry=TaskEntry(self.task.root,'example','owner',owner_root=self.owner,surface=self.surface,call=self.remote,learning_store=private)
        payload=dict(identity='synthetic-lesson',principle='Keep exact values.',rationale='Fixture repair.',projects=['example'],required_constraints=['synthetic'])
        result=entry.run('learn',payload,request_id='learn')
        self.assertEqual(result['storage'],'owner-local-learning');self.assertEqual(self.remote.writes,1)
        self.assertEqual(entry.run('learn',payload,request_id='learn'),result)
        self.assertEqual(len(entry.read('implement')['learning']['items']),1)
        entry.run('suppress-learning',dict(identity='synthetic-lesson',expected_revision=1),request_id='suppress')
        self.assertEqual(entry.read('implement')['learning']['items'],[]);self.assertEqual(self.remote.writes,1)

    def test_cli_uses_activated_entry_and_captured_output_bytes(self):
        import base64,io,os,sys
        from nexus.task import main
        self.activate()
        class Output(io.StringIO):
            def reconfigure(self,**kwargs):pass
        def cli(*arguments):
            stream=Output()
            with patch.object(sys,'argv',['nexus.task','--root',str(self.task.root),'--project','example',*arguments]),patch.object(sys,'stdout',stream),patch('nexus.task.getpass.getuser',return_value='owner'),patch.dict(os.environ,{'LOCALAPPDATA':str(self.root/'appdata')}),patch('nexus.git_intake.GitIntake._invoke',side_effect=self.remote):
                main()
            return json.loads(stream.getvalue())
        self.assertEqual(cli('read')['contract']['goal'],'Synthetic ordinary route')
        result=cli('--request-id','cli-output','output',str(self.file))
        self.assertEqual(result['receipt']['revision'],2)
        self.file.unlink()
        self.assertEqual(cli('retry','cli-output'),result)
        snapshot=cli('inspect');self.assertEqual(snapshot['events'][-1]['kind'],'output')

if __name__=='__main__':unittest.main()
