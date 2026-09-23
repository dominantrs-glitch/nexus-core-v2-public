import json
from pathlib import Path
import tempfile
import unittest
from nexus.efficiency import record, report


class EfficiencyTests(unittest.TestCase):
    def test_measured_repeats_do_not_invent_tokens_or_save_private_content(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'metrics.sqlite3'
            for _ in range(2):
                self.assertTrue(record('read', {'secret': 'synthetic-private-query'}, {'body': 'synthetic-private-note'}, 25, database=path))
            record('read', {'secret': 'synthetic-private-query'}, {'body': 'changed'}, 30, database=path)
            record('save', {'secret': 'synthetic-private-query'}, None, 10, database=path)
            result = report(path)
            self.assertEqual(result['same_request_same_response_repeats'], 1)
            self.assertEqual(result['elapsed_ms'], 90)
            self.assertIsNone(result['token_usage'])
            self.assertTrue(result['proposals'][0]['requires_owner_selection'])
            self.assertNotIn(b'synthetic-private', path.read_bytes())
            self.assertNotIn('synthetic-private', json.dumps(result))

    def test_unavailable_measurements_do_not_break_main_work(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertFalse(record('read', {}, {}, 0, database=Path(root)))
            self.assertEqual(report(Path(root) / 'absent.sqlite3')['calls'], 0)


if __name__ == '__main__':
    unittest.main()
