import base64
import json
from pathlib import Path
import tempfile
import unittest

import httpx2
from nexus.artifacts import Access, ArtifactStore, LocalBinaryProvider
from nexus.server import build_server
from nexus.handoff import UI_URI, MAX_HANDOFF
from nexus.transfer_evidence import verify_outbound


class HandoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_wire_contract_hidden_bytes_and_exact_resource(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = ArtifactStore(root / "db", LocalBinaryProvider(root / "blobs"))
            data = b"synthetic transport bytes"
            store.register("small-png", "synthetic-probe", data, "image/png", ("owner",))
            access = Access("owner", "synthetic-probe")
            app = build_server(store, access).streamable_http_app(json_response=True, stateless_http=True)
            async with app.router.lifespan_context(app):
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app), base_url="http://127.0.0.1:8000") as client:
                    async def call(method, params):
                        request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
                        response = await client.post("/mcp", json=request, headers={"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"})
                        self.assertEqual(response.status_code, 200)
                        return response, request
                    listing, _ = await call("tools/list", {})
                    tool = next(t for t in listing.json()["result"]["tools"] if t["name"] == "prepare_original_handoff")
                    self.assertIn("sha256", tool["outputSchema"]["properties"])
                    self.assertEqual(tool["_meta"]["ui"]["resourceUri"], UI_URI)
                    response, request = await call("tools/call", {"name": tool["name"], "arguments": {"artifact_id": "small-png", "revision": 1}})
                    result = response.json()["result"]
                    self.assertEqual(base64.b64decode(result["_meta"]["original_base64"]), data)
                    self.assertNotIn("original_base64", json.dumps(result["content"]))
                    self.assertNotIn("original_base64", json.dumps(result["structuredContent"]))
                    receipt = verify_outbound({"id": "probe", "body": json.dumps(request)}, 200, response.content, store, access)
                    self.assertEqual(receipt["size"], len(data))
                    resource, _ = await call("resources/read", {"uri": UI_URI})
                    item = resource.json()["result"]["contents"][0]
                    self.assertEqual(item["mimeType"], "text/html;profile=mcp-app")
                    self.assertIn("ui/initialize", item["text"])
                    self.assertNotIn("/* NEXUS_SCRIPT */", item["text"])
                    self.assertEqual(item["_meta"]["ui"]["csp"]["connectDomains"], [])
                    for args in ({"artifact_id": "other", "revision": 1}, {"artifact_id": "small-png", "revision": 2}):
                        denied, _ = await call("tools/call", {"name": tool["name"], "arguments": args})
                        self.assertTrue("error" in denied.json() or denied.json().get("result", {}).get("isError"))
                        self.assertNotIn("original_base64", denied.text)

    async def test_scope_permission_size_and_missing_original(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = ArtifactStore(root / "db", LocalBinaryProvider(root / "blobs"))
            store.register("small-png", "synthetic-probe", b"x" * (MAX_HANDOFF + 1), "image/png", ("owner",))
            other = build_server(store, Access("owner", "private-project"))
            self.assertNotIn("prepare_original_handoff", [t.name for t in await other.list_tools()])
            for principal in ("owner", "intruder"):
                server = build_server(store, Access(principal, "synthetic-probe"))
                result = await server.call_tool("prepare_original_handoff", {"artifact_id": "small-png", "revision": 1})
                self.assertTrue(result.is_error)
                self.assertIsNone(result.meta)
