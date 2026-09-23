"""Read-only local MCP adapter. Remote deployment/auth is intentionally gated."""
import argparse
import base64
import json
from pathlib import Path

from mcp import types
from mcp.server import MCPServer

from nexus.artifacts import Access, AccessDenied, ArtifactStore, InsufficientContext, LocalBinaryProvider, MAX_INLINE_BYTES, MAX_ORIGINAL_BYTES
from nexus.handoff import register_handoff


def build_server(store: ArtifactStore, access: Access, *, inline_limit: int = MAX_INLINE_BYTES) -> MCPServer:
    if not isinstance(inline_limit, int) or not 1 <= inline_limit <= MAX_ORIGINAL_BYTES:
        raise ValueError("inline limit must be between 1 byte and 64 MiB")
    server = MCPServer("nexus-core-v2-artifact-probe", version="0.1.0")

    @server.tool(annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
    def read_original(artifact_id: str, revision: int) -> types.CallToolResult:
        """Retrieve a permitted original at an exact revision, with integrity proof.

        Use the returned image attachment directly. Never transcribe Base64,
        recreate the image in code, generate a replacement, or enable tolerant
        decoding of corrupt/truncated images. If the attachment cannot be seen
        or opened directly, report insufficient_context and stop inspection.
        Metadata is not a substitute for the image. The manifest hash proves
        server-side integrity only; do not claim a client-side hash check unless
        a program actually checks the original received bytes without retyping.
        An error means required context is unavailable; do not substitute metadata.
        """
        try:
            original = store.read(artifact_id, revision, access, max_bytes=inline_limit)
        except (AccessDenied, InsufficientContext) as exc:
            code = "not_accessible" if isinstance(exc, AccessDenied) else "insufficient_context"
            return types.CallToolResult(isError=True, content=[
                types.TextContent(text=json.dumps({"status": code}))])
        manifest = original.manifest()
        content = [types.TextContent(text=json.dumps(manifest, ensure_ascii=False))]
        if original.mime_type == "image/png":
            content.append(types.ImageContent(data=base64.b64encode(original.data).decode("ascii"),
                                              mimeType=original.mime_type))
        elif original.mime_type == "text/plain":
            content.append(types.TextContent(text=original.data.decode("utf-8")))
        else:
            content.append(types.EmbeddedResource(resource=types.BlobResourceContents(
                uri=f"nexus://original/{original.sha256}", mimeType=original.mime_type,
                blob=base64.b64encode(original.data).decode("ascii"))))
        return types.CallToolResult(content=content, structuredContent=manifest)

    register_handoff(server, store, access)
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--principal", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--inline-limit-mib", type=int, choices=range(1, 65), default=4,
                        metavar="1..64", help="trusted launcher response limit; default 4 MiB")
    args = parser.parse_args()
    # The process identity is configured by the trusted launcher, not tool arguments.
    if not (args.data / "manifest.sqlite3").is_file():
        parser.error("existing artifact manifest required")
    store = ArtifactStore(args.data / "manifest.sqlite3", LocalBinaryProvider(args.data / "blobs"))
    build_server(store, Access(args.principal, args.project),
                 inline_limit=args.inline_limit_mib * 1024 * 1024).run(transport="stdio")


if __name__ == "__main__":
    main()
