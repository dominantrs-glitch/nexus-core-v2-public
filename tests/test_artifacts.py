import base64
import hashlib
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zlib

from mcp import Client, StdioServerParameters

from nexus.artifacts import Access, AccessDenied, ArtifactStore, InsufficientContext, LocalBinaryProvider


def synthetic_png():
    """A small synthetic red/blue image; contains no personal data."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\x00\x00\xff")) + chunk(b"IEND", b""))


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.provider = LocalBinaryProvider(self.root / "blobs")
        self.store = ArtifactStore(self.root / "manifest.sqlite3", self.provider)
        self.data = synthetic_png()
        self.readers = ("chatgpt-probe", "implementation-probe")
        self.store.register("artifact-image", "synthetic", self.data, "image/png", self.readers)
        self.access = Access("implementation-probe", "synthetic")

    def test_original_and_provider_neutral_manifest(self):
        original = self.store.read("artifact-image", 1, self.access)
        self.assertEqual(original.data, self.data)
        self.assertEqual(original.sha256, hashlib.sha256(self.data).hexdigest())
        self.assertNotIn("locator", original.manifest())

    def test_two_revisions_preserve_identity_and_original(self):
        self.store.register("artifact-image", "synthetic", b"changed", "text/plain", self.readers, expected_revision=1)
        self.assertEqual(self.store.read("artifact-image", 1, self.access).data, self.data)
        newer = self.store.read("artifact-image", 2, self.access)
        self.assertEqual(newer.artifact_id, "artifact-image")
        self.assertEqual(newer.data, b"changed")

    def test_stale_writer_conflict(self):
        with self.assertRaisesRegex(ValueError, "revision conflict"):
            self.store.register("artifact-image", "synthetic", b"overwrite", "text/plain", self.readers)
        self.assertEqual(self.store.read("artifact-image", 1, self.access).data, self.data)

    def test_permission_and_scope_non_delivery(self):
        for access in (Access("intruder", "synthetic"), Access("implementation-probe", "other")):
            with self.subTest(access=access), self.assertRaises(AccessDenied):
                self.store.read("artifact-image", 1, access)

    def test_scope_and_permission_cannot_silently_change(self):
        for project, readers in (("other", self.readers), ("synthetic", ("intruder",))):
            with self.subTest(project=project), self.assertRaises(ValueError):
                self.store.register("artifact-image", project, b"new", "text/plain", readers, expected_revision=1)

    def test_missing_revision_never_falls_back(self):
        with self.assertRaises(InsufficientContext):
            self.store.read("artifact-image", 2, self.access)

    def test_missing_original_fails_closed(self):
        (self.root / "blobs" / hashlib.sha256(self.data).hexdigest()).unlink()
        with self.assertRaises(InsufficientContext):
            self.store.read("artifact-image", 1, self.access)

    def test_corrupt_original_fails_closed(self):
        (self.root / "blobs" / hashlib.sha256(self.data).hexdigest()).write_bytes(b"corrupt")
        with self.assertRaises(InsufficientContext):
            self.store.read("artifact-image", 1, self.access)

    def test_provider_rejects_traversal(self):
        for path in ("../private", "C:/private", "a" * 63, "Z" * 64):
            with self.subTest(path=path), self.assertRaises(InsufficientContext):
                self.provider.get(path)

    def test_reopen_persists_identity(self):
        reopened = ArtifactStore(self.root / "manifest.sqlite3", self.provider)
        self.assertEqual(reopened.read("artifact-image", 1, self.access).data, self.data)


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_processes_receive_identical_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = synthetic_png()
            store = ArtifactStore(root / "manifest.sqlite3", LocalBinaryProvider(root / "blobs"))
            store.register("image", "synthetic", data, "image/png", ("chatgpt-probe", "implementation-probe"))
            received = []
            for principal in ("chatgpt-probe", "implementation-probe", "denied-probe"):
                server = StdioServerParameters(command=sys.executable, args=[
                    "-m", "nexus.server", "--data", str(root), "--project", "synthetic", "--principal", principal],
                    cwd=str(Path(__file__).resolve().parents[1]))
                async with Client(server) as client:
                    result = await client.call_tool("read_original", {"artifact_id": "image", "revision": 1})
                    if principal == "denied-probe":
                        self.assertTrue(result.is_error)
                        self.assertFalse(any(item.type == "image" for item in result.content))
                        continue
                    self.assertFalse(result.is_error)
                    image = next(item for item in result.content if item.type == "image")
                    received.append(base64.b64decode(image.data))
                    missing = await client.call_tool("read_original", {"artifact_id": "image", "revision": 99})
                    self.assertTrue(missing.is_error)
                    self.assertFalse(any(item.type == "image" for item in missing.content))
            self.assertEqual(received, [data, data])


if __name__ == "__main__":
    unittest.main()
