import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { WebStandardStreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js";
import { z } from "zod";
import { authenticate, challenge, configuration } from "./auth";
import { base64, permitted, readOriginal, reserve } from "./store";
import { ProbeError, type Env, type Identity } from "./types";

function serverFor(env: Env, identity: Identity) {
  // A fresh server per HTTP request: no mutable cross-user session state.
  const server = new McpServer({ name: "nexus-core-v2-probe", version: "0.2.0" });
  server.registerTool("read_original", {
    description: "Read the exact permitted original and its manifest. An error means stop; do not substitute an older revision or an AI description.",
    inputSchema: z.object({
      artifact_id: z.string().min(1).max(100).regex(/^[a-zA-Z0-9_-]+$/),
      revision: z.number().int().min(1).max(1000000)
    }).strict(),
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
    _meta: { securitySchemes: [{ type: "oauth2", scopes: ["nexus:read"] }] }
  }, async ({ artifact_id, revision }) => {
    try {
      const { manifest, bytes, text } = await readOriginal(env, identity, artifact_id, revision);
      return {
        content: [
          { type: "text" as const, text: JSON.stringify(manifest) },
          ...(manifest.mime_type === "image/png"
            ? [{ type: "image" as const, data: base64(bytes), mimeType: "image/png" }]
            : [{ type: "text" as const, text: text! }])
        ],
        structuredContent: { ...manifest }
      };
    } catch (error) {
      const code = error instanceof ProbeError ? error.code : "insufficient_context";
      return { isError: true, content: [{ type: "text" as const, text: JSON.stringify({ status: code }) }] };
    }
  });
  return server;
}

async function limitedJSON(request: Request): Promise<unknown> {
  if (!request.body) throw new Error("missing body");
  const reader = request.body.getReader();
  const parts: Uint8Array[] = [];
  let size = 0;
  try {
    for (;;) {
      const item = await reader.read();
      if (item.done) break;
      size += item.value.byteLength;
      if (size > 16384) { await reader.cancel(); throw new Error("request too large"); }
      parts.push(item.value);
    }
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const part of parts) { bytes.set(part, offset); offset += part.length; }
  return JSON.parse(new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes));
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const traceId = crypto.randomUUID();
    try {
      const { issuer, resource } = configuration(env);
      const url = new URL(request.url);
      if (url.origin !== resource.origin) return new Response("Not found", { status: 404 });
      const origin = request.headers.get("Origin");
      // Server-to-server MCP needs no browser Origin. Same-origin only otherwise.
      if (origin && origin !== resource.origin) return new Response("Forbidden", { status: 403 });
      if (env.PROBE_ENABLED !== "true") return new Response("Probe disabled", { status: 503 });
      if (request.method === "GET" && [
        "/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"
      ].includes(url.pathname)) return Response.json({
        resource: resource.href, authorization_servers: [issuer],
        scopes_supported: ["nexus:read"], bearer_methods_supported: ["header"]
      }, { headers: { "Cache-Control": "no-store" } });
      if (url.pathname !== "/mcp" || url.search) return new Response("Not found", { status: 404 });
      const identity = await authenticate(request, env);
      if (!identity) return challenge(env);
      if (!await permitted(env, identity)) return Response.json({ error: "not_accessible" }, { status: 403 });
      await reserve(env, 0, 1);
      if (request.method !== "POST") return new Response(null, {
        status: 405, headers: { Allow: "POST" }
      });
      if (!request.headers.get("Content-Type")?.toLowerCase().startsWith("application/json"))
        return new Response("JSON required", { status: 415 });
      let body: unknown;
      try { body = await limitedJSON(request); }
      catch { return new Response("Invalid or oversized request", { status: 400 }); }
      const server = serverFor(env, identity);
      const transport = new WebStandardStreamableHTTPServerTransport({
        sessionIdGenerator: undefined, enableJsonResponse: true
      });
      try {
        await server.connect(transport);
        const result = await transport.handleRequest(request, { parsedBody: body });
        const headers = new Headers(result.headers);
        headers.set("Cache-Control", "no-store");
        headers.set("X-Nexus-Trace", traceId);
        return new Response(result.body, { status: result.status, headers });
      } finally {
        await server.close();
      }
    } catch (error) {
      return Response.json({
        error: error instanceof ProbeError ? error.code : "service_unavailable",
        trace_id: traceId
      }, { status: error instanceof ProbeError && error.code === "budget_exhausted" ? 429 : 503,
        headers: { "Cache-Control": "no-store" } });
    }
  }
} satisfies ExportedHandler<Env>;
