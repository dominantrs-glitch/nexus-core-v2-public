"""Synthetic clients only; confirmations here are test fixtures, never owner UAT."""
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest

from nexus.artifacts import InsufficientContext
from nexus.context_store import ContextStore
from nexus.core_backup import export_core, restore_core
from nexus.learning import HOME
from nexus.task import Task


class SyntheticSurface:
    def confirm(self, request):
        return True


def produce(rows, lessons):
    """Small deterministic client fixture, not a general autonomous code executor."""
    if any('区切り文字を含む値も一つの項目として保つ' in i['principle'] for i in lessons):
        stream = io.StringIO(newline='')
        csv.writer(stream).writerows(rows)
        return stream.getvalue()
    return '\n'.join(','.join(row) for row in rows) + '\n'


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = ContextStore(self.root / 'learning.sqlite3')
        self.draft = dict(goal='Export rows without losing fields.',
                          acceptance={'roundtrip': ['machine', 'contract', 'uat']},
                          constraints=['Preserve every input field.'], source='synthetic-only fixture')
        self.a = self.task('A')
        self.a.start(self.draft)
        self.file = self.root / 'output.csv'
        self.rows_a = [['apple,pear', '10'], ['quote"', '20']]

    def task(self, project, store=None):
        return Task(self.root / project, project, 'owner', owner_root=self.root / 'owner',
                    surface=SyntheticSurface(), learning_store=store or self.store)

    def output(self, task, value):
        self.file.write_text(value, encoding='utf-8', newline='')
        return task.publish_output(self.file, mime='text/plain')

    def verify(self, task, value, rows):
        status = 'pass' if list(csv.reader(io.StringIO(value))) == rows else 'fail'
        for kind in ('machine', 'contract'):
            task.verify('roundtrip', kind, status, 'synthetic csv.reader roundtrip: ' + status)
        return status

    def repaired_source(self):
        bad = produce(self.rows_a, [])
        self.output(self.a, bad)
        self.assertEqual(self.verify(self.a, bad, self.rows_a), 'fail')
        self.a.correct('roundtrip', 'Keep commas inside their original field.', 'synthetic correction')
        good = produce(self.rows_a, [{'principle': '区切り文字を含む値も一つの項目として保つ'}])
        self.output(self.a, good)
        self.assertEqual(self.verify(self.a, good, self.rows_a), 'pass')

    def capture(self, **changes):
        options = dict(principle='区切り文字を含む値も一つの項目として保つ',
                       rationale='分割してから戻した値を元の入力と比較する。',
                       projects=frozenset({'B', 'C'}), required_constraints=['Preserve every input field.'],
                       forbidden_constraints=['Do not quote fields.'])
        options.update(changes)
        return self.a.learn('csv-fields', **options)

    def test_a_failure_repair_b_automatic_retrieval_application_and_outcome(self):
        self.repaired_source()
        receipt = self.capture()
        self.assertFalse(receipt['binding'])
        # Reopen both stores as a fresh client; no manual lesson read/ID in start.
        b = self.task('B', ContextStore(self.root / 'learning.sqlite3'))
        view = b.start(self.draft)
        lessons = view['learning']['items']
        self.assertEqual(len(lessons), 1)
        lesson = lessons[0]
        self.assertFalse(lesson['binding'])
        self.assertEqual(lesson['authority'], 'candidate')
        self.assertNotIn('correction', json.dumps(lessons))
        self.assertNotIn(str(self.root), json.dumps(lessons))
        application = b.apply_learning(lesson['id'], lesson['revision'], 'Local reversible CSV fixture; current Contract unchanged.')
        self.assertEqual(application, b.apply_learning(lesson['id'], lesson['revision'], 'Local reversible CSV fixture; current Contract unchanged.'))
        rows_b = [['first\nsecond', 'a,b'], ['日本語"', '30']]
        self.assertNotEqual(list(csv.reader(io.StringIO(produce(rows_b, [])))), rows_b)
        value = produce(rows_b, lessons)
        self.output(b, value)
        self.assertEqual(self.verify(b, value, rows_b), 'pass')
        outcome = b.evaluate_learning(application)
        self.assertEqual(outcome['result'], 'pass')
        self.assertEqual(outcome['uat'], 'not_inferred')
        # Idempotent replay after another client opens the same files.
        resumed = self.task('B', ContextStore(self.root / 'learning.sqlite3'))
        count = len(resumed.snapshot()['events'])
        self.assertEqual(resumed.evaluate_learning(application), outcome)
        self.assertEqual(len(resumed.snapshot()['events']), count)
        with self.assertRaises(InsufficientContext):
            resumed.finish()  # missing native UAT remains a hard gate

    def test_scope_conditions_modes_and_acl_do_not_deliver(self):
        self.repaired_source()
        self.capture()
        for project, constraints in [('unrelated', self.draft['constraints']), ('C', ['Different task.']),
                                     ('B', self.draft['constraints'] + ['Do not quote fields.'])]:
            view = self.task(project).start(dict(self.draft, constraints=constraints))
            self.assertEqual(view['learning']['items'], [])
        a = self.a.learning
        for mode in ('independent', 'red-team'):
            self.assertEqual(a.read('B', 'implement', self.draft, mode=mode)['items'], [])
        with self.store._db() as db:
            db.execute("UPDATE context_readers SET value='someone-else'")
        self.assertEqual(a.read('B', 'implement', self.draft)['items'], [])

    def restored_source(self):
        destination = self.root / 'restored'
        export_core(self.a.projects, self.a.contexts, self.a.artifacts, 'A', 'owner', self.root / 'bundle')
        restore_core(self.root / 'bundle', destination, 'owner')
        return Task(destination, 'A', 'owner', owner_root=self.root / 'owner',
                    surface=SyntheticSurface(), learning_store=self.store)

    def test_restored_learning_rebind_preserves_history_scope_and_invalidates_old_application(self):
        self.repaired_source(); self.capture()
        b = self.task('B'); b.start(self.draft)
        application = b.apply_learning('csv-fields', 1, 'synthetic local fixture')
        original = self.store.export(HOME, 'owner')
        restored = self.restored_source()
        # Move the synthetic old source offline, within the test workspace.
        old = self.a.root.resolve(); offline = (self.root / 'offline-A').resolve()
        self.assertTrue(old.is_relative_to(self.root.resolve()) and offline.is_relative_to(self.root.resolve()))
        old.rename(offline)
        self.assertEqual(b.read('implement')['learning']['items'], [])
        learning = restored.learning
        self.assertEqual(learning.rebind_source(restored, 'csv-fields', 1), 2)
        self.assertEqual(learning.rebind_source(restored, 'csv-fields', 1), 2)
        current = self.store.export(HOME, 'owner')
        self.assertEqual(len(current['contexts']), 2)
        self.assertEqual(current['contexts'][0], original['contexts'][0])
        for key in ('body','projects','operations','readers','authority','status'):
            self.assertEqual(current['contexts'][1][key], original['contexts'][0][key])
        self.assertEqual(current['sources'][0], original['sources'][0])
        items = b.read('implement')['learning']['items']
        self.assertEqual(items[0]['revision'], 2); self.assertFalse(items[0]['binding'])
        self.assertNotIn(str(self.root), json.dumps(items))
        self.output(b, produce(self.rows_a, items)); self.verify(b, produce(self.rows_a, items), self.rows_a)
        with self.assertRaises(InsufficientContext): b.evaluate_learning(application)
        self.assertEqual(self.task('unrelated').start(self.draft)['learning']['items'], [])
        # Every future read still checks the restored source, not a cached PASS.
        with restored.contexts._db() as db: db.execute("UPDATE contexts SET status='suppressed'")
        self.assertEqual(b.read('implement')['learning']['items'], [])

    def test_rebind_rejects_known_regression_and_stale_restore_without_changing_candidate(self):
        self.repaired_source(); self.capture(); restored = self.restored_source()
        before = self.store.export(HOME, 'owner')
        self.a.verify('roundtrip', 'machine', 'fail', 'later regression')
        with self.assertRaises(InsufficientContext): restored.learning.rebind_source(restored, 'csv-fields', 1)
        self.assertEqual(self.store.export(HOME, 'owner'), before)

    def test_frozen_source_is_withheld_until_explicit_verified_rebind(self):
        from nexus.core_freeze import freeze_core, inspect_core
        self.repaired_source(); self.capture()
        freeze_core(self.a, inspect_core(self.a)['source_digest'])
        self.assertEqual(self.a.learning.read('B', 'implement', self.draft)['items'], [])
        with self.assertRaisesRegex(InsufficientContext, 'archived'):
            self.a.learning.rebind_source(self.a, 'csv-fields', 1)
        restored = self.restored_source()
        self.assertEqual(restored.learning.rebind_source(restored, 'csv-fields', 1), 2)
        self.assertEqual(restored.learning.read('B', 'implement', self.draft)['items'][0]['revision'], 2)

    def test_frozen_regressed_source_cannot_be_rebound_to_older_success(self):
        from nexus.core_freeze import freeze_core, inspect_core
        self.repaired_source(); self.capture(); restored = self.restored_source()
        self.a.verify('roundtrip', 'machine', 'fail', 'regression before migration')
        freeze_core(self.a, inspect_core(self.a)['source_digest'])
        with self.assertRaises(InsufficientContext):
            restored.learning.rebind_source(restored, 'csv-fields', 1)

    def native_source(self):
        from nexus.core_migration import prepare_core, freeze_prepared_core
        from nexus.git_core import GitCore
        from tests.test_git_core import MemoryCore
        package = self.root / 'git-package'
        prepare_core(self.a, package); freeze_prepared_core(self.a, package)
        remote = MemoryCore()
        store = GitCore(self.root / 'git-route','owner','test-1',owner_root=self.root / 'owner',
            surface=SyntheticSurface(),route_name='canonical-native',learning_store=self.store,call=remote)
        store.import_prepared(package,'import-source')
        return store, remote

    def capture_native(self, task, *, identity='git-fields', expected_revision=0):
        return task.learn(identity, principle='区切り文字を含む値も一つの項目として保つ', rationale='compare roundtrip',
            projects=frozenset({'B'}), required_constraints=self.draft['constraints'], expected_revision=expected_revision)

    def test_git_learning_survives_scratch_cleanup_and_withholds_on_current_source_regression(self):
        self.repaired_source(); store, remote = self.native_source()
        with store.open('A') as session:
            self.capture_native(session.task)
            scratch = session.task.root
        self.assertFalse(scratch.exists())
        proof = json.loads(self.store.export(HOME,'owner')['sources'][0]['body'])
        self.assertEqual(proof['schema'],'nexus.lesson-proof.v2')
        self.assertEqual(proof['native_source'],dict(route='canonical-native',generation='test-1'))
        self.assertNotIn('database',proof); self.assertNotIn(str(scratch),json.dumps(proof))
        b = self.task('B')
        self.assertEqual(b.start(self.draft)['learning']['items'], [])  # no implicit route discovery
        b.learning.native_sources = {'canonical-native':store}
        self.assertEqual(b.read('implement')['learning']['items'][0]['id'],'git-fields')
        with store.open('A') as session:
            session.task.verify('roundtrip','machine','fail','later native Git regression')
            session.commit('regression')
        self.assertEqual(b.read('implement')['learning']['items'], [])

    def test_uncommitted_native_scratch_is_not_a_learning_source(self):
        self.repaired_source(); store, remote = self.native_source()
        before = self.store.export(HOME,'owner')
        with store.open('A') as session:
            session.task.correct('roundtrip','another synthetic correction','fixture')
            self.output(session.task,produce(self.rows_a,[{'principle':'区切り文字を含む値も一つの項目として保つ'}]))
            self.verify(session.task,produce(self.rows_a,[{'principle':'区切り文字を含む値も一つの項目として保つ'}]),self.rows_a)
            with self.assertRaises(InsufficientContext): self.capture_native(session.task)
        self.assertEqual(self.store.export(HOME,'owner'),before)
        self.assertEqual(remote.writes,1)

    def test_frozen_local_learning_rebinds_to_git_with_history_and_retry_preserved(self):
        self.repaired_source(); self.capture(); before=self.store.export(HOME,'owner')
        store, remote = self.native_source()
        with store.open('A') as session:
            self.assertEqual(session.task.learning.rebind_source(session.task,'csv-fields',1),2)
            self.assertEqual(session.task.learning.rebind_source(session.task,'csv-fields',1),2)
        after=self.store.export(HOME,'owner')
        self.assertEqual(after['contexts'][0],before['contexts'][0]); self.assertEqual(after['sources'][0],before['sources'][0])
        newest=json.loads(after['sources'][-1]['body'])
        self.assertEqual(newest['schema'],'nexus.lesson-proof.v2'); self.assertNotIn('database',newest)
        with store.open('A') as session:
            session.task.classify_work(['data-export'],'synthetic classification')
            session.commit('classification')
        with store.open('A') as session:
            self.assertEqual(session.task.learning.rebind_source(session.task,'csv-fields',1),2)
        b=self.task('B');b.learning.native_sources={'canonical-native':store}
        self.assertEqual(b.start(self.draft)['learning']['items'][0]['revision'],2)

    def test_native_checkpoint_rollback_freeze_and_generation_change_withhold_learning(self):
        from copy import deepcopy
        self.repaired_source(); store, remote = self.native_source()
        original=deepcopy(remote.document)
        with store.open('A') as session:
            session.task.classify_work(['data-export'],'synthetic classification')
            session.commit('classification')
        with store.open('A') as session:self.capture_native(session.task)
        b=self.task('B');b.learning.native_sources={'canonical-native':store}
        self.assertEqual(len(b.start(self.draft)['learning']['items']),1)
        current=remote.document;remote.document=original
        self.assertEqual(b.read('implement')['learning']['items'],[])
        remote.document=current;remote.state='frozen'
        self.assertEqual(b.read('implement')['learning']['items'],[])
        remote.state='active';remote.generation='different-generation'
        self.assertEqual(b.read('implement')['learning']['items'],[])

    def test_rebind_checks_restored_original_context_identity_and_suppression(self):
        self.repaired_source(); self.capture(); restored = self.restored_source()
        before = self.store.export(HOME, 'owner')
        with restored.contexts._db() as db: db.execute("UPDATE contexts SET status='suppressed'")
        with self.assertRaises(InsufficientContext): restored.learning.rebind_source(restored, 'csv-fields', 1)
        with restored.contexts._db() as db: db.execute("UPDATE contexts SET status='current'")
        proof = json.loads(before['sources'][0]['body'])
        blob = restored.artifacts.provider.root / proof['output']['payload']['sha256']
        original = blob.read_bytes(); blob.write_bytes(b'corrupt')
        with self.assertRaises(InsufficientContext): restored.learning.rebind_source(restored, 'csv-fields', 1)
        blob.write_bytes(original)
        with self.assertRaises(InsufficientContext): restored.learning.rebind_source(self.task('B'), 'csv-fields', 1)
        with self.assertRaises(ValueError): restored.learning.rebind_source(restored, 'csv-fields', 99)
        self.assertEqual(self.store.export(HOME, 'owner'), before)
        restored.learning.suppress('csv-fields', 1)
        with self.assertRaises(InsufficientContext): restored.learning.rebind_source(restored, 'csv-fields', 2)

    def test_rebind_does_not_reactivate_superseded_proof(self):
        self.repaired_source(); self.capture(); restored = self.restored_source()
        original = self.store.export(HOME, 'owner')['sources'][0]
        self.store.register_source(original['id'], HOME, 'owner', original['body'], expected_revision=1)
        with self.assertRaises(InsufficientContext): restored.learning.rebind_source(restored, 'csv-fields', 1)
        self.assertEqual(len(self.store.export(HOME, 'owner')['contexts']), 1)

    def test_source_fail_after_pass_and_missing_original_withhold(self):
        self.repaired_source()
        self.capture()
        self.a.verify('roundtrip', 'machine', 'fail', 'later regression')
        self.assertEqual(self.a.learning.read('B', 'implement', self.draft)['items'], [])
        self.a.verify('roundtrip', 'machine', 'pass', 'recheck')
        self.assertEqual(self.a.learning.read('B', 'implement', self.draft)['items'], [])  # requires new capture
        self.capture(expected_revision=1)
        self.assertEqual(len(self.a.learning.read('B', 'implement', self.draft)['items']), 1)
        sha = self.a.read('review')['output']['payload']['sha256']
        (self.a.artifacts.provider.root / sha).unlink()
        self.assertEqual(self.a.learning.read('B', 'implement', self.draft)['items'], [])

    def test_capture_requires_repair_and_verification_and_is_idempotent(self):
        with self.assertRaises(InsufficientContext): self.capture()
        self.repaired_source()
        first = self.capture()
        self.assertEqual(self.capture(), first)
        self.assertEqual(len(self.store.export(HOME, 'owner')['contexts']), 1)
        with self.assertRaises(ValueError): self.capture(principle='different stale write')
        with self.assertRaises(ValueError): self.capture(projects=frozenset({'*'}))
        with self.assertRaises(ValueError): self.capture(required_constraints=[])

    def test_suppression_and_revision_conflict(self):
        self.repaired_source()
        self.capture()
        b = self.task('B')
        b.start(self.draft)
        app = b.apply_learning('csv-fields', 1, 'synthetic safe local work')
        self.assertEqual(self.a.learning.suppress('csv-fields', 1), 2)
        self.assertEqual(b.read('implement')['learning']['items'], [])
        with self.assertRaises(InsufficientContext): b.apply_learning('csv-fields', 1, 'stale')
        with self.assertRaises(ValueError): self.a.learning.suppress('csv-fields', 1)
        self.output(b, produce(self.rows_a, []))
        with self.assertRaises(InsufficientContext): b.evaluate_learning(app)
        self.assertEqual(len(self.store.export(HOME, 'owner')['contexts']), 2)

    def test_target_missing_context_fail_closed_and_bad_output_is_not_success(self):
        self.repaired_source()
        self.capture()
        b = self.task('B')
        b.start(self.draft)
        app = b.apply_learning('csv-fields', 1, 'synthetic')
        with self.assertRaises(InsufficientContext): b.evaluate_learning(app)
        bad = produce(self.rows_a, [])
        self.output(b, bad)
        self.assertEqual(b.evaluate_learning(app)['result'], 'not_verified')
        self.verify(b, bad, self.rows_a)
        self.assertEqual(b.evaluate_learning(app)['result'], 'fail')
        with b.contexts._db() as db:
            db.execute("UPDATE contexts SET status='suppressed'")
        with self.assertRaises(InsufficientContext): b.read('implement')
        self.assertEqual(b.snapshot()['project']['state'], 'blocked')

    def test_source_gate_revoked_or_source_tampered_is_not_delivered(self):
        self.repaired_source()
        self.capture()
        with self.a.contexts._db() as db:
            db.execute("UPDATE contexts SET status='suppressed'")
        self.assertEqual(self.a.learning.read('B', 'implement', self.draft)['items'], [])
        with self.a.contexts._db() as db:
            db.execute("UPDATE contexts SET status='current'")
        with self.store._db() as db:
            db.execute("UPDATE sources SET body=body || ' '")
        self.assertEqual(self.a.learning.read('B', 'implement', self.draft)['items'], [])

    def test_target_required_original_gate_precedes_learning(self):
        self.repaired_source()
        self.capture()
        b = self.task('B')
        b.start(self.draft)
        policy = b.contexts.policy('B', 'owner', 1, 'implement')
        b.contexts.set_operation_policy('B', 'owner', contract_revision=1, operation='implement',
            principal='owner', required_context=policy.required_context,
            required_authority=policy.required_authority, required_artifacts=(('missing-original', 1),),
            expected_revision=policy.revision)
        with self.assertRaises(InsufficientContext): b.apply_learning('csv-fields', 1, 'no bypass')
        self.assertEqual(b.snapshot()['project']['state'], 'blocked')

    def test_work_type_reuses_in_new_project_but_not_other_work(self):
        self.repaired_source()
        self.capture(projects=frozenset(), reuse_scope='task_type', applicable_work_types=['slides'],
                     reuse_basis='synthetic local owner request for work-type reuse')
        slides = self.task('never-listed-before')
        self.assertEqual(slides.start(self.draft)['learning']['items'], [])
        seq = slides.classify_work(['slides'], 'synthetic classification')
        self.assertEqual(seq, slides.classify_work(['slides'], 'synthetic classification'))
        view = self.task('never-listed-before').read('implement')
        self.assertEqual(len(view['learning']['items']), 1)
        self.assertEqual(view['learning']['items'][0]['judgment_layer'], 'work_knowledge')
        self.assertFalse(view['work_classification']['payload']['binding'])
        app = slides.apply_learning('csv-fields', 1, 'synthetic same-type reuse')
        value = produce(self.rows_a, view['learning']['items'])
        self.output(slides, value)
        self.verify(slides, value, self.rows_a)
        self.assertEqual(slides.evaluate_learning(app)['result'], 'pass')
        other = self.task('spreadsheet-case')
        other.start(self.draft)
        other.classify_work(['spreadsheets'], 'synthetic classification')
        self.assertEqual(other.read('implement')['learning']['items'], [])
        other.classify_work(['slides', 'spreadsheets'], 'synthetic mixed work',
                            expected_event=other.read('implement')['work_classification']['seq'])
        self.assertEqual(len(other.read('implement')['learning']['items']), 1)

    def test_general_judgment_candidate_does_not_replace_contract_or_personal_truth(self):
        self.repaired_source()
        self.capture(projects=frozenset(), reuse_scope='general', required_constraints=[],
                     reuse_basis='synthetic owner request for general judgment candidates')
        other = self.task('any-new-project')
        before = other.start(dict(self.draft, constraints=['Current explicit constraint.']))['contract']
        view = other.read('implement')
        item = view['learning']['items'][0]
        self.assertEqual(item['reuse_scope'], 'general')
        self.assertEqual(item['judgment_layer'], 'candidate')
        self.assertEqual(item['authority'], 'candidate')
        self.assertFalse(item['binding'])
        self.assertEqual(view['contract'], before)
        self.assertEqual(view['personal']['items'], [])
        self.assertEqual(other.read('implement', mode='independent')['learning']['items'], [])
        self.a.learning.suppress('csv-fields', 1)
        self.assertEqual(other.read('implement')['learning']['items'], [])

    def test_scope_classification_schema_conflicts_and_stale_application(self):
        self.repaired_source()
        for options in [dict(reuse_scope='general'),
                        dict(projects=frozenset(), reuse_scope='task_type', reuse_basis='test'),
                        dict(projects=frozenset(), reuse_scope='general', applicable_work_types=['slides'], reuse_basis='test')]:
            with self.assertRaises(ValueError): self.capture(**options)
        self.capture(projects=frozenset(), reuse_scope='task_type', applicable_work_types=['slides'], reuse_basis='test')
        b = self.task('B')
        b.start(self.draft)
        seq = b.classify_work(['slides'], 'test')
        app = b.apply_learning('csv-fields', 1, 'local test scope')
        with self.assertRaises(ValueError): b.classify_work(['other'], 'test')
        b.classify_work(['other'], 'test', expected_event=seq)
        self.assertEqual(b.read('implement')['learning']['items'], [])
        value = produce(self.rows_a, [{'principle': '区切り文字を含む値も一つの項目として保つ'}])
        self.output(b, value)
        self.verify(b, value, self.rows_a)
        with self.assertRaises(InsufficientContext): b.evaluate_learning(app)

    def test_general_still_checks_conflict_source_and_reader_permission(self):
        self.repaired_source()
        self.capture(projects=frozenset(), reuse_scope='general', required_constraints=[], reuse_basis='test')
        self.assertEqual(self.a.learning.read('new', 'implement', dict(self.draft, constraints=['Do not quote fields.']))['items'], [])
        with self.store._db() as db:
            db.execute("UPDATE context_readers SET value='another-owner'")
        self.assertEqual(self.a.learning.read('new', 'implement', self.draft)['items'], [])
        with self.store._db() as db:
            db.execute("UPDATE context_readers SET value='owner'")
        self.a.verify('roundtrip', 'machine', 'fail', 'regression')
        self.assertEqual(self.a.learning.read('new', 'implement', self.draft)['items'], [])


if __name__ == '__main__':
    unittest.main()
