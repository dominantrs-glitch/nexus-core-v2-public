import io
import asyncio
from contextlib import asynccontextmanager
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx2

from nexus.artifacts import Access, AccessDenied, ArtifactStore, LocalBinaryProvider, InsufficientContext
from nexus.backup import export_bundle, restore_bundle
from nexus.connector import local_response, validate_job, run
from nexus.server import build_server
from nexus.fixtures import write_png


class BackupTests(unittest.TestCase):
    def test_export_restore_preserves_versions_permissions_and_exact_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ArtifactStore(root / "manifest.sqlite3", LocalBinaryProvider(root / "blobs"))
            store.register("a", "p", b"first", "text/plain", ("owner",))
            store.register("a", "p", b"second", "text/plain", ("owner",), expected_revision=1)
            self.assertEqual(export_bundle(store, root / "backup"), 2)
            self.assertEqual(restore_bundle(root / "backup", root / "restored"), 2)
            restored = ArtifactStore(root / "restored/manifest.sqlite3", LocalBinaryProvider(root / "restored/blobs"))
            self.assertEqual(restored.read("a", 1, Access("owner", "p")).data, b"first")
            self.assertEqual(restored.read("a", 2, Access("owner", "p")).data, b"second")
            with self.assertRaises(AccessDenied):
                restored.read("a", 1, Access("intruder", "p"))
            with self.assertRaises(FileExistsError):
                restore_bundle(root / "backup", root / "restored")
            next((root / "backup/blobs").iterdir()).write_bytes(b"broken")
            with self.assertRaises(InsufficientContext):
                restore_bundle(root / "backup", root / "bad-restore")
            self.assertFalse((root / "bad-restore/manifest.sqlite3").exists())


class ConnectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_connector_loop_dispatches_sdk_and_authenticated_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ArtifactStore(root / "manifest.sqlite3", LocalBinaryProvider(root / "blobs"))
            store.register("a", "synthetic-probe", b"connector-original", "text/plain", ("remote-owner",))
            job = {"id": "00000000-0000-4000-8000-000000000001", "project": "synthetic-probe",
                   "protocol": "2025-11-25", "body": json.dumps({"jsonrpc": "2.0", "id": 1,
                   "method": "tools/call", "params": {"name": "read_original",
                   "arguments": {"artifact_id": "a", "revision": 1}}})}
            replies = []
            class Socket:
                delivered = False
                async def recv(self):
                    if self.delivered:
                        raise asyncio.CancelledError
                    self.delivered = True
                    return json.dumps(job)
                async def send(self, value):
                    pass
            @asynccontextmanager
            async def fake_connect(url, **kwargs):
                self.assertEqual(url, "wss://relay.example/connect")
                self.assertEqual(kwargs["additional_headers"]["Authorization"], "Bearer " + "a" * 43)
                yield Socket()
            def reply(request):
                self.assertEqual(str(request.url), "https://relay.example/reply/" + job["id"])
                self.assertEqual(request.headers["Authorization"], "Bearer " + "a" * 43)
                self.assertEqual(request.headers["X-Nexus-Status"], "200")
                replies.append(json.loads(request.content))
                return httpx2.Response(204)
            client_class = httpx2.AsyncClient
            def client(**kwargs):
                if "transport" not in kwargs:
                    kwargs["transport"] = httpx2.MockTransport(reply)
                return client_class(**kwargs)
            with patch("nexus.connector.connect", fake_connect), patch("nexus.connector.httpx2.AsyncClient", client):
                with self.assertRaises(asyncio.CancelledError):
                    await run("https://relay.example", "a" * 43, root, "synthetic-probe", "remote-owner")
            self.assertEqual(replies[0]["result"]["content"][1]["text"], "connector-original")

    async def test_malformed_relay_fields_fail_closed(self):
        job = {"id": "00000000-0000-4000-8000-000000000001", "project": "synthetic-probe",
               "protocol": "2025-11-25", "body": "{}"}
        for key in job:
            for invalid in (None, 7, {}, []):
                with self.subTest(key=key, invalid=invalid), self.assertRaises(ValueError):
                    validate_job({**job, key: invalid}, "synthetic-probe")
        with self.assertRaises(ValueError):
            build_server(None, None, inline_limit=65 * 1024 * 1024)

    async def test_large_png_survives_real_sdk_http_serialization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "large.png"
            write_png(image, 3072, 3072, 0)
            store = ArtifactStore(root / "manifest.sqlite3", LocalBinaryProvider(root / "blobs"))
            with image.open("rb") as source:
                store.register_stream("large", "synthetic-probe", source, "image/png", ("remote-owner",))
            self.assertGreater(image.stat().st_size, 25 * 1024 * 1024)
            app = build_server(store, Access("remote-owner", "synthetic-probe"), inline_limit=64 * 1024 * 1024).streamable_http_app(
                json_response=True, stateless_http=True)
            async with app.router.lifespan_context(app):
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app), base_url="http://127.0.0.1:8000") as client:
                    job = {"body": json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "read_original", "arguments": {"artifact_id": "large", "revision": 1}}}),
                           "protocol": "2025-11-25"}
                    status, body = await local_response(client, job)
                    self.assertEqual(status, 200)
                    result = json.loads(body)["result"]
                    data = base64.b64decode(result["content"][1]["data"], validate=True)
                    with image.open("rb") as source:
                        self.assertEqual(hashlib.sha256(data).digest(), hashlib.file_digest(source, "sha256").digest())
                    self.assertFalse(result.get("isError"))

    async def test_pc_sdk_http_response_and_scope_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ArtifactStore(root / "manifest.sqlite3", LocalBinaryProvider(root / "blobs"))
            store.register("a", "synthetic-probe", b"verified original", "text/plain", ("remote-owner",))
            app = build_server(store, Access("remote-owner", "synthetic-probe")).streamable_http_app(
                json_response=True, stateless_http=True)
            async with app.router.lifespan_context(app):
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app), base_url="http://127.0.0.1:8000") as client:
                    job = {"id": "00000000-0000-4000-8000-000000000001", "project": "synthetic-probe", "protocol": "2025-11-25",
                           "body": json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "read_original", "arguments": {"artifact_id": "a", "revision": 1}}})}
                    validate_job(job, "synthetic-probe")
                    status, body = await local_response(client, job)
                    self.assertEqual(status, 200)
                    result = json.loads(body)["result"]
                    self.assertEqual(result["content"][1]["text"], "verified original")
                    with self.assertRaises(ValueError):
                        validate_job(job, "other-project")
