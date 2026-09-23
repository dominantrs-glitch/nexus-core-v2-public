"""Synthetic-only preparation/freeze/retry, no live migration or owner UAT."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nexus.artifacts import InsufficientContext
from nexus.core_freeze import archived
from nexus.core_migration import freeze_prepared_core, prepare_core, verify_package
from nexus.task import Task


class SyntheticSurface:
    def confirm(self, request): return True


class CoreMigrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.task = Task(self.root / 'task', 'example', 'owner', owner_root=self.root / 'owner', surface=SyntheticSurface())
        self.task.start(dict(goal='Synthetic example', acceptance={'file': ['machine']}, constraints=[], source='fixture'))
        self.file = self.root / 'file.txt'
        self.file.write_text('synthetic', encoding='utf-8')
        self.task.publish_output(self.file)
        self.destination = self.root / 'prepared'

    def test_prepare_is_read_only_and_frozen_retry_preserves_all_records(self):
        before = self.task.snapshot()
        review = prepare_core(self.task, self.destination)
        self.assertEqual(before, self.task.snapshot())
        self.assertEqual(verify_package(self.destination), review)
        self.assertFalse(archived(self.task.projects.database, 'example'))
        self.assertNotIn('capability', ' '.join(review['files']))
        frozen = freeze_prepared_core(self.task, self.destination)
        self.assertEqual(frozen, freeze_prepared_core(self.task, self.destination))
        self.assertEqual(before, self.task.projects.export('example', 'owner'))
        self.assertEqual(verify_package(self.destination), review)

    def test_changed_source_after_review_does_not_freeze(self):
        prepare_core(self.task, self.destination)
        self.task.verify('file', 'machine', 'pass', 'later event same Contract revision')
        with self.assertRaisesRegex(InsufficientContext, 'does not match source'):
            freeze_prepared_core(self.task, self.destination)
        self.assertFalse(archived(self.task.projects.database, 'example'))

    def test_corrupt_package_and_wrong_project_never_freeze(self):
        prepare_core(self.task, self.destination)
        manifest = self.destination / 'bundle' / 'context.json'
        original = manifest.read_bytes()
        manifest.write_bytes(b'{}')
        with self.assertRaisesRegex(InsufficientContext, 'package changed'):
            freeze_prepared_core(self.task, self.destination)
        manifest.write_bytes(original)
        with patch.object(self.task, 'project', 'another'):
            with self.assertRaisesRegex(InsufficientContext, 'identity mismatch'):
                freeze_prepared_core(self.task, self.destination)
        self.assertFalse(archived(self.task.projects.database, 'example'))

    def test_failure_or_change_during_prepare_leaves_source_live_and_no_ready_marker(self):
        from nexus import core_migration
        original = core_migration._verify_restore
        def changed(bundle, owner):
            original(bundle, owner)
            self.task.verify('file', 'machine', 'pass', 'concurrent change')
        with patch.object(core_migration, '_verify_restore', side_effect=changed):
            with self.assertRaisesRegex(InsufficientContext, 'source changed during'):
                prepare_core(self.task, self.destination)
        self.assertFalse((self.destination / 'package.json').exists())
        self.assertFalse(archived(self.task.projects.database, 'example'))

    def test_uncertain_receipt_write_retries_without_unfreezing_or_new_generation(self):
        prepare_core(self.task, self.destination)
        with patch('nexus.core_migration._write_new', side_effect=OSError('synthetic disk unavailable')):
            with self.assertRaises(OSError):
                freeze_prepared_core(self.task, self.destination)
        self.assertTrue(archived(self.task.projects.database, 'example'))
        result = freeze_prepared_core(self.task, self.destination)
        self.assertEqual(json.loads((self.destination / 'frozen.json').read_text()), result)
        self.assertEqual(result, freeze_prepared_core(self.task, self.destination))

    def test_atomic_ready_marker_failure_is_retryable_and_never_exposes_partial_json(self):
        prepare_core(self.task, self.destination)
        import os
        link = os.link
        def fail_receipt(source, target):
            if Path(target).name == 'frozen.json':
                raise OSError('synthetic link failure')
            return link(source, target)
        with patch('nexus.core_migration.os.link', side_effect=fail_receipt):
            with self.assertRaises(OSError):
                freeze_prepared_core(self.task, self.destination)
        self.assertFalse((self.destination / 'frozen.json').exists())
        self.assertEqual(list(self.destination.glob('.nexus-preparing-*')), [])
        self.assertTrue(archived(self.task.projects.database, 'example'))
        result = freeze_prepared_core(self.task, self.destination)
        self.assertEqual(result['status'], 'frozen')


if __name__ == '__main__': unittest.main()
