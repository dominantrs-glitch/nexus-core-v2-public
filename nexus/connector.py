"""Outbound-only synthetic relay connector. No TCP listener or arbitrary file RPC."""
import argparse
import asyncio
from collections import deque
import json
import os
from pathlib import Path
import re
from urllib.parse import urlparse
from uuid import UUID

import httpx2
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from nexus.artifacts import Access, ArtifactStore, LocalBinaryProvider, MAX_ORIGINAL_BYTES
from nexus.server import build_server
from nexus.transfer_evidence import verify_outbound, record

MAX_RESPONSE = 96 * 1024 * 1024


def validate_job(value, project):
    if not isinstance(value, dict) or set(value) != {"id", "body", "project", "protocol"}:
        raise ValueError("invalid relay message")
    if any(not isinstance(value[key], str) for key in value):
        raise ValueError("relay fields must be strings")
    if str(UUID(value["id"])) != value["id"] or value["project"] != project:
        raise ValueError("wrong job scope")
    if not isinstance(value["body"], str) or len(value["body"].encode("utf-8")) > 16384:
        raise ValueError("request limit")
    if value["protocol"] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value["protocol"]):
        raise ValueError("invalid protocol version")
    return value


async def local_response(app_client, job):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if job["protocol"]:
        headers["MCP-Protocol-Version"] = job["protocol"]
    response = await app_client.post("/mcp", content=job["body"].encode("utf-8"), headers=headers)
    if len(response.content) > MAX_RESPONSE:
        return 503, b'{"error":"response_limit"}'
    return response.status_code, response.content


async def run(origin, token, data, project, principal, evidence_path=None):
    parsed = urlparse(origin)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("canonical HTTPS relay origin required")
    if not project.startswith("synthetic-"):
        raise ValueError("this increment only enables synthetic projects")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token):
        raise ValueError("connector token must be a generated 256-bit or stronger value")
    if not (data / "manifest.sqlite3").is_file():
        raise ValueError("existing synthetic manifest required")
    origin = origin.rstrip("/")
    store = ArtifactStore(data / "manifest.sqlite3", LocalBinaryProvider(data / "blobs"))
    app = build_server(store, Access(principal, project), inline_limit=MAX_ORIGINAL_BYTES).streamable_http_app(
        json_response=True, stateless_http=True, max_request_body_size=16384)
    await relay_app(origin, token, project, app, store=store, principal=principal, evidence_path=evidence_path)


async def relay_app(origin, token, project, app, *, store=None, principal=None, evidence_path=None, on_state=None):
    """Trusted transport helper. A caller must authorize its own data projection.

    The public synthetic launcher above retains its original project restriction.
    This helper never chooses files, projects or an MCP server from remote input.
    """
    parsed = urlparse(origin)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("canonical HTTPS relay origin required")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token):
        raise ValueError("connector token must be a generated 256-bit or stronger value")
    if not isinstance(project, str) or not project:
        raise ValueError("explicit relay project required")
    if evidence_path is not None and (store is None or principal is None):
        raise ValueError("original transfer evidence requires an artifact store")
    origin = origin.rstrip("/")
    seen = deque(maxlen=1000)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as local:
            async with httpx2.AsyncClient(timeout=40, follow_redirects=False, trust_env=False,
                                         headers={"User-Agent": "Nexus-Core-V2-Probe/0.1"}) as remote:
                from nexus.recovery import ReconnectBackoff
                retry = ReconnectBackoff()
                while True:
                    try:
                        async with connect("wss://" + parsed.netloc + "/connect",
                            additional_headers={"Authorization": "Bearer " + token},
                            max_size=65536, ping_interval=None, open_timeout=15, proxy=None,
                            user_agent_header="Nexus-Core-V2-Probe/0.1") as socket:
                            print("Scoped relay connected; no local port opened.", flush=True)
                            if on_state:
                                on_state("connected")
                            connected_at = asyncio.get_running_loop().time()
                            async def heartbeat():
                                while True:
                                    await asyncio.sleep(20)
                                    await socket.send("ping")
                            task = asyncio.create_task(heartbeat())
                            try:
                                while True:
                                    message = await asyncio.wait_for(socket.recv(), timeout=65)
                                    if message == "pong":
                                        if asyncio.get_running_loop().time() - connected_at >= 60:
                                            retry.healthy()
                                        continue
                                    job = validate_job(json.loads(message), project)
                                    if job["id"] in seen:
                                        raise ValueError("replayed request")
                                    seen.append(job["id"])
                                    status, body = await asyncio.wait_for(local_response(local, job), timeout=35)
                                    receipt = None
                                    if evidence_path is not None:
                                        receipt = verify_outbound(job, status, body, store, Access(principal, project))
                                        if receipt:
                                            record(evidence_path, receipt, "outbound_verified")
                                    result = await remote.post(origin + "/reply/" + job["id"], content=body,
                                        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                                                 "X-Nexus-Status": str(status)})
                                    if result.status_code != 204:
                                        raise ValueError("relay rejected response")
                                    retry.healthy()
                                    if receipt:
                                        record(evidence_path, receipt, "relay_accepted")
                            finally:
                                task.cancel()
                                await asyncio.gather(task, return_exceptions=True)
                    except (OSError, TimeoutError, ValueError, httpx2.HTTPError, WebSocketException):
                        state, delay = retry.failed()
                        if on_state:
                            on_state(state)
                        # Never log URLs, bearer tokens, data, or request bodies.
                        print(f"Relay unavailable; reconnect in {delay}s.", flush=True)
                        await asyncio.sleep(delay)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--project", default="synthetic-probe")
    parser.add_argument("--principal", default="remote-owner")
    parser.add_argument("--evidence-log", type=Path, help="opt-in local synthetic transfer evidence (no contents; max 1 MiB)")
    args = parser.parse_args()
    token = os.environ.get("NEXUS_CONNECTOR_TOKEN", "")
    asyncio.run(run(args.origin, token, args.data, args.project, args.principal, args.evidence_log))


if __name__ == "__main__":
    main()
