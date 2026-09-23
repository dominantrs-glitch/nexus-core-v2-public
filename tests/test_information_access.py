import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from nexus.intake import Intake
from nexus.information_access import inventory


class InformationAccessTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Synthetic')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'core.autocrlf', 'false')
        self.path = self.repo / 'brain/projects/example.md'
        self.path.parent.mkdir(parents=True)
        self.path.write_text('---\nproject: example\ntitle: Example\nstatus: ACTIVE\n---\nSensitive body\n', encoding='utf-8')
        self.commit()
        self.baseline = self.git('rev-parse', 'HEAD')
        self.store = Intake(self.base / 'intake')
        self.source = f'git:ai-workspace@{self.baseline}:brain/projects/example.md'
        self.p = self.store.create('Example', self.source, 'create')['project']
        self.store.save(self.p, 'source', 'Sensitive body', self.source, 'external_source', '', 0, 'import')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.PIPE).decode().strip()

    def commit(self):
        self.git('add', '.')
        self.git('commit', '-qm', 'Synthetic record')

    def test_metadata_only_read_does_not_mutate_or_claim_phone_access(self):
        before = (self.store.root / 'intake.sqlite3').read_bytes()
        report = inventory(self.store.root, self.repo, 'HEAD')
        p = report['projects'][0]
        self.assertEqual(p['legacy']['comparison'], 'unchanged')
        self.assertEqual(p['priority_basis'], 'legacy_active')
        self.assertEqual(p['local_nonlegacy_note_count'], 0)
        self.assertIn('not_in_git_common_entry', p['gaps'])
        self.assertNotIn('Sensitive body', json.dumps(report))
        self.assertEqual(before, (self.store.root / 'intake.sqlite3').read_bytes())

    def test_changed_remote_dirty_worktree_and_local_updates_are_distinct(self):
        self.path.write_text(self.path.read_text() + 'committed change\n')
        self.commit()
        self.path.write_text(self.path.read_text() + 'uncommitted change\n')
        self.store.save(self.p, 'proposal', 'Later idea', 'current conversation', 'model_inference', '', 1, 'new')
        p = inventory(self.store.root, self.repo, 'HEAD')['projects'][0]
        self.assertEqual(p['legacy']['comparison'], 'changed_since_import')
        self.assertTrue(p['legacy']['worktree_modified'])
        self.assertEqual(p['local_nonlegacy_note_count'], 1)
        self.assertIn('local_post_import_records', p['gaps'])
        self.assertEqual(p['legacy']['blob'], self.git('rev-parse', 'HEAD:brain/projects/example.md'))

    def test_generated_overview_alone_is_not_a_substantive_local_update(self):
        self.store.save(self.p, 'proposal', '【画面用の概要】\nSummary', 'summary source', 'model_inference', '', 1, 'summary')
        p = inventory(self.store.root, self.repo, 'HEAD')['projects'][0]
        self.assertEqual(p['local_nonlegacy_note_count'], 0)
        self.assertNotIn('local_post_import_records', p['gaps'])

    def test_routed_archive_is_never_presented_as_current(self):
        with self.store.db() as db:
            db.execute('INSERT INTO intake_fences(project,generation,snapshot_sha256) VALUES (?,?,?)', (self.p, 'fixture', 'hash'))
            db.execute('INSERT INTO intake_routes VALUES (?,?,?)', (self.p, 'fixture', 'unused'))
        p = inventory(self.store.root, self.repo, 'HEAD')['projects'][0]
        self.assertEqual(p['canonical'], 'git_route')
        self.assertIsNone(p['revision'])
        self.assertIsNone(p['local_nonlegacy_note_count'])
        self.assertEqual(p['cloud_status'], 'registered_not_live_verified')

    def test_unfinished_new_project_and_unmapped_legacy_not_dropped(self):
        new = self.store.create('New unfinished project', 'conversation', 'new')['project']
        other = self.repo / 'brain/projects/other.md'
        other.write_text('---\nproject: other\nstatus: WAITING\n---\n')
        self.commit()
        report = inventory(self.store.root, self.repo, 'HEAD', [new])
        self.assertEqual(report['projects'][0]['project'], new)
        self.assertIn('no_legacy_project_mapping', report['projects'][0]['gaps'])
        self.assertEqual(report['unmatched_legacy_projects'][0]['path'], 'brain/projects/other.md')
        with self.assertRaisesRegex(ValueError, 'selection unavailable'):
            inventory(self.store.root, self.repo, 'HEAD', ['missing'])

    def test_missing_source_is_explicit_and_missing_db_not_created(self):
        self.path.unlink()
        self.commit()
        p = inventory(self.store.root, self.repo, 'HEAD')['projects'][0]
        self.assertEqual(p['legacy']['comparison'], 'missing')
        with self.assertRaises(FileNotFoundError):
            inventory(self.base / 'absent', self.repo, 'HEAD')
        self.assertFalse((self.base / 'absent').exists())


if __name__ == '__main__':
    unittest.main()
