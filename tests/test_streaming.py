import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from nexus.artifacts import (Access, AccessDenied, ArtifactStore, CHUNK_BYTES,
                             InsufficientContext, LocalBinaryProvider, MAX_ORIGINAL_BYTES)


class GeneratedStream:
    def __init__(self, size, fail=False):
        self.remaining = size
        self.calls = 0
        self.fail = fail

    def read(self, count):
        assert 0 < count <= CHUNK_BYTES, "unbounded read"
        self.calls += 1
        if self.fail and self.calls == 2:
            raise OSError("synthetic interrupted input")
        count = min(count, self.remaining)
        self.remaining -= count
        return b"x" * count


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.provider = LocalBinaryProvider(self.root / "blobs")
        self.store = ArtifactStore(self.root / "manifest.sqlite3", self.provider)
        self.access = Access("owner", "synthetic")

    def register(self, source, **kwargs):
        return self.store.register_stream("large", "synthetic", source, "text/plain", ("owner",), **kwargs)

    def test_64_mib_round_trip_without_unbounded_reads(self):
        source = GeneratedStream(MAX_ORIGINAL_BYTES)
        self.register(source)
        digest = hashlib.sha256()
        for _ in range(64):
            digest.update(b"x" * CHUNK_BYTES)
        with self.store.open_verified("large", 1, self.access) as (manifest, stream):
            self.assertEqual(manifest["size"], MAX_ORIGINAL_BYTES)
            self.assertEqual(hashlib.file_digest(stream, "sha256").hexdigest(), digest.hexdigest())
        self.assertGreater(source.calls, 1)
        with self.assertRaisesRegex(InsufficientContext, "inline"):
            self.store.read("large", 1, self.access)

    def test_size_limit_and_interrupted_write_never_publish(self):
        for source, error in [(GeneratedStream(MAX_ORIGINAL_BYTES + 1), ValueError),
                              (GeneratedStream(CHUNK_BYTES * 2, fail=True), OSError)]:
            with self.subTest(error=error), self.assertRaises(error):
                self.register(source)
            self.assertEqual(list((self.root / "blobs").iterdir()), [])
            with self.assertRaises(AccessDenied):
                self.store.describe("large", 1, self.access)

    def test_storage_budget_preserves_existing_original(self):
        self.store = ArtifactStore(self.root / "manifest.sqlite3", self.provider, storage_bytes=8)
        self.register(io.BytesIO(b"12345"))
        with self.assertRaisesRegex(ValueError, "budget"):
            self.register(io.BytesIO(b"6789"), expected_revision=1)
        self.assertEqual(self.store.read("large", 1, self.access).data, b"12345")
        with self.assertRaises(InsufficientContext):
            self.store.describe("large", 2, self.access)

    def test_invalid_utf8_across_chunks_never_commits(self):
        with self.assertRaises(UnicodeDecodeError):
            self.register(io.BytesIO(b"a" * (CHUNK_BYTES-1) + b"\xe3\x81"))
        self.assertEqual(list((self.root / "blobs").iterdir()), [])

    def test_no_partial_read_on_corruption_and_snapshot_stays_verified(self):
        self.register(io.BytesIO(b"original"))
        blob = next((self.root / "blobs").iterdir())
        with self.store.open_verified("large", 1, self.access) as (_, snapshot):
            blob.write_bytes(b"modified")
            self.assertEqual(snapshot.read(), b"original")
        with self.assertRaises(InsufficientContext):
            with self.store.open_verified("large", 1, self.access):
                self.fail("corrupt original released")

    def test_denied_reader_does_not_open_provider(self):
        self.register(io.BytesIO(b"secret"))
        with patch.object(self.provider, "open", side_effect=AssertionError("opened before auth")):
            with self.assertRaises(AccessDenied):
                with self.store.open_verified("large", 1, Access("other", "synthetic")):
                    self.fail()

    def test_concurrent_writers_commit_only_one_revision(self):
        def write(value):
            try:
                return self.register(io.BytesIO(value))
            except ValueError:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, [b"first", b"second"]))
        self.assertCountEqual(results, [1, "conflict"])
        self.assertIn(self.store.read("large", 1, self.access).data, [b"first", b"second"])

    def test_reupload_cannot_replace_corrupt_immutable_blob(self):
        self.register(io.BytesIO(b"original"))
        next((self.root / "blobs").iterdir()).write_bytes(b"corrupt!")
        with self.assertRaises(InsufficientContext):
            self.register(io.BytesIO(b"original"), expected_revision=1)
        with self.assertRaises(InsufficientContext):
            self.store.describe("large", 2, self.access)
