import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from nexus.recovery_inventory import inventory


class RecoveryInventoryTests(unittest.TestCase):
    def test_live_wal_data_is_restored_without_credentials_or_overwriting_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'source'
            (root / 'personal').mkdir(parents=True)
            (root / 'owner').mkdir()
            (root / 'owner' / 'secret.key').write_text('synthetic secret')
            source = root / 'personal' / 'context.sqlite3'
            with closing(sqlite3.connect(source)) as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE notes (body TEXT)')
                db.execute("INSERT INTO notes VALUES ('latest committed value')")
                db.commit()
                destination = Path(directory) / 'snapshot'
                report = inventory(root, destination)
                self.assertEqual(report['databases'][0]['backup'], 'created_and_opened')
                with closing(sqlite3.connect(destination / 'personal' / 'context.sqlite3')) as restored:
                    self.assertEqual(restored.execute('SELECT body FROM notes').fetchone()[0], 'latest committed value')
                self.assertFalse(report['complete_system_restore'])
                self.assertFalse((destination / 'owner').exists())
                self.assertNotIn('synthetic secret', json.dumps(report))
                with self.assertRaises(FileExistsError):
                    inventory(root, destination)
