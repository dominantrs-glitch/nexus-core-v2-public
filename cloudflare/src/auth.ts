import { createRemoteJWKSet, jwtVerify } from "jose";
import type { Env, Identity } from "./types";

const keySets = new Map<string, ReturnType<typeof createRemoteJWKSet>>();
export function configuration(env: Env): { issuer: string; resource: URL } {
  const issuer = new URL(env.AUTH_ISSUER);
  const resource = new URL(env.MCP_RESOURCE);
  if (issuer.protocol !== "https:" || issuer.username || issuer.password ||
      issuer.search || issuer.hash || !issuer.pathname.endsWith("/") ||
      resource.protocol !== "https:" || resource.username || resource.password ||
      resource.pathname !== "/mcp" || resource.search || resource.hash ||
      !env.PROJECT_ID) throw new Error("invalid server configuration");
  return { issuer: issuer.href, resource };
}
export async function authenticate(request: Request, env: Env): Promise<Identity | null> {
  const header = request.headers.get("Authorization");
  if (!header || header.length > 16384 || !/^Bearer [^\s]+$/i.test(header)) return null;
  const { issuer, resource } = configuration(env);
  // Key discovery is pinned to trusted deployment config, never a JWT jku/iss claim.
  let keys = keySets.get(issuer);
  if (!keys) {
    keys = createRemoteJWKSet(new URL(".well-known/jwks.json", issuer), {
      timeoutDuration: 3000, cooldownDuration: 30000, cacheMaxAge: 600000
    });
    keySets.set(issuer, keys);
  }
  try {
    const { payload } = await jwtVerify(header.slice(7), keys, {
      issuer, audience: resource.href, algorithms: ["RS256"],
      requiredClaims: ["iss", "sub", "aud", "iat", "exp"],
      clockTolerance: 5
    });
    const scopes = typeof payload.scope === "string" ? payload.scope.split(" ") : [];
    const clientId = typeof payload.azp === "string" ? payload.azp : payload.client_id;
    if (typeof payload.sub !== "string" || !payload.sub ||
        typeof clientId !== "string" || !clientId ||
        !scopes.includes("nexus:read")) return null;
    return { issuer, subject: payload.sub, clientId };
  } catch {
    return null;
  }
}
export function challenge(env: Env): Response {
  const { resource } = configuration(env);
  const metadata = new URL("/.well-known/oauth-protected-resource/mcp", resource);
  return Response.json({ error: "unauthorized" }, {
    status: 401,
    headers: {
      "WWW-Authenticate": `Bearer resource_metadata="${metadata.href}", scope="nexus:read"`,
      "Cache-Control": "no-store"
    }
  });
}
