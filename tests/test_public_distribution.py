import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('distribution', Path(__file__).resolve().parents[1] / 'scripts/prepare_public_distribution.py')
distribution = importlib.util.module_from_spec(spec)
spec.loader.exec_module(distribution)


class DistributionTests(unittest.TestCase):
    def fixture(self, root):
        (root / 'source.txt').write_bytes(b'account-placeholder\n')
        return {'schema': 1, 'private_patterns': ['private-owner'], 'files': [{
            'source': 'source.txt', 'path': 'README.md',
            'sha256': hashlib.sha256((root / 'source.txt').read_bytes()).hexdigest()}]}

    def test_exact_allowlist_and_archive_exclude_extra_private_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = self.fixture(root)
            (root / 'private.sqlite3').write_bytes(b'private-owner')
            result = distribution.build(root, manifest, root / 'candidate')
            self.assertEqual(result['files'], 1)
            self.assertFalse(result['published'])
            self.assertEqual(distribution.verify(root / 'candidate')['bytes_verified'], True)
            (root / 'candidate' / 'secret.txt').write_text('extra')
            with self.assertRaises(ValueError):
                distribution.verify(root / 'candidate')

    def test_changed_source_private_payload_and_replacement_drift_stop_before_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = self.fixture(root)
            row = manifest['files'][0]
            (root / 'source.txt').write_bytes(b'private-owner')
            with self.assertRaises(ValueError):
                distribution.build(root, manifest, root / 'candidate')
            row['sha256'] = hashlib.sha256(b'private-owner').hexdigest()
            with self.assertRaises(ValueError):
                distribution.build(root, manifest, root / 'candidate')
            row['replacements'] = [{'from': 'private-owner', 'to': 'PUBLIC', 'count': 2}]
            with self.assertRaises(ValueError):
                distribution.build(root, manifest, root / 'candidate')
            self.assertFalse((root / 'candidate').exists())
            row['replacements'][0]['count'] = 1
            distribution.build(root, manifest, root / 'candidate')
            self.assertEqual((root / 'candidate/README.md').read_text(), 'PUBLIC')

    def test_unsafe_paths_and_existing_destination_rejected(self):
        for value in ['../a', '/a', 'C:/a', 'a\\b', '.git/config', 'runtime/a', 'a.key', '.env.local', 'config.remote.jsonc']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                distribution.relative(value)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = self.fixture(root)
            with self.assertRaises(ValueError):
                distribution.build(root, manifest, root)

    def test_case_aliases_and_private_key_material_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = self.fixture(root)
            manifest['files'].append({**manifest['files'][0], 'path': 'readme.md'})
            with self.assertRaises(ValueError):
                distribution.assemble(root, manifest)
            manifest['files'].pop()
            data = ('-----BEGIN PRIVATE KEY-----\n' + 'a' * 100).encode()
            (root / 'source.txt').write_bytes(data)
            manifest['files'][0]['sha256'] = hashlib.sha256(data).hexdigest()
            with self.assertRaises(ValueError):
                distribution.assemble(root, manifest)

    def test_mit_release_requires_license_and_marks_only_preparation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = self.fixture(root)
            manifest['license'] = 'MIT'
            with self.assertRaises(ValueError):
                distribution.assemble(root, manifest)
            data = b'MIT test fixture'
            (root / 'LICENSE').write_bytes(data)
            manifest['files'].append({'source': 'LICENSE', 'path': 'LICENSE',
                                      'sha256': hashlib.sha256(data).hexdigest()})
            _, report = distribution.assemble(root, manifest)
            self.assertEqual(report['license_status'], 'MIT')
            self.assertEqual(report['publication_action'], 'not_performed_by_builder')
