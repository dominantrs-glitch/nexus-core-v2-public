import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from nexus.intake import Intake
from nexus.git_migration import snapshot, inspect, stage, digest


class GitMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.store = Intake(self.base/'intake')
        self.p = self.store.create('架空案件','synthetic source','c',remote=True)['project']
        self.args = dict(project=self.p,kind='constraint',body='無料',source='synthetic',evidence='user_statement',
                         quote='無料',expected_revision=0,request_id='s')

    def test_readonly_classification_and_history_keep_attribution(self):
        first = self.store.save(**self.args)
        self.store.save(**(self.args | dict(kind='correction',body='訂正',request_id='s2',expected_revision=1,supersedes=first['note'])))
        original = (self.store.root/'intake.sqlite3').read_bytes()
        selected = snapshot(self.store.root,[self.p])
        report = inspect(selected)
        self.assertEqual(report['blockers'],[])
        self.assertFalse(report['migration_ready'])
        self.assertEqual([n['lifecycle'] for n in report['projects'][0]['notes']],['historical','current'])
        self.assertEqual(report['projects'][0]['notes'][1]['kind'],'constraint')
        self.assertNotIn('無料',json.dumps(report,ensure_ascii=False))
        self.assertEqual((self.store.root/'intake.sqlite3').read_bytes(),original)

    def test_stage_preserves_ids_provenance_overview_and_private_scope(self):
        self.store.save(**(self.args | dict(kind='proposal',evidence='model_inference',quote='',body='【画面用の概要】\n目的')))
        self.store.save(**(self.args | dict(expected_revision=1,request_id='new')))
        self.store.set_remote(self.p,False)
        selected = snapshot(self.store.root,[self.p])
        target = self.base/'staged'
        stage(selected,'owner',target)
        catalog = json.loads((target/'nexus.json').read_text('utf-8'))
        self.assertFalse(catalog['projects'][0]['remote'])
        for note in selected[0]['notes']:
            saved = json.loads((target/'projects'/self.p/'records'/(note['id']+'.json')).read_text('utf-8'))
            self.assertEqual({k:saved[k] for k in note},note)
        self.assertFalse(inspect(selected)['projects'][0]['overview']['current'])
        with self.assertRaisesRegex(ValueError,'must not exist'):
            stage(selected,'owner',target)

    def test_missing_db_never_creates_one_and_broken_chains_block_staging(self):
        with self.assertRaises(FileNotFoundError): snapshot(self.base/'missing',[self.p])
        self.assertFalse((self.base/'missing').exists())
        self.store.save(**self.args)
        selected = snapshot(self.store.root,[self.p])
        selected[0]['notes'][0]['supersedes']='n-missing'
        self.assertTrue(inspect(selected)['blockers'])
        with self.assertRaisesRegex(ValueError,'blockers'): stage(selected,'owner',self.base/'bad')
        self.assertFalse((self.base/'bad').exists())

    def test_legacy_model_replacement_of_user_goal_blocks_migration(self):
        first = self.store.save(**(self.args | dict(kind='goal')))
        self.store.save(**(self.args | dict(kind='correction', body='changed goal',
            expected_revision=1, request_id='change', supersedes=first['note'])))
        selected = snapshot(self.store.root, [self.p])
        # Simulate legacy data from before live-write attribution protection.
        selected[0]['notes'][1].update(evidence='model_inference', quote='')
        report = inspect(selected)
        self.assertTrue(any(b['issue'] == 'authority_change' for b in report['blockers']))
        with self.assertRaisesRegex(ValueError, 'blockers'):
            stage(selected, 'owner', self.base / 'rejected')

    def test_possible_secret_and_duplicate_content_are_not_silently_migrated_or_merged(self):
        self.store.save(**self.args)
        self.store.save(**(self.args | dict(expected_revision=1,request_id='duplicate')))
        report = inspect(snapshot(self.store.root,[self.p]))
        self.assertEqual(len(report['projects'][0]['duplicate_content_groups']),1)
        self.assertEqual(report['projects'][0]['current_count'],2)
        self.store.save(**(self.args | dict(expected_revision=2,request_id='secret',body='-----BEGIN RSA PRIVATE KEY-----')))
        report = inspect(snapshot(self.store.root,[self.p]))
        self.assertTrue(any(b['issue']=='possible_secret_review_required' for b in report['blockers']))

    def test_real_git_commit_and_independent_clone_preserve_every_staged_byte(self):
        self.store.save(**self.args)
        selected = snapshot(self.store.root,[self.p])
        root = self.base/'stage'
        stage(selected,'owner',root)
        def git(*args, cwd=root):
            return subprocess.run(['git',*args],cwd=cwd,check=True,capture_output=True,text=True).stdout.strip()
        git('init','-q')
        git('add','.')
        git('-c','user.name=Synthetic Test','-c','user.email=test@example.invalid','-c','core.autocrlf=false','commit','-qm','Synthetic staging')
        restored = self.base/'independent'
        git('clone','--no-local','-q',str(root),str(restored))
        for path in root.rglob('*.json'):
            self.assertEqual(path.read_bytes(),(restored/path.relative_to(root)).read_bytes())
        review = json.loads((restored/'MIGRATION-REVIEW.json').read_text('utf-8'))
        self.assertEqual(review['report']['source_snapshot_sha256'],digest(selected))
        self.assertFalse(review['production_cutover_allowed'])


if __name__ == '__main__': unittest.main()
