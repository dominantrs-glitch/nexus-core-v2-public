import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nexus.intake import Intake
from nexus.git_intake import GitIntake, open_intake, RoutedIntake, register_route, register_default_route
from nexus.git_migration import snapshot,digest
from nexus.intake_freeze import freeze,thaw


class GitIntakeClientTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        telemetry = patch('nexus.efficiency.default_database', return_value=self.root/'metrics.sqlite3')
        telemetry.start()
        self.addCleanup(telemetry.stop)

    def test_explicit_route_never_initializes_or_falls_back_to_local_database(self):
        (self.root/'canonical-git.json').write_text('{}',encoding='utf-8')
        store=open_intake(self.root)
        self.assertIsInstance(store,GitIntake)
        with patch('nexus.git_intake.shutil.which',return_value=None):
            with self.assertRaisesRegex(ValueError,'no local fallback'):store.list()
        self.assertFalse((self.root/'intake.sqlite3').exists())
        (self.root/'canonical-git.json').unlink()
        self.assertIsInstance(open_intake(self.root),Intake)

    def test_local_only_creation_and_configuration_are_not_silently_uploaded(self):
        calls=[]
        store=GitIntake(self.root,lambda *args:calls.append(args))
        with self.assertRaisesRegex(ValueError,'local-only'):store.create('private','source','key')
        with self.assertRaisesRegex(ValueError,'administrator'):store.set_remote('p-1',True)
        with self.assertRaisesRegex(ValueError,'original folder'):store.folder('p-1')
        self.assertEqual(calls,[])

    def test_export_preserves_snapshot_and_refuses_generation_change(self):
        calls=[]
        def read(operation,args):
            calls.append((operation,args))
            return dict(project='p-1',title='synthetic',revision=2,snapshot='a'*40,generation='g-1',
                        notes=[dict(body='second' if args['offset'] else 'first')],
                        next_offset=None if args['offset'] else 10)
        store=GitIntake(self.root,read)
        result=store.export('p-1')
        self.assertEqual(json.loads(Path(result['path']).read_text('utf-8'))['notes'],
                         [dict(body='first'),dict(body='second')])
        self.assertEqual(calls[1][1]['snapshot'],'a'*40)
        self.assertEqual(result['generation'],'g-1')
        def changed(operation,args):
            page=read(operation,args)
            if args['offset']:page['generation']='g-2'
            return page
        with self.assertRaisesRegex(ValueError,'changed'):GitIntake(self.root,changed).export('p-1')

    def test_export_includes_source_documents_from_later_pages(self):
        historical='git:ai-workspace@'+'a'*40+':projects/late-source.md'
        def read(operation,args):
            later=bool(args['offset'])
            return dict(project='p-1',title='synthetic',revision=11,snapshot='a'*40,generation='g-1',
                        notes=[dict(source=historical if later else 'synthetic',body='late' if later else 'first')],
                        source_documents=[],next_offset=None if later else 10)
        exported=GitIntake(self.root,read).export('p-1')
        data=json.loads(Path(exported['path']).read_text('utf-8'))
        self.assertEqual([item['path'] for item in data['source_documents']],['projects/late-source.md'])

    def test_export_keeps_later_page_withdrawal_restore_handles_without_the_old_body(self):
        def read(operation,args):
            later=bool(args['offset'])
            return dict(project='p-1',title='synthetic',revision=12,snapshot='a'*40,generation='g-1',
                        notes=[],withdrawn_notes=[dict(note='n-withdrawn',original_note='n-old',status='withdrawn')] if later else [],
                        next_offset=None if later else 10)
        result=GitIntake(self.root,read).export('p-1')
        exported=json.loads(Path(result['path']).read_text('utf-8'))
        self.assertEqual(exported['notes'],[])
        self.assertEqual(exported['withdrawn_notes'][0]['note'],'n-withdrawn')

    def test_retry_key_and_attribution_are_passed_unchanged(self):
        calls=[]
        store=GitIntake(self.root,lambda op,args:calls.append((op,args)) or dict(status='saved-draft'))
        args=dict(project='p-1',kind='constraint',body='quoted choice',source='synthetic',
                  evidence='user_statement',quote='exact quote',expected_revision=1,request_id='same-retry')
        store.save(**args);store.save(**args)
        self.assertEqual(calls[0],calls[1])
        self.assertEqual(calls[0][1],args|{'supersedes':None})
        store.read('p-1',snapshot='a'*40,offset=10,operation='review',mode='independent')
        self.assertEqual(calls[-1][1]['mode'],'independent')
        store.read('p-1',detail='overview')
        self.assertEqual(calls[-1][1]['detail'],'overview')
        store.read('p-1',detail='changes',since_revision=2,known_snapshot='a'*40,known_context_digest='b'*64)
        self.assertEqual(calls[-1][1]['since_revision'],2)
        self.assertEqual(calls[-1][1]['known_snapshot'],'a'*40)
        self.assertEqual(calls[-1][1]['known_context_digest'],'b'*64)
        store.search('query',project='p-1',kinds=['goal'],cursor='cursor',snapshot='a'*40)
        self.assertEqual(calls[-1],('search',dict(query='query',project='p-1',kinds=['goal'],cursor='cursor',snapshot='a'*40)))

    def test_native_handoff_stays_unconfirmed_and_blocks_missing_required_context(self):
        complete=True
        def read(operation,args):
            return dict(project='p-1',title='synthetic',revision=2,snapshot='a'*40,generation='g-1',
                context=dict(complete=complete),next_offset=None,
                notes=[dict(kind='goal',body='fictional goal',quote='',evidence='model_inference'),
                       dict(kind='acceptance',body='fictional acceptance',quote='',evidence='model_inference')])
        store=GitIntake(self.root,read)
        result=store.prepare('p-1')
        self.assertFalse(result['confirmed'])
        self.assertTrue(Path(result['draft']).is_file())
        self.assertIn('native owner confirmation',result['next'])
        complete=False
        with self.assertRaisesRegex(ValueError,'required context'):store.prepare('p-1')

    def test_resume_conditions_cannot_substitute_for_required_plan_originals(self):
        calls=[]
        def read(operation,args):
            calls.append(args)
            return dict(project='p-1',title='synthetic',revision=2,snapshot='a'*40,generation='g-1',
                context=dict(complete=args['operation']=='resume'),next_offset=None,
                notes=[dict(kind='goal',body='goal',quote='',evidence='model_inference'),
                       dict(kind='acceptance',body='acceptance',quote='',evidence='model_inference')])
        store=GitIntake(self.root,read)
        self.assertTrue(store.read('p-1')['context']['complete'])
        with self.assertRaisesRegex(ValueError,'required context'):store.prepare('p-1')
        self.assertEqual(calls[-1]['operation'],'plan')
        self.assertFalse((self.root/'handoffs').exists())
        self.assertFalse((self.root/'exports').exists())

    def test_handoff_pages_keep_plan_and_stop_if_later_required_context_is_unavailable(self):
        calls=[];available=True
        def read(operation,args):
            calls.append(args)
            first=args['offset']==0
            return dict(project='p-1',title='synthetic',revision=2,snapshot='a'*40,generation='g-1',
                context=dict(complete=first or available,operation=args['operation']),next_offset=10 if first else None,
                notes=[dict(kind='goal' if first else 'acceptance',body='fictional',quote='',evidence='model_inference')])
        store=GitIntake(self.root,read)
        result=store.prepare('p-1')
        self.assertFalse(result['confirmed'])
        self.assertEqual([c['operation'] for c in calls],['plan','plan'])
        self.assertEqual(calls[1]['snapshot'],'a'*40)
        handoff=json.loads(Path(result['handoff']['path']).read_text('utf-8'))
        self.assertEqual(handoff['context']['operation'],'plan')
        available=False
        with self.assertRaisesRegex(ValueError,'required context'):store.prepare('p-1')

    def test_selected_cutover_routes_old_entry_and_never_thaws_or_returns_stale_fallback(self):
        local=Intake(self.root/'local')
        p=local.create('migrate','synthetic','old-create',remote=True)['project']
        other=local.create('keep local','synthetic','other',remote=True)['project']
        args=dict(project=p,kind='proposal',body='old source',source='synthetic',evidence='model_inference',
                  quote='',expected_revision=0,request_id='old-save')
        local.save(**args)
        generation=freeze(local,[p],digest(snapshot(local.root,[p])))['generation']
        cfg=self.root/'cloud';cfg.mkdir()
        (cfg/'canonical-git.json').write_text(json.dumps(dict(generation='g-1')),encoding='utf-8')
        view=dict(project=p,revision=1,generation='g-1',snapshot='a'*40,storage=dict(write_state='frozen'))
        with patch.object(GitIntake,'read',return_value=view):
            register_route(local,p,generation,cfg,'a'*40)
        with self.assertRaisesRegex(ValueError,'already routed'):thaw(local,generation)
        routed=open_intake(local.root)
        self.assertIsInstance(routed,RoutedIntake)
        calls=[]
        def invoke(self,op,payload):
            calls.append((op,payload))
            if op=='list':return dict(projects=[dict(id=p,title='migrate',revision=2),dict(id='p-not-authorized',title='exclude',revision=1)],
                                      next_offset=None,snapshot='b'*40,generation='g-1')
            return dict(project=p,revision=2,body='cloud latest')
        with patch.object(GitIntake,'_invoke',invoke):
            self.assertEqual(routed.read(p)['revision'],2)
            self.assertEqual(routed.save(**args)['revision'],2)
            self.assertEqual(routed.create('migrate','synthetic','old-create',remote=True)['revision'],2)
            listing=routed.list()
            self.assertEqual({row['id'] for row in listing['projects']},{p,other})
            self.assertEqual(listing['search'],dict(status='not_searched',scope='project_titles',total_matches=2))
            self.assertEqual(next(row['revision'] for row in listing['projects'] if row['id']==p),2)
            self.assertEqual(routed.read(other)['revision'],0)
        with patch.object(GitIntake,'_invoke',side_effect=ValueError('offline')):
            with self.assertRaisesRegex(ValueError,'offline'):routed.read(p)
            with self.assertRaisesRegex(ValueError,'offline'):routed.list()
        (cfg/'canonical-git.json').write_text(json.dumps(dict(generation='wrong')),encoding='utf8')
        with self.assertRaisesRegex(ValueError,'generation'):routed.read(p)

    def test_classified_handoff_keeps_context_and_refuses_stale_contract_draft(self):
        calls=[];changed=False
        def read(operation,args):
            calls.append(args)
            return dict(project='p-1',title='synthetic',revision=2,snapshot='a'*40,generation='g-1',
                context=dict(complete=True),currentness=dict(changed=changed),next_offset=None,
                notes=[dict(kind='goal',body='fictional goal',quote='',evidence='model_inference'),
                       dict(kind='acceptance',body='fictional acceptance',quote='',evidence='model_inference')])
        store=GitIntake(self.root,read)
        result=store.prepare('p-1',task_types=['coding'],decision_factors=['delegated_decision'])
        self.assertFalse(result['confirmed'])
        self.assertEqual(calls[-1]['task_types'],['coding'])
        self.assertEqual(calls[-1]['decision_factors'],['delegated_decision'])
        changed=True
        with self.assertRaisesRegex(ValueError,'project changed'):store.prepare('p-1')

    def test_reviewed_shared_create_never_accidentally_creates_a_local_duplicate(self):
        routed=RoutedIntake(self.root)
        with self.assertRaisesRegex(ValueError,'canonical Git root'):
            routed.create('shared project','test','key',remote=True,review={'digest':'synthetic'})
        self.assertEqual(routed.list()['projects'],[])

    def test_opt_in_default_route_keeps_phone_and_pc_new_projects_in_one_store(self):
        local=Intake(self.root/'local')
        existing=local.create('existing','test','existing',remote=True)['project']
        private=local.create('private','test','private',remote=False)['project']
        cfg=self.root/'cloud';cfg.mkdir()
        (cfg/'canonical-git.json').write_text(json.dumps(dict(generation='g-1')),encoding='utf-8')
        freeze(local,[existing],digest(snapshot(local.root,[existing])))
        with local.db() as db:
            db.execute('INSERT INTO intake_routes VALUES (?,?,?)',(existing,'g-1',str(cfg)))
        routed=RoutedIntake(local.root)
        with self.assertRaisesRegex(ValueError,'project unavailable'):routed.read('p-created-on-phone')
        with patch.object(GitIntake,'capabilities',return_value={'features':{'projects':{'creation_enabled':False}}}):
            with self.assertRaisesRegex(ValueError,'not enabled'):register_default_route(routed,cfg)
        with patch.object(GitIntake,'capabilities',return_value={'features':{'projects':{'creation_enabled':True}}}):
            register_default_route(routed,cfg)
        calls=[]
        def invoke(self,operation,args):
            calls.append((operation,args))
            if operation=='list':return dict(projects=[dict(id=existing,title='existing',revision=1),
                dict(id='p-created-on-phone',title='phone',revision=2)],snapshot='a'*40,generation='g-1',next_offset=None)
            if operation=='read' and args['project']=='p-denied':raise ValueError('project unavailable')
            return dict(project='p-created-on-phone',revision=2,status='saved-draft')
        with patch.object(GitIntake,'_invoke',invoke):
            self.assertEqual(routed.read('p-created-on-phone')['revision'],2)
            review={'digest':'synthetic','input':{}}
            args=dict(title='new',source='test',request_id='same-key',remote=True,review=review)
            self.assertEqual(routed.create(**args),routed.create(**args))
            self.assertEqual(calls[-1],calls[-2])
            self.assertEqual(calls[-1][1]['review'],review)
            self.assertEqual({p['id'] for p in routed.list()['projects']},{existing,private,'p-created-on-phone'})
            with self.assertRaisesRegex(ValueError,'unavailable'):routed.read('p-denied')
            with self.assertRaisesRegex(ValueError,'unavailable'):routed.read(private,remote=True)
        with local.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM projects').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM requests').fetchone()[0],2)
        with self.assertRaisesRegex(ValueError,'originals are not configured'):routed.folder('p-created-on-phone')
        private_new=routed.create('new private','test','new-private',remote=False)
        self.assertTrue(routed.folder(private_new['project']).is_dir())
        # The old pre-cutover client cannot split the canonical store.
        import sqlite3
        with self.assertRaisesRegex(sqlite3.IntegrityError,'canonical route'):
            local.create('stale process','test','old-process',remote=True)
        (cfg/'canonical-git.json').write_text(json.dumps(dict(generation='changed')),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'no local fallback'):routed.create(**args)
        with patch.object(GitIntake,'_invoke',side_effect=ValueError('offline')):
            with self.assertRaisesRegex(ValueError,'no local fallback'):routed.read('p-created-on-phone')


if __name__=='__main__':unittest.main()
