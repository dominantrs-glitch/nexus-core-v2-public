"""Bounded, synthetic-only ChatGPT file handoff capability probe."""
import base64
import json
from pathlib import Path
from typing import Annotated, Literal, TypedDict

from mcp import types
from nexus.artifacts import AccessDenied, InsufficientContext

UI_URI = "ui://nexus/original-handoff-v5.html"
MAX_HANDOFF = 128 * 1024


class Manifest(TypedDict):
    artifact_id: str
    revision: int
    project: str
    mime_type: str
    sha256: str
    size: int


def register_handoff(server, store, access):
    # No new production-data surface. Trusted launch identity still governs reads.
    if access.project != "synthetic-probe":
        return

    @server.resource(UI_URI, name="Nexus original handoff probe",
        mime_type="text/html;profile=mcp-app",
        meta={"ui": {"prefersBorder": True, "csp": {
            "connectDomains": [], "resourceDomains": []}},
            "openai/widgetDescription": "Verifies a synthetic original, then probes model context delivery with explicit actions."})
    def handoff_ui() -> str:
        folder = Path(__file__).parent / "ui"
        return (folder / "model-context.html").read_text(encoding="utf-8").replace(
            "/* NEXUS_SCRIPT */", (folder / "model-context.js").read_text(encoding="utf-8"))

    @server.tool(title="Open original handoff probe",
        annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
        meta={"ui": {"resourceUri": UI_URI}, "openai/outputTemplate": UI_URI,
              "openai/toolInvocation/invoking": "合成原本を照合しています",
              "openai/toolInvocation/invoked": "受渡し試験画面を開きました"})
    def prepare_original_handoff(artifact_id: Literal["small-png"], revision: Literal[1]) -> Annotated[types.CallToolResult, Manifest]:
        """Open the small synthetic original file handoff UI at revision 1.

        The UI verifies actual bytes and requires explicit actions for context probes.
        Tool metadata alone does not mean ChatGPT received or saw the image.
        Do not reconstruct the file or infer its visual content from metadata.
        """
        try:
            original = store.read(artifact_id, revision, access, max_bytes=MAX_HANDOFF)
            if original.mime_type != "image/png":
                raise InsufficientContext("wrong probe type")
        except (AccessDenied, InsufficientContext) as exc:
            return types.CallToolResult(isError=True, content=[types.TextContent(text=json.dumps({
                "status": "not_accessible" if isinstance(exc, AccessDenied) else "insufficient_context"}))])
        manifest = original.manifest()
        return types.CallToolResult(structuredContent=manifest,
            content=[types.TextContent(text=json.dumps(manifest))],
            _meta={"original_base64": base64.b64encode(original.data).decode("ascii")})
