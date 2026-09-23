import type { AuthRequest } from "@cloudflare/workers-oauth-provider";
import { failure, hash, reserve, smallBody, type RelayEnv } from "./relay-common";

const cookieName = "__Host-nexus-flow";
export class OAuthFlowError extends Error {}
function escape(value: string) {
  return value.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}
function page(body: string, cookie?: string, formRedirect?: string) {
  // Chromium applies form-action to a POST's redirect destination too.
  // Only the specific, validated OAuth destination is added to this page.
  const formTarget = formRedirect ? " " + new URL(formRedirect).origin : "";
  return new Response(`<!doctype html><html lang="ja"><meta charset="utf-8"><title>Nexus接続</title><body>${body}</body></html>`, {
    headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store",
      "Content-Security-Policy": `default-src 'none'; form-action 'self'${formTarget}; frame-ancestors 'none'; base-uri 'none'`,
      // Native form POSTs under no-referrer send Origin:null. Preserve the
      // same-origin CSRF check without disclosing referrers to other origins.
      "Referrer-Policy": "same-origin", "X-Content-Type-Options": "nosniff",
      ...(cookie ? { "Set-Cookie": `${cookieName}=${cookie}; Secure; HttpOnly; SameSite=Lax; Path=/; Max-Age=600` } : {}) }
  });
}
async function save(env: RelayEnv, phase: string, payload: object) {
  const id = crypto.randomUUID(), cookie = crypto.randomUUID();
  const now = Date.now();
  await env.DB.prepare("DELETE FROM relay_flows WHERE expires<?").bind(now).run();
  await env.DB.prepare("INSERT INTO relay_flows VALUES(?,?,?,?,?)")
    .bind(id, await hash(cookie), phase, JSON.stringify(payload), now + 600000).run();
  return { id, cookie };
}
async function consume(request: Request, env: RelayEnv, id: string, phase: string) {
  const cookie = request.headers.get("Cookie")?.split(";").map(p => p.trim())
    .find(p => p.startsWith(cookieName + "="))?.slice(cookieName.length + 1);
  if (!cookie) throw new OAuthFlowError("authorization_flow_cookie_missing");
  if (!/^[a-f0-9-]{36}$/.test(id)) throw new OAuthFlowError("authorization_flow_state_invalid");
  const row = await env.DB.prepare(`DELETE FROM relay_flows
    WHERE id=? AND cookie_hash=? AND phase=? AND expires>? RETURNING payload`)
    .bind(id, await hash(cookie), phase, Date.now()).first<{ payload: string }>();
  if (!row) throw new OAuthFlowError("authorization_flow_expired_or_mismatched");
  return JSON.parse(row.payload) as { auth: AuthRequest; userId?: string };
}
function redirects(env: RelayEnv): string[] {
  const allowed = JSON.parse(env.ALLOWED_REDIRECTS);
  if (!Array.isArray(allowed) || !allowed.every(x => typeof x === "string")) throw new Error("invalid redirect configuration");
  return allowed;
}

export async function oauthPages(request: Request, env: RelayEnv): Promise<Response> {
  const url = new URL(request.url);
  if (url.pathname === "/authorize" && request.method === "GET") {
    await reserve(env, 0, 0, 1);
    const auth = await env.OAUTH_PROVIDER.parseAuthRequest(request);
    if (!redirects(env).includes(auth.redirectUri) || auth.codeChallengeMethod !== "S256" || !auth.codeChallenge ||
      auth.scope.length !== 1 || auth.scope[0] !== "nexus:read" || auth.resource !== env.PUBLIC_ORIGIN + "/mcp")
      return failure(400, "unsupported_authorization");
    const flow = await save(env, "login", { auth });
    return page(`<h1>Nexusへの接続</h1><p>本人のGitHubアカウントで確認します。リポジトリへのアクセス権は要求しません。</p>
      <p>接続先: ${escape(auth.clientId)}</p><p>戻り先: ${escape(auth.redirectUri)}</p>
      <form method="post" action="/login"><input type="hidden" name="flow" value="${flow.id}"><button>GitHubで本人確認</button></form>`, flow.cookie, "https://github.com");
  }
  if (["/login", "/approve"].includes(url.pathname) && request.method === "POST") {
    if (request.headers.get("Origin") !== env.PUBLIC_ORIGIN) return failure(403, "invalid_origin");
    const form = new URLSearchParams(await smallBody(request));
    const state = await consume(request, env, form.get("flow") || "", url.pathname === "/login" ? "login" : "consent");
    if (url.pathname === "/login") {
      const flow = await save(env, "github", state);
      const target = new URL("https://github.com/login/oauth/authorize");
      target.searchParams.set("client_id", env.GITHUB_CLIENT_ID);
      target.searchParams.set("redirect_uri", env.PUBLIC_ORIGIN + "/callback");
      target.searchParams.set("state", flow.id);
      // No repo/email scope. A fresh, dedicated GitHub OAuth application is required.
      return new Response(null, { status: 302, headers: { Location: target.href, "Cache-Control": "no-store",
        "Set-Cookie": `${cookieName}=${flow.cookie}; Secure; HttpOnly; SameSite=Lax; Path=/; Max-Age=600` } });
    }
    if (state.userId !== env.OWNER_ID) return failure(403, "not_accessible");
    // Reauthorizing another device must not revoke existing sessions. Epoch is
    // reserved for explicit revocation; a disabled grant cannot be reenabled here.
    const grant = await env.DB.prepare(`INSERT INTO relay_grants(user_id,client_id,epoch,enabled) VALUES(?,?,1,1)
      ON CONFLICT(user_id,client_id) DO UPDATE SET epoch=epoch WHERE enabled=1 RETURNING epoch`)
      .bind(state.userId, state.auth.clientId).first<{ epoch: number }>();
    if (!grant) return failure(403, "grant_disabled");
    const result = await env.OAUTH_PROVIDER.completeAuthorization({ request: state.auth,
      userId: state.userId, metadata: {}, scope: ["nexus:read"], revokeExistingGrants: false,
      props: { userId: state.userId, clientId: state.auth.clientId, epoch: grant.epoch } });
    return new Response(null, { status: 302, headers: { Location: result.redirectTo, "Cache-Control": "no-store",
      "Set-Cookie": `${cookieName}=; Secure; HttpOnly; SameSite=Lax; Path=/; Max-Age=0` } });
  }
  if (url.pathname === "/callback" && request.method === "GET") {
    const state = await consume(request, env, url.searchParams.get("state") || "", "github");
    const code = url.searchParams.get("code");
    if (!code || code.length > 512) return failure(400, "invalid_callback");
    const exchange = await fetch("https://github.com/login/oauth/access_token", {
      // workerd rejects redirect:"error". Manual + status validation rejects
      // redirects without forwarding the client secret or token elsewhere.
      method: "POST", redirect: "manual", signal: AbortSignal.timeout(10000),
      headers: { Accept: "application/json", "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Nexus-Core-V2-Probe/0.1" },
      body: new URLSearchParams({ client_id: env.GITHUB_CLIENT_ID, client_secret: env.GITHUB_CLIENT_SECRET,
        code, redirect_uri: env.PUBLIC_ORIGIN + "/callback" })
    });
    if (!exchange.ok || !exchange.headers.get("Content-Type")?.includes("application/json")) {
      console.warn("nexus_oauth_exchange", exchange.status);
      return failure(502, "identity_exchange_unavailable");
    }
    const token = await exchange.json() as { access_token?: string; scope?: string };
    if (!exchange.ok || !token.access_token || token.scope) return failure(403, "identity_verification_failed");
    const user = await fetch("https://api.github.com/user", { redirect: "manual", signal: AbortSignal.timeout(10000),
      headers: { Authorization: `Bearer ${token.access_token}`, "User-Agent": "Nexus-Core-V2", Accept: "application/vnd.github+json" } });
    if (!user.ok || !user.headers.get("Content-Type")?.includes("application/json")) {
      console.warn("nexus_oauth_profile", user.status);
      return failure(502, "identity_profile_unavailable");
    }
    const profile = await user.json() as { id?: number };
    if (!user.ok || String(profile.id) !== env.OWNER_ID) return failure(403, "not_accessible");
    // Upstream token is not persisted or forwarded to the MCP client.
    const flow = await save(env, "consent", { auth: state.auth, userId: env.OWNER_ID });
    const accessDescription = env.GIT_WORKSPACE_ENABLED === "true" && env.GIT_DATA_MODE !== "draft-intake"
      ? "架空の試験案件を検索・作成し、記録の読み取り・保存ができます。実際の案件や個人情報は保存しないでください。本人の確定承認は代行できません。"
      : "共有された案件を検索・作成し、記録の読み取り・保存ができます。本人の確定承認は代行できません。";
    return page(`<h1>Nexusの利用を許可</h1><p>${escape(state.auth.clientId)}</p><p>戻り先: ${escape(state.auth.redirectUri)}</p>
      <p>${accessDescription}PCの任意ファイルやコマンドにはアクセスできません。</p>
      <form method="post" action="/approve"><input type="hidden" name="flow" value="${flow.id}"><button>この接続を許可</button></form>
      <p>許可しない場合はこの画面を閉じてください。</p>`, flow.cookie, state.auth.redirectUri);
  }
  return failure(404, "not_found");
}
