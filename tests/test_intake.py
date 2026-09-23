import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import httpx2
from nexus.connector import local_response
from nexus.intake import Intake
from nexus.intake_server import build_intake_server
from nexus.migrate_v1 import migrate


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.w=Intake(Path(self.temp.name)/'store')
        self.p=self.w.create('試験案件','synthetic conversation','create')['project']

    def save(self,**kwargs):
        args=dict(project=self.p,kind='constraint',body='無料のみ',source='synthetic user turn',
            evidence='user_statement',quote='無料でお願いします',expected_revision=0,request_id='save')
        args.update(kwargs);return self.w.save(**args)

    def test_saved_restart_retry_conflict_and_no_authority_promotion(self):
        first=self.save();self.assertEqual(first,self.save())
        w=Intake(self.w.root); view=w.read(self.p)
        self.assertEqual(view['revision'],1);self.assertFalse(view['binding'])
        self.assertEqual(view['notes'][0]['quote'],'無料でお願いします')
        with self.assertRaisesRegex(ValueError,'reused'): self.save(body='有料可')
        with self.assertRaisesRegex(ValueError,'revision'): self.save(request_id='different')
        self.assertEqual(w.read(self.p)['revision'],1)
        result=w.export(self.p);self.assertTrue(Path(result['path']).is_file())
        self.assertEqual(result,w.export(self.p))

    def test_overview_only_is_status_not_complete_context_and_hides_stale_summary(self):
        self.assertEqual(self.w.read(self.p,detail='overview')['overview_status'],'missing')
        self.save(kind='proposal',evidence='model_inference',quote='',body='【画面用の概要】\n現在地')
        v=self.w.read(self.p,detail='overview')
        self.assertEqual(v['overview']['text'],'現在地');self.assertEqual(v['notes'],[])
        self.assertTrue(v['notes_omitted']);self.assertFalse(v['context']['complete'])
        with self.assertRaises(ValueError):self.w.read(self.p,detail='overview',operation='implement')
        with self.assertRaises(ValueError):self.w.read(self.p,detail='overview',offset=10)
        self.save(expected_revision=1,request_id='later')
        self.assertIsNone(self.w.read(self.p,detail='overview')['overview'])
        self.assertEqual(len(self.w.read(self.p)['notes']),2)

    def test_remote_cannot_discover_read_edit_local_project(self):
        self.assertEqual(self.w.list(remote=True)['projects'],[])
        with self.assertRaisesRegex(ValueError,'unavailable'): self.w.read(self.p,remote=True)
        with self.assertRaisesRegex(ValueError,'unavailable'): self.save(remote=True)
        self.w.set_remote(self.p,True);self.save(remote=True)
        self.w.set_remote(self.p,False)
        with self.assertRaisesRegex(ValueError,'unavailable'): self.save(remote=True)

    def test_title_search_distinguishes_no_match_from_empty_page_and_unavailable(self):
        self.assertEqual(self.w.list('試験',remote=True)['search'],
                         dict(status='no_match',scope='project_titles',total_matches=0))
        self.w.set_remote(self.p,True)
        found=self.w.list('試験',remote=True)
        self.assertEqual(found['search']['status'],'matches')
        self.assertEqual(found['search']['total_matches'],1)
        self.assertEqual(self.w.list('試験',remote=True,offset=25)['search']['status'],'matches')
        self.assertEqual(self.w.list('試験',remote=True,offset=25)['projects'],[])
        self.assertEqual(self.w.list('  ',remote=True)['search']['status'],'not_searched')
        with self.assertRaises(ValueError):self.w.list('試験',remote=True,snapshot='0'*40)

    def test_corrections_keep_history_and_cannot_cross_projects(self):
        first=self.save()
        other=self.w.create('other','synthetic','other')['project']
        with self.assertRaisesRegex(ValueError,'target'): self.save(project=other,supersedes=first['note'],request_id='bad')
        self.save(body='新しい制限',quote='無料範囲ならよい',expected_revision=1,request_id='correct',supersedes=first['note'])
        self.assertEqual([n['body'] for n in self.w.read(self.p)['notes']],['新しい制限'])
        with self.w.db() as db: self.assertEqual(db.execute('SELECT COUNT(*) FROM notes').fetchone()[0],2)

    def test_claim_validation_and_path_bounds(self):
        with self.assertRaisesRegex(ValueError,'proposal'): self.save(evidence='model_inference',quote='')
        with self.assertRaises(ValueError): self.save(quote='')
        with self.assertRaises(ValueError): self.w.folder('../outside')
        with self.assertRaises(ValueError): self.w.folder('con')
        with self.assertRaises(ValueError): self.save(body='あ'*8000)

    def test_generic_correction_retains_acceptance_category_and_authority(self):
        self.save(kind='goal')
        a=self.save(kind='acceptance',body='3 groups',quote='3 groups',expected_revision=1,request_id='a')
        self.save(kind='correction',body='4 groups',quote='4 groups',expected_revision=2,request_id='b',supersedes=a['note'])
        note=self.w.read(self.p)['notes'][-1]
        self.assertEqual(note['kind'],'acceptance');self.assertEqual(note['captured_kind'],'correction')
        self.assertIn('4 groups',Path(self.w.prepare(self.p)['draft']).read_text(encoding='utf-8'))
        c=self.save(kind='constraint',expected_revision=3,request_id='c')
        with self.assertRaisesRegex(ValueError,'cannot replace'):
            self.save(kind='correction',evidence='model_inference',quote='',supersedes=c['note'],expected_revision=4,request_id='d')

    def test_model_cannot_supersede_user_goals_acceptance_or_attributed_sources(self):
        for kind in ('goal', 'acceptance', 'source'):
            with self.subTest(kind=kind):
                project=self.w.create(kind,'synthetic',f'create-{kind}')['project']
                first=self.save(project=project,kind=kind,request_id=f'user-{kind}')
                corrected=self.save(project=project,kind='correction',supersedes=first['note'],
                    expected_revision=1,request_id=f'user-correction-{kind}')
                for evidence in ('model_inference', 'external_source'):
                    with self.assertRaisesRegex(ValueError,'cannot replace'):
                        self.save(project=project,kind='correction',evidence=evidence,quote='',
                            supersedes=corrected['note'],expected_revision=2,request_id=f'bad-{kind}-{evidence}')
                view=self.w.read(project)
                self.assertEqual(view['revision'],2)
                self.assertEqual(view['notes'][0]['id'],corrected['note'])

    def test_model_can_revise_its_own_draft_goal_and_add_an_alternative(self):
        first=self.save(kind='goal',evidence='model_inference',quote='')
        self.save(kind='correction',evidence='model_inference',quote='',supersedes=first['note'],
            expected_revision=1,request_id='own-revision')
        self.save(kind='goal',expected_revision=2,request_id='user-goal')
        self.save(kind='proposal',evidence='model_inference',quote='',expected_revision=3,request_id='alternative')
        self.assertEqual(len(self.w.read(self.p)['notes']),3)

    def test_pagination_and_all_note_export(self):
        historical='git:ai-workspace@'+'a'*40+':projects/late-source.md'
        for i in range(12):
            self.save(expected_revision=i,request_id=str(i),
                      source=historical if i==11 else 'synthetic user turn')
        page=self.w.read(self.p); self.assertEqual(page['next_offset'],10)
        self.assertEqual(page['source_documents'],[])
        self.assertEqual(len(self.w.read(self.p,offset=10)['notes']),2)
        exported=json.loads(Path(self.w.export(self.p)['path']).read_text(encoding='utf-8'))
        self.assertEqual(len(exported['notes']),12)
        self.assertEqual([item['path'] for item in exported['source_documents']],['projects/late-source.md'])

    def test_prepare_preserves_user_choice_not_model_paraphrase(self):
        with self.assertRaisesRegex(ValueError,'goal'): self.w.prepare(self.p)
        self.save(kind='goal',body='model paraphrase',quote='私の手作業を減らす')
        self.save(kind='acceptance',body='model paraphrase',quote='会話から案件を保存できる',expected_revision=1,request_id='a')
        self.save(kind='explicit_choice',body='model paraphrase',quote='無料でPythonを使う',expected_revision=2,request_id='b')
        prepared=self.w.prepare(self.p)
        draft=json.loads(Path(prepared['draft']).read_text(encoding='utf-8'))
        self.assertIn('私の手作業を減らす',draft['goal'])
        self.assertIn('会話から案件を保存できる',draft['goal'])
        self.assertEqual(draft['constraints'],['無料でPythonを使う'])
        self.assertNotIn('model paraphrase',str(draft))
        self.assertFalse(prepared['confirmed'])

    def test_migration_preserves_source_bytes_and_private_default(self):
        repo=Path(self.temp.name)/'repo';repo.mkdir()
        def git(*args):return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.DEVNULL)
        git('init');git('config','user.email','test@example.invalid');git('config','user.name','fixture')
        record=repo/'brain/projects/example.md';record.parent.mkdir(parents=True)
        record.write_text('---\nproject: example\ntitle: Example\nstatus: ACTIVE\n---\nOriginal decision text.\n',encoding='utf-8')
        raw=repo/'projects/example/01_raw/data.bin';raw.parent.mkdir(parents=True);raw.write_bytes(bytes(range(256)))
        git('add','.');git('commit','-m','fixture')
        local_before=record.read_bytes(); before=git('show','HEAD:brain/projects/example.md'); report=migrate(repo,self.w,'HEAD')
        self.assertEqual(report['projects'],1);self.assertEqual(report['copied_files'],2)
        manifest=json.loads(Path(report['manifest']).read_text(encoding='utf-8'))
        snapshot=Path(report['manifest']).parent
        self.assertEqual((snapshot/'brain/projects/example.md').read_bytes(),before)
        self.assertEqual(record.read_bytes(),local_before)
        self.assertEqual((snapshot/'projects/example/01_raw/data.bin').read_bytes(),raw.read_bytes())
        self.assertEqual(self.w.list(remote=True)['projects'],[])
        self.assertEqual(report,migrate(repo,self.w,'HEAD'))
        imported=self.w.read(manifest['projects'][0]['project'])
        self.assertFalse(imported['binding'])


class IntakeHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_mcp_write_and_read_same_local_database(self):
        with tempfile.TemporaryDirectory() as root:
            w=Intake(root);server=build_intake_server(w)
            listing=await server.list_tools()
            self.assertEqual(len(listing),4)
            self.assertFalse(next(x for x in listing if x.name=='create_project').annotations.read_only_hint)
            app=server.streamable_http_app(json_response=True,stateless_http=True)
            async with app.router.lifespan_context(app):
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
                    async def call(name,args):
                        status,body=await local_response(client,dict(protocol='2025-11-25',body=json.dumps(dict(jsonrpc='2.0',id=1,method='tools/call',params=dict(name=name,arguments=args)))))
                        self.assertEqual(status,200)
                        return json.loads(body)['result']
                    created=await call('create_project',dict(title='Remote test',source='synthetic trial',request_id='remote-create'))
                    project=created['structuredContent']['project']
                    saved=await call('save_project_note',dict(project=project,kind='explicit_choice',body='Use Python',source='synthetic user',evidence='user_statement',quote='Use Python',expected_revision=0,request_id='remote-save'))
                    self.assertFalse(saved.get('isError',False))
                    read=await call('read_project',dict(project=project))
                    self.assertEqual(read['structuredContent']['notes'][0]['body'],'Use Python')
                    self.assertEqual(Intake(root).read(project)['revision'],1)
                    denied=await call('read_project',dict(project='../owner'))
                    self.assertTrue(denied['isError'])


if __name__=='__main__':unittest.main()
