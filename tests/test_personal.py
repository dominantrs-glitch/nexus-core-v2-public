from pathlib import Path
import json
import tempfile
import unittest

from nexus.artifacts import AccessDenied, InsufficientContext
from nexus.context_store import ContextStore
from nexus.personal import HOME, PersonalContext


class LocalCapability:
    def authenticate(self, actor):
        if actor != 'owner':
            raise AccessDenied('unavailable')


class PersonalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ContextStore(Path(self.tmp.name) / 'context.sqlite3')
        self.personal = PersonalContext(self.store, LocalCapability(), 'owner')

    def capture(self, text='Prefer concise progress.', **kwargs):
        return self.personal.capture('preference', text=text, provenance='conversation:test:message:1',
            projects=frozenset({'project-a'}), operations=frozenset({'implement'}), **kwargs)

    def test_explicit_cross_project_scope_and_provenance(self):
        self.capture()
        item = self.personal.read('project-a', 'implement', required=frozenset({'preference'}))['items'][0]
        self.assertFalse(item['binding'])
        self.assertEqual(item['authority'], 'confirmed')
        evidence = self.store.export(HOME, 'owner')
        self.assertIn('conversation:test:message:1', evidence['sources'][0]['body'])
        self.assertEqual(self.personal.read('project-b', 'implement')['items'], [])
        self.assertEqual(self.personal.read('project-a', 'review')['items'], [])
        self.assertEqual(self.store.resolve('project-a', 'other', 'implement', required=frozenset()).items, ())

    def test_correction_replaces_current_but_preserves_history(self):
        self.capture()
        self.capture('Explain risk before a choice.', expected_revision=1)
        item = self.personal.read('project-a', 'implement')['items'][0]
        self.assertEqual(item['revision'], 2)
        self.assertEqual(item['text'], 'Explain risk before a choice.')
        self.assertEqual(len(self.store.export(HOME, 'owner')['contexts']), 2)
        with self.assertRaises(ValueError):
            self.capture('stale write', expected_revision=1)

    def test_narrowing_and_suppression_do_not_fall_back(self):
        self.capture()
        self.personal.capture('preference', text='Only project-b now.', provenance='correction:2',
            projects=frozenset({'project-b'}), operations=frozenset({'implement'}), expected_revision=1)
        with self.assertRaises(InsufficientContext):
            self.personal.read('project-a', 'implement', required=frozenset({'preference'}))
        self.personal.capture('preference', text='Withdrawn.', provenance='correction:3',
            projects=frozenset({'project-b'}), operations=frozenset({'implement'}), expected_revision=2, status='suppressed')
        self.assertEqual(self.personal.read('project-b', 'implement')['items'], [])

    def test_interpretation_cannot_be_silently_promoted(self):
        self.capture(interpretation=True)
        self.assertEqual(self.personal.read('project-a', 'implement')['items'][0]['authority'], 'candidate')
        with self.assertRaises(ValueError):
            self.capture(expected_revision=1)

    def test_capability_and_explicit_scope_required(self):
        with self.assertRaises(AccessDenied):
            PersonalContext(self.store, LocalCapability(), 'other').read('project-a', 'implement')
        with self.assertRaises(ValueError):
            self.personal.capture('x', text='x', provenance='test', projects=frozenset({'*'}), operations=frozenset({'implement'}))

    def test_local_archive_restores_history_and_cannot_overwrite(self):
        self.capture()
        self.capture('Corrected preference.', expected_revision=1)
        path = Path(self.tmp.name) / 'personal.json'
        self.personal.export_local(path)
        with self.assertRaises(FileExistsError): self.personal.export_local(path)
        restored = ContextStore(Path(self.tmp.name) / 'restored.sqlite3')
        restored.restore_export(json.loads(path.read_text(encoding='utf-8')), 'owner')
        self.assertEqual(restored.export(HOME, 'owner'), self.store.export(HOME, 'owner'))
        self.assertEqual(PersonalContext(restored, LocalCapability(), 'owner').read('project-a', 'implement')['items'][0]['text'], 'Corrected preference.')


if __name__ == '__main__':
    unittest.main()
