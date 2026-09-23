import json
from pathlib import Path
import tempfile
import unittest

from nexus.artifacts import AccessDenied
from nexus.context_store import ContextStore
from nexus.decision_review import DecisionReview
from nexus.personal import HOME, PersonalContext
from tests.test_personal import LocalCapability


class DecisionReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ContextStore(Path(self.tmp.name) / 'personal.sqlite3')
        self.review = DecisionReview(self.store, LocalCapability(), 'owner')
        self.prediction = dict(project='sample', question='Ship now or review once more?',
            options={'ship': 'Ship now', 'review': 'Review once more'}, predicted_choice='review',
            reason='Unresolved failure', priorities='Evidence before speed',
            reversal_conditions='A passing recovery check', provenance='synthetic:before-answer')
        self.answer = dict(chosen_option='ship', quote='Ship now; the check has passed.',
            provenance='synthetic:later-answer', comparison='Choice differed; new evidence met the reversal condition.')

    def test_frozen_prediction_and_attributed_comparison(self):
        first = self.review.freeze('sample-1', **self.prediction)
        self.assertEqual(self.review.freeze('sample-1', **self.prediction), first)
        with self.assertRaises(ValueError):
            self.review.freeze('sample-1', **dict(self.prediction, predicted_choice='ship'))
        answer = self.review.evaluate('sample-1', **self.answer)
        self.assertEqual(answer, self.review.evaluate('sample-1', **self.answer))
        read = self.review.read('sample')
        self.assertEqual((read['evaluated'], read['matched']), (1, 0))
        self.assertEqual(read['items'][0]['latest']['actual_answer_timing'], 'not_verified')
        self.assertFalse(read['binding'])
        self.assertEqual(self.review.read('another')['items'], [])
        self.assertEqual(PersonalContext(self.store, LocalCapability(), 'owner').read('sample', 'implement')['items'], [])

    def test_answer_without_prediction_or_stale_correction_is_rejected(self):
        with self.assertRaises(ValueError):
            self.review.evaluate('missing', **self.answer)
        self.review.freeze('sample-1', **self.prediction)
        self.review.evaluate('sample-1', **self.answer)
        with self.assertRaises(ValueError):
            self.review.evaluate('sample-1', **dict(self.answer, chosen_option='review'))
        self.review.evaluate('sample-1', **dict(self.answer, chosen_option='review'), expected_revision=2)
        self.assertEqual(self.review.read('sample')['matched'], 1)
        self.assertEqual(len(self.store.export(HOME, 'owner')['sources']), 3)

    def test_withdrawal_keeps_history_and_excludes_score(self):
        self.review.freeze('sample-1', **self.prediction)
        self.review.evaluate('sample-1', **self.answer)
        self.review.withdraw('sample-1', reason='Synthetic only', provenance='synthetic:withdraw', expected_revision=2)
        self.assertEqual(self.review.read('sample')['evaluated'], 0)
        with self.assertRaises(ValueError):
            self.review.evaluate('sample-1', **self.answer, expected_revision=3)

    def test_existing_personal_archive_restores_predictions_and_reports(self):
        self.review.freeze('sample-1', **self.prediction)
        self.review.evaluate('sample-1', **self.answer)
        archive = Path(self.tmp.name) / 'archive.json'
        PersonalContext(self.store, LocalCapability(), 'owner').export_local(archive)
        restored = ContextStore(Path(self.tmp.name) / 'restored.sqlite3')
        restored.restore_export(json.loads(archive.read_text(encoding='utf-8')), 'owner')
        self.assertEqual(DecisionReview(restored, LocalCapability(), 'owner').read('sample'), self.review.read('sample'))

    def test_owner_and_explicit_scope_required(self):
        with self.assertRaises(AccessDenied):
            DecisionReview(self.store, LocalCapability(), 'someone').read('sample')
        with self.assertRaises(ValueError):
            self.review.freeze('sample-1', **dict(self.prediction, project='*'))


if __name__ == '__main__':
    unittest.main()
