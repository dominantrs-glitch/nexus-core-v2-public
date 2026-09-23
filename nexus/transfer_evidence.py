"""Opt-in, local synthetic-probe evidence. Never records original contents."""
import base64
from datetime import datetime, timezone
import hashlib
import json

MAX_LOG_BYTES = 1024 * 1024


def verify_outbound(job, status, body, store, access):
    request = json.loads(job["body"])
    if request.get("method") != "tools/call" or status != 200:
        return None
    if not access.project.startswith("synthetic-"):
        raise ValueError("synthetic evidence only")
    params = request.get("params", {})
    if params.get("name") not in ("read_original", "prepare_original_handoff"):
        return None
    result = json.loads(body).get("result", {})
    if result.get("isError"):
        return None
    args = params["arguments"]
    expected = store.describe(args["artifact_id"], args["revision"], access)
    if result.get("structuredContent") != expected:
        raise ValueError("outbound manifest mismatch")
    content = result["content"]
    if json.loads(content[0]["text"]) != expected:
        raise ValueError("outbound content mismatch")
    mime = expected["mime_type"]
    if params["name"] == "prepare_original_handoff":
        if len(content) != 1 or mime != "image/png" or expected["size"] > 128 * 1024:
            raise ValueError("outbound handoff mismatch")
        data = base64.b64decode(result["_meta"]["original_base64"], validate=True)
    elif len(content) != 2:
        raise ValueError("outbound content mismatch")
    elif mime == "image/png" and (payload := content[1]).get("type") == "image" and payload.get("mimeType") == mime:
        data = base64.b64decode(payload["data"], validate=True)
    elif mime == "text/plain" and (payload := content[1]).get("type") == "text":
        data = payload["text"].encode("utf-8")
    elif (payload := content[1]).get("type") == "resource" and payload.get("resource", {}).get("mimeType") == mime:
        data = base64.b64decode(payload["resource"]["blob"], validate=True)
    else:
        raise ValueError("outbound payload type mismatch")
    if len(data) != expected["size"] or hashlib.sha256(data).hexdigest() != expected["sha256"]:
        raise ValueError("outbound bytes mismatch")
    return {"transfer_id": job["id"], "artifact_id": expected["artifact_id"],
            "revision": expected["revision"], "size": len(data), "sha256": expected["sha256"]}


def record(path, receipt, stage):
    if stage not in ("outbound_verified", "relay_accepted"):
        raise ValueError("unsupported evidence stage")
    entry = {**receipt, "stage": stage, "receiver_verified": False,
             "time": datetime.now(timezone.utc).isoformat()}
    line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
    if (path.stat().st_size if path.exists() else 0) + len(line) > MAX_LOG_BYTES:
        raise ValueError("evidence log full")
    with path.open("ab") as stream:
        stream.write(line)
