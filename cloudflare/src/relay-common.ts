import type { OAuthHelpers } from "@cloudflare/workers-oauth-provider";
import type { GitConfig } from "./github-store";

export interface RelayEnv extends Partial<GitConfig> {
  GIT_WORKSPACE_ENABLED?: string;
  GIT_NATIVE_OWNER?: string;
  GIT_DATA_MODE?: string;
  GIT_GENERATION?: string;
  DB: D1Database;
  OAUTH_KV: KVNamespace;
  OAUTH_PROVIDER: OAuthHelpers;
  TUNNEL: DurableObjectNamespace;
  RELAY_ENABLED: string;
  PUBLIC_ORIGIN: string;
  OWNER_ID: string;
  PROJECT_ID: string;
  GITHUB_CLIENT_ID: string;
  GITHUB_CLIENT_SECRET: string;
  CONNECTOR_TOKEN_SHA256: string;
  ALLOWED_REDIRECTS: string;
}
export const MAX_REQUEST = 16384;
export const MAX_RESPONSE = 96 * 1024 * 1024;
export const DAILY_RESPONSE = 512 * 1024 * 1024;
// Bound normal multi-page workspace use, not just the original synthetic probe.
export const DAILY_REQUESTS = 10000;
export const DAILY_AUTH = 100;
// Owner requested long-idle recovery and concurrent devices (intake r177).
// Expiring sessions remain bounded; explicit revocation is enforced in D1.
export const REFRESH_TTL = 30 * 86400;
export const CLIENT_TTL = 90 * 86400;
export class BudgetExceeded extends Error {}
export function budgetFailure() {
  const now = Date.now();
  const reset = (Math.floor(now / 86400000) + 1) * 86400000;
  return Response.json({ error: "daily_limit_reached", retry_at: new Date(reset).toISOString(),
    reconnect_required: false }, { status: 429, headers: {
      "Cache-Control": "no-store", "Retry-After": String(Math.ceil((reset - now) / 1000))
    } });
}
export function failure(status: number, error: string) {
  return Response.json({ error }, { status, headers: { "Cache-Control": "no-store" } });
}
export async function hash(value: string) {
  return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value))),
    b => b.toString(16).padStart(2, "0")).join("");
}
export async function smallBody(request: Request) {
  if (!request.body) return "";
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const part = await reader.read();
    if (part.done) break;
    size += part.value.length;
    if (size > MAX_REQUEST) { await reader.cancel(); throw new Error("request too large"); }
    chunks.push(part.value);
  }
  const out = new Uint8Array(size);
  let offset = 0;
  for (const part of chunks) { out.set(part, offset); offset += part.length; }
  return new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(out);
}
export async function reserve(env: RelayEnv, requests: number, bytes: number, auth = 0) {
  if (![requests, bytes, auth].every(v => Number.isSafeInteger(v) && v >= 0) ||
      requests > DAILY_REQUESTS || bytes > DAILY_RESPONSE || auth > DAILY_AUTH) throw new Error("invalid budget reservation");
  const day = new Date().toISOString().slice(0, 10);
  const result = await env.DB.prepare(`INSERT INTO relay_budget(day,requests,bytes,auth) VALUES (?,?,?,?)
    ON CONFLICT(day) DO UPDATE SET requests=requests+excluded.requests,
    bytes=bytes+excluded.bytes,auth=auth+excluded.auth
    WHERE requests+excluded.requests<=? AND bytes+excluded.bytes<=? AND auth+excluded.auth<=?
    RETURNING day`).bind(day, requests, bytes, auth, DAILY_REQUESTS, DAILY_RESPONSE, DAILY_AUTH).first();
  if (!result) throw new BudgetExceeded("budget exhausted");
}
