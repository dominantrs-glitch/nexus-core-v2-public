import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from nexus.artifacts import Access, AccessDenied, ArtifactStore, LocalBinaryProvider, InsufficientContext
from nexus.completion import CompletionGate
from nexus.context import ContextRecord
from nexus.project import ProjectStore


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'projects.db'
        self.artifacts = ArtifactStore(Path(self.tmp.name) / 'artifacts.db', LocalBinaryProvider(Path(self.tmp.name) / 'blobs'))
        self.records = (ContextRecord('decision', 1, 'decision', 'confirmed', 'current',
            frozenset({'p'}), frozenset({'finish'}), frozenset({'owner'}), 'fixture-source', 'synthetic'),)
        self.gate = CompletionGate('p', 1, 'owner', 'finish', frozenset({'decision'}),
            {'decision': 'confirmed'}, (), lambda: self.records, self.artifacts)
        self.store = ProjectStore(self.path, completion_check=self.gate)
        self.confirm()
        self.bind(1)

    def confirm(self, previous=0):
        return self.store.confirm_contract('p', 'owner', goal='Synthetic artifact',
            acceptance={'layout': ['machine', 'contract', 'uat']},
            constraints=['synthetic only'], source='owner-confirmation-fixture', expected_revision=previous)

    def bind(self, revision):
        self.artifacts.register('fixture', 'p', ('output-' + str(revision)).encode(), 'text/plain',
                                ('owner',), expected_revision=revision - 1)
        manifest = self.artifacts.describe('fixture', revision, Access('owner', 'p'))
        self.store.bind_output('p', 'owner', expected_revision=1,
            artifact_id='fixture', artifact_revision=revision, sha256=manifest['sha256'])
        self.output = self.store.export('p', 'owner')['events'][-1]['seq']

    def verify(self, kind='machine', status='pass', revision=1, output=None):
        return self.store.record_verification('p', 'owner', expected_revision=revision,
            output_event=self.output if output is None else output, acceptance='layout',
            kind=kind, status=status, source='fixture-evidence')

    def all_pass(self):
        for kind in ('machine', 'contract', 'uat'):
            self.verify(kind)

    def test_verification_separation_and_persistence(self):
        self.verify()
        with self.assertRaises(InsufficientContext): self.store.finish('p', 'owner')
        self.verify('contract')
        self.verify('uat', 'unavailable')
        with self.assertRaises(InsufficientContext): self.store.finish('p', 'owner')
        self.verify('uat')
        self.store.finish('p', 'owner')
        reopened = ProjectStore(self.path).export('p', 'owner')
        self.assertEqual(reopened['project']['state'], 'done')
        self.assertEqual(json.loads(json.dumps(reopened)), reopened)
        self.assertEqual([e['payload']['result'] for e in reopened['events'] if e['kind'] == 'done_gate'], ['blocked', 'blocked', 'done'])

    def test_new_artifact_cannot_reuse_old_passes(self):
        self.all_pass()
        old = self.output
        self.bind(2)
        with self.assertRaises(ValueError): self.verify(output=old)
        with self.assertRaises(InsufficientContext): self.store.finish('p', 'owner')
        self.all_pass()
        self.store.finish('p', 'owner')

    def test_correction_requires_new_output_then_reverification(self):
        self.all_pass()
        self.store.correction('p', 'owner', expected_revision=1, output_event=self.output,
            acceptance='layout', body='Move title above the picture', source='synthetic-owner-correction')
        self.all_pass()
        with self.assertRaises(InsufficientContext): self.store.finish('p', 'owner')
        with self.assertRaises(ValueError):
            self.store.bind_output('p', 'owner', expected_revision=1, artifact_id='fixture', artifact_revision=1, sha256='a'*64)
        self.bind(2)
        self.all_pass()
        self.store.finish('p', 'owner')

    def test_contract_change_invalidates_target_and_preserves_history(self):
        self.all_pass()
        self.confirm(1)
        with self.assertRaises(ValueError): self.verify()
        with self.assertRaises(InsufficientContext): self.store.finish('p', 'owner')
        exported = self.store.export('p', 'owner')
        self.assertEqual(len(exported['contracts']), 2)
        with self.assertRaises(ValueError): self.confirm(1)
        self.assertEqual(len(self.store.export('p', 'owner')['contracts']), 2)

    def test_later_fail_revokes_done(self):
        self.all_pass()
        self.store.finish('p', 'owner')
        self.verify('machine', 'fail')
        with self.assertRaises(InsufficientContext): self.store.finish('p', 'owner')

    def test_owner_boundary_and_schema(self):
        with self.assertRaises(AccessDenied): self.store.export('p', 'other')
        with self.assertRaises(AccessDenied): self.store.finish('p', 'other')
        with self.assertRaises(ValueError): self.verify(kind='all')
        with self.assertRaises(ValueError): self.verify(status='assumed_pass')
        with self.assertRaises(ValueError):
            self.store.confirm_contract('q', 'owner', goal='goal', acceptance={}, constraints=[], source='owner')

    def test_missing_context_or_denied_required_context_prevents_done(self):
        self.all_pass()
        original = self.records
        for records in ((), (replace(original[0], readers=frozenset({'other'})),),
                        (replace(original[0], status='historical'),),
                        (replace(original[0], authority='derived'),)):
            self.records = records
            with self.assertRaises(InsufficientContext): self.store.finish('p', 'owner')
            self.assertEqual(self.store.export('p', 'owner')['project']['state'], 'blocked')

    def test_missing_binary_and_unconfigured_gate_prevent_done(self):
        self.all_pass()
        with self.assertRaises(InsufficientContext): ProjectStore(self.path).finish('p', 'owner')
        manifest = self.artifacts.describe('fixture', 1, Access('owner', 'p'))
        (Path(self.tmp.name) / 'blobs' / manifest['sha256']).unlink()
        with self.assertRaises(InsufficientContext): self.store.finish('p', 'owner')

    def test_required_reference_artifact_and_optional_non_delivery(self):
        self.all_pass()
        self.store.completion_check = replace(self.gate, required_artifacts=(('missing-reference', 1),))
        with self.assertRaises(InsufficientContext): self.store.finish('p', 'owner')
        self.artifacts.register('missing-reference', 'p', b'reference', 'text/plain', ('owner',))
        self.records += (replace(self.records[0], id='private', readers=frozenset({'other'}), body='not delivered'),)
        self.store.finish('p', 'owner')

    def test_candidate_requires_local_evidence_and_never_becomes_binding(self):
        evidence = self.verify()
        self.store.candidate('p', 'owner', evidence_events=[evidence], body='<script>synthetic</script>')
        candidate = self.store.export('p', 'owner')['events'][-1]['payload']
        self.assertFalse(candidate['binding'])
        self.assertEqual(candidate['authority'], 'candidate')
        self.assertEqual(candidate['evidence_events'], [evidence])
        with self.assertRaises(ValueError): self.store.candidate('p', 'owner', evidence_events=[9999], body='lesson')
        html = self.store.inspection_html('p', 'owner')
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)
