import { WorkerEntrypoint } from "cloudflare:workers";
import { OAuthProvider } from "@cloudflare/workers-oauth-provider";
import { BudgetExceeded, budgetFailure, failure, hash, reserve, smallBody, REFRESH_TTL, CLIENT_TTL, type RelayEnv } from "./relay-common";
import { oauthPages, OAuthFlowError } from "./relay-oauth";
import { GitHubBackend, type GitConfig } from "./github-store";
import { GitIntake, dataMode } from "./git-store";
import { gitMcp } from "./git-mcp";
export { LocalTunnel } from "./tunnel";

export class RelayApi extends WorkerEntrypoint<RelayEnv> {
  async fetch(request: Request): Promise<Response> {
    const props = this.ctx.props as { userId?: string; clientId?: string; epoch?: number };
    if (!props || props.userId !== this.env.OWNER_ID || !props.clientId || !props.epoch) return failure(403, "not_accessible");
    const grant = await this.env.DB.prepare("SELECT 1 FROM relay_grants WHERE user_id=? AND client_id=? AND epoch=? AND enabled=1")
      .bind(props.userId, props.clientId, props.epoch).first();
    if (!grant) return failure(403, "not_accessible");
    if (new URL(request.url).pathname !== "/mcp" || new URL(request.url).search) return failure(404, "not_found");
    if (request.method !== "POST") return new Response(null, { status: 405, headers: { Allow: "POST" } });
    if (!request.headers.get("Content-Type")?.startsWith("application/json")) return failure(415, "json_required");
    const body = await smallBody(request);
    const parsed = JSON.parse(body);
    if (!parsed || Array.isArray(parsed) || parsed.jsonrpc !== "2.0" ||
      !["initialize", "notifications/initialized", "ping", "tools/list", "tools/call",
        "resources/list", "resources/templates/list", "resources/read"].includes(parsed.method))
      return failure(400, "unsupported_request");
    if (parsed.method === "resources/read" && parsed.params?.uri !== "ui://nexus/original-handoff-v5.html")
      return failure(400, "unsupported_resource");
    await reserve(this.env, 1, 0);
    if (this.env.GIT_WORKSPACE_ENABLED === "true") {
      const mode=dataMode.safeParse(this.env.GIT_DATA_MODE || "synthetic");
      const generation=this.env.GIT_GENERATION;
      if (!mode.success || (mode.data === "draft-intake" && !generation) ||
          (generation !== undefined && !/^[a-z0-9][a-z0-9_-]{0,79}$/.test(generation)))
        return failure(503,"git_configuration_required");
      const response = await gitMcp(parsed, new GitIntake(new GitHubBackend(this.env as GitConfig), props.userId,generation,mode.data,this.env.GIT_NATIVE_OWNER));
      await reserve(this.env, 0, new TextEncoder().encode(await response.clone().text()).length);
      return response;
    }
    return this.env.TUNNEL.get(this.env.TUNNEL.idFromName("single-owner-pc")).fetch("https://tunnel/request", {
      method: "POST", headers: { "MCP-Protocol-Version": request.headers.get("MCP-Protocol-Version") || "" }, body
    });
  }
}

function provider(env: RelayEnv) {
  return new OAuthProvider<RelayEnv>({ apiRoute: "/mcp", apiHandler: RelayApi,
    defaultHandler: { fetch: oauthPages }, authorizeEndpoint: "/authorize", tokenEndpoint: "/oauth/token",
    clientRegistrationEndpoint: "/oauth/register", scopesSupported: ["nexus:read"],
    accessTokenTTL: 900, refreshTokenTTL: REFRESH_TTL, clientRegistrationTTL: CLIENT_TTL,
    allowImplicitFlow: false, allowPlainPKCE: false, allowTokenExchangeGrant: false,
    clientIdMetadataDocumentEnabled: true,
    resourceMetadata: { resource: env.PUBLIC_ORIGIN + "/mcp", authorization_servers: [env.PUBLIC_ORIGIN],
      scopes_supported: ["nexus:read"], resource_name: env.GIT_DATA_MODE === "draft-intake" ? "Nexus Core V2 shared drafts" : "Nexus synthetic local probe" }
  });
}

export default {
  async fetch(request: Request, env: RelayEnv, ctx: ExecutionContext): Promise<Response> {
    try {
      if (env.RELAY_ENABLED !== "true") return failure(503, "relay_disabled");
      const origin = new URL(env.PUBLIC_ORIGIN);
      const url = new URL(request.url);
      if (origin.protocol !== "https:" || origin.origin !== env.PUBLIC_ORIGIN || url.origin !== origin.origin)
        return failure(404, "not_found");
      if (request.headers.has("Origin") && request.headers.get("Origin") !== origin.origin) return failure(403, "invalid_origin");
      if (url.pathname === "/connect" || url.pathname.startsWith("/reply/")) {
        const token = request.headers.get("Authorization")?.match(/^Bearer ([A-Za-z0-9_-]{43,128})$/)?.[1];
        if (!token || await hash(token) !== env.CONNECTOR_TOKEN_SHA256) return failure(401, "connector_unauthorized");
        if (url.search) return failure(400, "query_not_allowed");
        return env.TUNNEL.get(env.TUNNEL.idFromName("single-owner-pc")).fetch(new Request("https://tunnel" + url.pathname, request));
      }
      // Bound all OAuth request bodies, including registration/token endpoints.
      if (request.method === "POST") {
        const body = await smallBody(request);
        request = new Request(request, { body });
        if (url.pathname === "/oauth/register") await reserve(env, 0, 0, 1);
        if (url.pathname === "/oauth/token") {
          await reserve(env, 0, 0, 1);
          const form = new URLSearchParams(body);
          if (["authorization_code", "refresh_token"].includes(form.get("grant_type") || "")) {
            const code = form.get(form.get("grant_type") === "authorization_code" ? "code" : "refresh_token");
            if (!code) return failure(400, "invalid_grant");
            // A strong one-use fence complements KV's eventual deletion behavior.
            await env.DB.prepare("DELETE FROM relay_used_codes WHERE expires<?").bind(Date.now()).run();
            const used = await env.DB.prepare("INSERT OR IGNORE INTO relay_used_codes VALUES(?,?) RETURNING digest")
              .bind(await hash(code), Date.now() + REFRESH_TTL * 1000).first();
            if (!used) return failure(400, "invalid_grant");
          }
        }
      }
      return await provider(env).fetch(request, env, ctx);
    } catch (error) {
      if (error instanceof BudgetExceeded) return budgetFailure();
      if (error instanceof OAuthFlowError) {
        // Fixed categories only: never log URLs, state, cookies, codes or tokens.
        console.warn("nexus_oauth_flow", error.message);
        return failure(400, error.message);
      }
      console.warn("nexus_relay_failure", error instanceof Error ? error.name : "unknown");
      return failure(503, "relay_unavailable");
    }
  }
} satisfies ExportedHandler<RelayEnv>;
