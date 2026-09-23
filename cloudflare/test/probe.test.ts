import { env as bindings } from "cloudflare:workers";
import { applyD1Migrations, type D1Migration } from "cloudflare:test";
import { beforeAll, beforeEach, afterEach, describe, it, expect, vi } from "vitest";
import { exportJWK, generateKeyPair, SignJWT } from "jose";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import worker from "../src/index";
import { base64, sha256, reserve } from "../src/store";
import { DAILY_RESPONSE_BYTES, type Env } from "../src/types";

const raw = bindings as unknown as Env & { TEST_MIGRATIONS: D1Migration[] };
const issuer = "https://issuer.invalid/";
const resource = "https://nexus.invalid/mcp";
let env: Env;
let key: CryptoKey;
let publicJWK: Record<string, unknown>;
let artifact: string;
const png = Uint8Array.from(atob("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLttAAAAABJRU5ErkJggg=="), c => c.charCodeAt(0));
let digest: string;

async function token(options: {
  subject?: string; client?: string; scope?: string; issuer?: string;
  audience?: string; expires?: number; key?: CryptoKey; omitExpiry?: boolean
} = {}) {
  let jwt = new SignJWT({
    azp: options.client ?? "client-a", scope: options.scope ?? "nexus:read"
  }).setProtectedHeader({ alg: "RS256", kid: "probe-test-key" })
    .setIssuer(options.issuer ?? issuer).setSubject(options.subject ?? "owner")
    .setAudience(options.audience ?? resource).setIssuedAt();
  if (!options.omitExpiry) jwt = jwt.setExpirationTime(
    options.expires ?? Math.floor(Date.now() / 1000) + 300);
  return jwt.sign(options.key ?? key);
}
async function rpc(bearer: string, method = "tools/call", args: Record<string, unknown> = {
  name: "read_original", arguments: { artifact_id: artifact, revision: 1 }
}) {
  return worker.fetch(new Request(resource, {
    method: "POST",
    headers: { Authorization: "Bearer " + bearer, "Content-Type": "application/json",
      Accept: "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params: args })
  }), env);
}
async function result(response: Response): Promise<any> { return response.json(); }

beforeAll(async () => {
  const pair = await generateKeyPair("RS256", { extractable: true });
  key = pair.privateKey;
  publicJWK = { ...await exportJWK(pair.publicKey), kid: "probe-test-key", alg: "RS256", use: "sig" };
  digest = await sha256(png);
  await applyD1Migrations(raw.DB, raw.TEST_MIGRATIONS);
});
beforeEach(async () => {
  env = { ...raw, PROBE_ENABLED: "true", AUTH_ISSUER: issuer, MCP_RESOURCE: resource,
    PROJECT_ID: "project-" + crypto.randomUUID() };
  artifact = "original-" + crypto.randomUUID();
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    if (url !== issuer + ".well-known/jwks.json") throw new Error("Unexpected outbound request");
    return Response.json({ keys: [publicJWK] });
  });
  await raw.DB.prepare("DELETE FROM probe_budget").run();
  await raw.DB.batch([
    raw.DB.prepare("INSERT INTO projects VALUES (?)").bind(env.PROJECT_ID),
    ...["client-a", "client-b"].map(client => raw.DB.prepare(
      "INSERT INTO grants VALUES (?,?,?,?)").bind(issuer, "owner", client, env.PROJECT_ID)),
    raw.DB.prepare("INSERT INTO artifacts VALUES (?,?)").bind(artifact, env.PROJECT_ID),
    raw.DB.prepare("INSERT INTO revisions VALUES (?,?,?,?,?,?)")
      .bind(artifact, 1, "image/png", digest, png.length, "sha256/" + digest)
  ]);
  await raw.BLOBS.put("sha256/" + digest, png);
});
afterEach(() => vi.restoreAllMocks());

describe("authenticated local Workers / D1 / R2 proof", () => {
  it("publishes OAuth metadata and challenges a missing bearer", async () => {
    const metadata = await worker.fetch(new Request(
      "https://nexus.invalid/.well-known/oauth-protected-resource/mcp"), env);
    expect(await metadata.json()).toMatchObject({ resource, authorization_servers: [issuer] });
    const response = await worker.fetch(new Request(resource), env);
    expect(response.status).toBe(401);
    expect(response.headers.get("WWW-Authenticate")).toContain("oauth-protected-resource/mcp");
  });
  it.each([
    ["wrong issuer", { issuer: "https://attacker.invalid/" }],
    ["wrong audience", { audience: "https://another.invalid/mcp" }],
    ["expired", { expires: 1 }],
    ["missing scope", { scope: "openid" }],
    ["missing expiration", { omitExpiry: true }]
  ])("rejects %s before returning data", async (_, options) => {
    expect((await rpc(await token(options as any))).status).toBe(401);
  });
  it("rejects a signature from a different key", async () => {
    const other = await generateKeyPair("RS256");
    expect((await rpc(await token({ key: other.privateKey }))).status).toBe(401);
  });
  it.each([
    ["ungranted subject", { subject: "outsider" }],
    ["ungranted client", { client: "client-c" }]
  ])("rejects %s despite a valid token", async (_, options) => {
    const response = await rpc(await token(options));
    expect(response.status).toBe(403);
    expect(await response.text()).not.toContain(artifact);
  });
  it("two actual SDK clients receive byte-identical images over HTTP transport", async () => {
    const outputs: any[] = [];
    for (const name of ["client-a", "client-b"]) {
      const bearer = await token({ client: name });
      const client = new Client({ name, version: "test" });
      const transport = new StreamableHTTPClientTransport(new URL(resource), {
        requestInit: { headers: { Authorization: "Bearer " + bearer } },
        fetch: (input, init) => worker.fetch(new Request(input, init), env)
      });
      try {
        await client.connect(transport);
        const tools = await client.listTools();
        expect(tools.tools.map(t => t.name)).toEqual(["read_original"]);
        outputs.push(await client.callTool({ name: "read_original",
          arguments: { artifact_id: artifact, revision: 1 } }));
      } finally { await client.close(); }
    }
    expect(outputs[0]).toEqual(outputs[1]);
    expect(outputs[0].structuredContent.sha256).toBe(digest);
    expect(outputs[0].content[1]).toEqual({ type: "image", mimeType: "image/png", data: base64(png) });
  });
  it("applies a grant revocation immediately to an unexpired token", async () => {
    const bearer = await token();
    expect((await rpc(bearer)).status).toBe(200);
    await raw.DB.prepare("DELETE FROM grants WHERE project=?").bind(env.PROJECT_ID).run();
    expect((await rpc(bearer)).status).toBe(403);
  });
  it("does not deliver an artifact from another project or reveal its existence", async () => {
    const other = "other-" + crypto.randomUUID();
    await raw.DB.batch([
      raw.DB.prepare("INSERT INTO projects VALUES (?)").bind(other),
      raw.DB.prepare("INSERT INTO artifacts VALUES (?,?)").bind(other, other)
    ]);
    const denied = await result(await rpc(await token(), "tools/call", {
      name: "read_original", arguments: { artifact_id: other, revision: 1 }
    }));
    const absent = await result(await rpc(await token(), "tools/call", {
      name: "read_original", arguments: { artifact_id: "unknown", revision: 1 }
    }));
    expect(denied.result).toEqual(absent.result);
    expect(denied.result.isError).toBe(true);
    expect(denied.result.content[0].text).toContain("not_accessible");
  });
  it("does not substitute the existing revision when requested revision is missing", async () => {
    const body = await result(await rpc(await token(), "tools/call", {
      name: "read_original", arguments: { artifact_id: artifact, revision: 2 }
    }));
    expect(body.result.isError).toBe(true);
    expect(body.result.content).toHaveLength(1);
    expect(body.result.content[0].text).toContain("insufficient_context");
  });
  it.each(["missing", "wrong size", "wrong hash"])("fails closed for %s original", async mode => {
    if (mode === "missing") await raw.BLOBS.delete("sha256/" + digest);
    else if (mode === "wrong size") await raw.BLOBS.put("sha256/" + digest, "short");
    else await raw.BLOBS.put("sha256/" + digest, new Uint8Array(png.length));
    const body = await result(await rpc(await token()));
    expect(body.result.isError).toBe(true);
    expect(body.result.content).toHaveLength(1);
    expect(body.result.content[0].text).toContain("insufficient_context");
  });
  it("prevents changing an existing revision or project scope", async () => {
    await expect(raw.DB.prepare("UPDATE revisions SET size=0 WHERE artifact_id=?")
      .bind(artifact).run()).rejects.toThrow();
    await expect(raw.DB.prepare("UPDATE artifacts SET project=? WHERE id=?")
      .bind("other", artifact).run()).rejects.toThrow();
  });
  it("does not expose write tools or accept a forged project/identity argument", async () => {
    const body = await result(await rpc(await token(), "tools/call", {
      name: "read_original",
      arguments: { artifact_id: artifact, revision: 1, project: "other", principal: "owner" }
    }));
    expect(body.result?.isError ?? !!body.error).toBe(true);
    const write = await result(await rpc(await token(), "tools/call", { name: "delete_all", arguments: {} }));
    expect(write.result?.isError ?? !!write.error).toBe(true);
  });
  it("enforces the shared request budget atomically under a race", async () => {
    const day = new Date().toISOString().slice(0, 10);
    await raw.DB.prepare("INSERT INTO probe_budget VALUES (?,199,0)").bind(day).run();
    const bearer = await token();
    const responses = await Promise.all([rpc(bearer, "tools/list"), rpc(bearer, "tools/list")]);
    expect(responses.map(r => r.status).sort()).toEqual([200, 429]);
  });
  it("enforces the output budget without returning the original", async () => {
    await raw.DB.prepare("INSERT INTO probe_budget VALUES (?,0,?)")
      .bind(new Date().toISOString().slice(0, 10), DAILY_RESPONSE_BYTES).run();
    const body = await result(await rpc(await token()));
    expect(body.result.isError).toBe(true);
    expect(body.result.content[0].text).toContain("budget_exhausted");
    expect(body.result.content).toHaveLength(1);
  });
  it("rejects untrusted origins and different hosts", async () => {
    expect((await worker.fetch(new Request(resource, {
      headers: { Origin: "https://attacker.invalid" }
    }), env)).status).toBe(403);
    expect((await worker.fetch(new Request("https://other.invalid/mcp"), env)).status).toBe(404);
  });
  it("stays disabled unless deliberately enabled and fails on bad config", async () => {
    expect((await worker.fetch(new Request(resource), { ...env, PROBE_ENABLED: "false" })).status).toBe(503);
    expect((await worker.fetch(new Request(resource), { ...env, MCP_RESOURCE: "http://bad.invalid/mcp" })).status).toBe(503);
  });
  it("rejects oversized request bodies", async () => {
    const response = await worker.fetch(new Request(resource, {
      method: "POST", headers: { Authorization: "Bearer " + await token(),
        "Content-Type": "application/json" },
      body: JSON.stringify({ padding: "x".repeat(17000) })
    }), env);
    expect(response.status).toBe(400);
  });
  it("returns exact UTF-8 text and reserves for JSON control-character expansion", async () => {
    const text = "\u0000".repeat(2000) + "原本\n";
    const bytes = new TextEncoder().encode(text);
    const hash = await sha256(bytes);
    const id = "text-" + crypto.randomUUID();
    await raw.BLOBS.put("sha256/" + hash, bytes);
    await raw.DB.batch([
      raw.DB.prepare("INSERT INTO artifacts VALUES (?,?)").bind(id, env.PROJECT_ID),
      raw.DB.prepare("INSERT INTO revisions VALUES (?,?,?,?,?,?)")
        .bind(id, 1, "text/plain", hash, bytes.length, "sha256/" + hash)
    ]);
    const response = await rpc(await token(), "tools/call", {
      name: "read_original", arguments: { artifact_id: id, revision: 1 }
    });
    const body = await response.text();
    expect(JSON.parse(body).result.content[1].text).toBe(text);
    const row = await raw.DB.prepare("SELECT response_bytes FROM probe_budget").first<{ response_bytes: number }>();
    expect(row!.response_bytes).toBeGreaterThan(new TextEncoder().encode(body).length);
  });
  it("rejects an oversized initial reservation when the daily row does not exist", async () => {
    await expect(reserve(env, DAILY_RESPONSE_BYTES + 1)).rejects.toThrow("budget_exhausted");
    expect(await raw.DB.prepare("SELECT * FROM probe_budget").first()).toBeNull();
  });
});
