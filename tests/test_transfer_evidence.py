import base64
import json
from pathlib import Path
import tempfile
import unittest

from nexus.artifacts import Access, ArtifactStore, LocalBinaryProvider
from nexus.transfer_evidence import verify_outbound, record, MAX_LOG_BYTES


class TransferEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ArtifactStore(self.root / "db.sqlite3", LocalBinaryProvider(self.root / "blobs"))
        self.access = Access("owner", "synthetic-evidence")
        self.data = b"synthetic original bytes"
        self.store.register("a", self.access.project, self.data, "image/png", ("owner",))
        manifest = self.store.describe("a", 1, self.access)
        self.job = {"id": "00000000-0000-4000-8000-000000000001", "body": json.dumps({"method": "tools/call", "params": {"name": "read_original", "arguments": {"artifact_id": "a", "revision": 1}}})}
        self.result = {"result": {"structuredContent": manifest, "content": [{"type": "text", "text": json.dumps(manifest)}, {"type": "image", "mimeType": "image/png", "data": base64.b64encode(self.data).decode()}]}}

    def verify(self):
        return verify_outbound(self.job, 200, json.dumps(self.result).encode(), self.store, self.access)

    def test_verified_bytes_and_minimal_stage_records(self):
        receipt = self.verify()
        path = self.root / "evidence.jsonl"
        for stage in ("outbound_verified", "relay_accepted"):
            record(path, receipt, stage)
        entries = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual([x["stage"] for x in entries], ["outbound_verified", "relay_accepted"])
        self.assertTrue(all(x["receiver_verified"] is False for x in entries))
        self.assertNotIn(base64.b64encode(self.data).decode(), path.read_text())
        self.assertEqual(set(entries[0]), {"transfer_id", "artifact_id", "revision", "size", "sha256", "stage", "receiver_verified", "time"})

    def test_payload_tampering_and_manifest_substitution_rejected(self):
        self.result["result"]["content"][1]["data"] = base64.b64encode(b"changed").decode()
        with self.assertRaisesRegex(ValueError, "bytes mismatch"):
            self.verify()
        self.result["result"]["structuredContent"]["revision"] = 2
        with self.assertRaisesRegex(ValueError, "manifest mismatch"):
            self.verify()

    def test_errors_have_no_success_receipt_and_log_limit_preserves_file(self):
        self.result = {"result": {"isError": True, "content": []}}
        self.assertIsNone(self.verify())
        path = self.root / "evidence.jsonl"
        path.write_bytes(b"x" * MAX_LOG_BYTES)
        with self.assertRaisesRegex(ValueError, "log full"):
            record(path, {}, "outbound_verified")
        self.assertEqual(path.stat().st_size, MAX_LOG_BYTES)
