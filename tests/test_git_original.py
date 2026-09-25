import hashlib
import base64
import json
from pathlib import Path
import tempfile
import unittest

from nexus.git_original import prepare, MAX_BINARY_BYTES


class GitOriginalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / 'original.md'
        self.raw = '\ufeff# 架空の条件\r\n本人確認と原資料は別です。\r\n'.encode('utf-8')
        self.file.write_bytes(self.raw)
        self.spec = dict(owner='owner', generation='test', mode='synthetic', project='target', documents=[
            dict(id='rules', revision=1, file=str(self.file), title='架空条件', source='synthetic fixture',
                 media_type='text/markdown', projects=['target'], operations=['resume'])])
        self.destination = self.root / 'stage'

    def test_original_bytes_and_non_authority_survive_independent_json_read(self):
        report = prepare(self.spec, self.destination)
        record = json.loads((self.destination / report['documents'][0]['file']).read_bytes())
        self.assertEqual(record['content'].encode('utf-8'), self.raw)
        self.assertEqual(record['sha256'], hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(record['authority'], 'source-document-not-native-confirmation')
        self.assertEqual(self.file.read_bytes(), self.raw)
        self.assertFalse(report['uploaded'])
        self.assertFalse(report['transfer_authorized'])
        with self.assertRaises(FileExistsError):
            prepare(self.spec, self.destination)

    def test_invalid_encoding_size_secret_and_scope_do_not_create_package(self):
        for raw in [b'\xff', b'x' * 24577, b'-----BEGIN PRIVATE KEY-----', b'   ']:
            with self.subTest(raw=raw[:12]):
                self.file.write_bytes(raw)
                with self.assertRaises((ValueError, UnicodeError)):
                    prepare(self.spec, self.destination)
                self.assertFalse(self.destination.exists())
        self.file.write_bytes(self.raw)
        self.spec['documents'][0]['projects'] = ['*']
        with self.assertRaises(ValueError):
            prepare(self.spec, self.destination)
        self.assertFalse(self.destination.exists())

    def test_late_invalid_selection_does_not_leave_partial_package(self):
        self.spec['documents'].append({**self.spec['documents'][0], 'id': '../escape'})
        with self.assertRaises(ValueError):
            prepare(self.spec, self.destination)
        self.assertFalse(self.destination.exists())

    def test_binary_preparation_keeps_exact_bytes_and_requires_separate_transfer_review(self):
        raw = b'%PDF-1.4\nsynthetic-byte-transport-fixture'
        self.file.write_bytes(raw)
        self.spec['documents'][0]['media_type'] = 'application/pdf'
        report = prepare(self.spec, self.destination, binary=True)
        record = json.loads((self.destination / report['documents'][0]['file']).read_bytes())
        self.assertEqual(base64.b64decode(record['content'], validate=True), raw)
        self.assertEqual(record['bytes'], len(raw))
        self.assertFalse(report['transfer_authorized'])
        self.assertEqual(report['secret_scan'], 'binary-not-scanned-review-required')

    def test_binary_limits_and_declared_media_are_checked_before_staging(self):
        self.spec['documents'][0]['media_type'] = 'application/pdf'
        for raw in (b'not a PDF', b'%PDF-' + b'x' * MAX_BINARY_BYTES):
            self.file.write_bytes(raw)
            with self.assertRaises(ValueError):
                prepare(self.spec, self.destination, binary=True)
            self.assertFalse(self.destination.exists())

    def test_large_binary_is_chunked_and_all_staged_bytes_reassemble_exactly(self):
        raw = b'%PDF-' + bytes(range(256)) * 4200
        self.file.write_bytes(raw)
        self.spec['documents'][0]['media_type'] = 'application/pdf'
        report = prepare(self.spec, self.destination, binary=True)
        record = json.loads((self.destination / report['documents'][0]['file']).read_bytes())
        self.assertEqual(record['schema'], 2)
        self.assertNotIn('content', record)
        restored = b''
        for ref in record['chunks']:
            path = self.destination / f"projects/target/binary/chunk-{ref['sha256']}/1.json"
            self.assertLess(path.stat().st_size, 300000)
            restored += base64.b64decode(json.loads(path.read_bytes())['content'], validate=True)
        self.assertEqual(restored, raw)
        self.assertEqual(record['sha256'], hashlib.sha256(raw).hexdigest())

    def test_webp_requires_riff_and_webp_headers(self):
        self.spec['documents'][0]['media_type'] = 'image/webp'
        self.file.write_bytes(b'RIFF0000FAKEbytes')
        with self.assertRaises(ValueError):prepare(self.spec,self.destination,binary=True)
        self.file.write_bytes(b'RIFF0000WEBPbytes')
        result=prepare(self.spec,self.destination,binary=True)
        self.assertEqual(result['documents'][0]['bytes'],17)
