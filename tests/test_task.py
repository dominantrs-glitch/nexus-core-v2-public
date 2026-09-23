from pathlib import Path
import tempfile
import unittest

from nexus.artifacts import InsufficientContext
from nexus.context_store import ContextStore
from nexus.core_backup import export_core, restore_core
from nexus.owner import ApprovalDeclined
from nexus.personal import PersonalContext
from nexus.task import Task


class Surface:
    def __init__(self): self.answer, self.requests = True, []
    def confirm(self, request):
        self.requests.append(request)
        return self.answer


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.surface = Surface()
        self.personal = ContextStore(self.root / 'personal.sqlite3')
        self.task = Task(self.root / 'task', 'real-task', 'owner', owner_root=self.root / 'owner',
                         surface=self.surface, personal_store=self.personal)
        self.draft = dict(goal='Produce a short guide.', acceptance={'guide': ['machine', 'contract', 'uat']},
                          constraints=['Local only.'], source='user request')
        self.file = self.root / 'guide.txt'
        self.file.write_text('guide v1', encoding='utf-8')

    def verify(self):
        for kind in ('machine', 'contract', 'uat'):
            self.task.verify('guide', kind, 'pass', 'test-only evidence', review_file=self.file)

    def test_generic_loop_correction_and_resume(self):
        self.task.start(self.draft)
        self.task.start(self.draft)
        self.assertEqual(len(self.surface.requests), 1)
        self.task.publish_output(self.file)
        with self.assertRaises(InsufficientContext): self.task.finish()
        self.verify()
        self.assertEqual(self.task.finish()['project']['state'], 'done')
        self.task.correct('guide', 'Use shorter sentences.', 'test correction')
        with self.assertRaises(InsufficientContext): self.task.finish()
        self.file.write_text('short v2', encoding='utf-8')
        self.task.publish_output(self.file)
        with self.assertRaises(InsufficientContext): self.task.finish()
        self.verify()
        self.assertEqual(self.task.finish()['project']['state'], 'done')

    def test_personal_context_used_without_manual_attachment(self):
        personal = PersonalContext(self.personal, self.task.adapter.capability, 'owner')
        personal.capture('style', text='Short progress.', provenance='test source',
            projects=frozenset({'real-task'}), operations=frozenset({'implement'}))
        view = self.task.start(self.draft)
        self.assertEqual(view['personal']['items'][0]['text'], 'Short progress.')
        self.assertEqual(self.task.read('review')['personal']['items'], [])

    def test_cancel_and_mismatched_review(self):
        self.surface.answer = False
        with self.assertRaises(ApprovalDeclined): self.task.start(self.draft)
        self.assertEqual(self.task.contexts.export('real-task', 'owner')['sources'], [])
        self.surface.answer = True
        self.task.start(self.draft)
        self.task.publish_output(self.file)
        self.file.write_text('wrong file', encoding='utf-8')
        count = len(self.surface.requests)
        with self.assertRaises(InsufficientContext):
            self.task.verify('guide', 'uat', 'pass', 'test', review_file=self.file)
        self.assertEqual(len(self.surface.requests), count)

    def test_missing_required_reference_fails_before_work(self):
        self.task.start(self.draft)
        self.task.publish_output(self.file)
        self.verify()
        self.task.finish()
        with self.task.contexts._db() as db:
            db.execute("UPDATE contexts SET status='suppressed'")
        with self.assertRaises(InsufficientContext): self.task.publish_output(self.file)
        self.assertEqual(self.task.snapshot()['project']['state'], 'blocked')
        self.assertEqual(self.task.snapshot()['events'][-1]['kind'], 'context_gate')

    def test_restored_project_can_resume_through_same_operator(self):
        self.task.start(self.draft)
        self.task.publish_output(self.file)
        self.verify()
        self.task.finish()
        export_core(self.task.projects, self.task.contexts, self.task.artifacts, 'real-task', 'owner', self.root / 'backup')
        restore_core(self.root / 'backup', self.root / 'restored', 'owner')
        resumed = Task(self.root / 'restored', 'real-task', 'owner', owner_root=self.root / 'owner', surface=self.surface)
        self.assertEqual(resumed.finish()['project']['state'], 'done')
        self.file.write_text('restored revision', encoding='utf-8')
        self.assertEqual(resumed.publish_output(self.file)['output']['payload']['revision'], 2)


if __name__ == '__main__': unittest.main()
