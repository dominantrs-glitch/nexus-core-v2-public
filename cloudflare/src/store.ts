import { DAILY_REQUESTS, DAILY_RESPONSE_BYTES, MAX_ORIGINAL_BYTES, ProbeError,
  type Env, type Identity } from "./types";

export async function permitted(env: Env, identity: Identity): Promise<boolean> {
  return !!await env.DB.prepare(
    "SELECT 1 FROM grants WHERE issuer=? AND subject=? AND client_id=? AND project=?"
  ).bind(identity.issuer, identity.subject, identity.clientId, env.PROJECT_ID).first();
}

// Atomic shared admission. Failed calls conservatively consume reservations;
// never undercount on process restart/concurrent clients. Not a provider billing cap.
export async function reserve(env: Env, bytes = 0, requests = 0): Promise<void> {
  const day = new Date().toISOString().slice(0, 10);
  const row = await env.DB.prepare(`
    INSERT INTO probe_budget(day, requests, response_bytes)
    SELECT ?, ?, ? WHERE ? <= ? AND ? <= ?
    ON CONFLICT(day) DO UPDATE SET
      requests=requests+excluded.requests,
      response_bytes=response_bytes+excluded.response_bytes
    WHERE requests+excluded.requests <= ? AND response_bytes+excluded.response_bytes <= ?
    RETURNING day
  `).bind(day, requests, bytes, requests, DAILY_REQUESTS, bytes, DAILY_RESPONSE_BYTES,
    DAILY_REQUESTS, DAILY_RESPONSE_BYTES).first();
  if (!row) throw new ProbeError("budget_exhausted");
}

export interface Manifest {
  artifact_id: string; revision: number; project: string;
  mime_type: string; sha256: string; size: number;
}
interface Row { mime: string; digest: string; size: number; locator: string }
export async function readOriginal(env: Env, identity: Identity, id: string, revision: number) {
  // Recheck live grants for every data operation (revocation isn't tied to JWT expiry).
  if (!await permitted(env, identity)) throw new ProbeError("not_accessible");
  const artifact = await env.DB.prepare("SELECT 1 FROM artifacts WHERE id=? AND project=?")
    .bind(id, env.PROJECT_ID).first();
  if (!artifact) throw new ProbeError("not_accessible");
  const row = await env.DB.prepare(
    "SELECT mime,digest,size,locator FROM revisions WHERE artifact_id=? AND revision=?"
  ).bind(id, revision).first<Row>();
  if (!row) throw new ProbeError("insufficient_context");
  if (!Number.isSafeInteger(row.size) || row.size < 0 || row.size > MAX_ORIGINAL_BYTES ||
      !/^[a-f0-9]{64}$/.test(row.digest) || row.locator !== "sha256/" + row.digest ||
      !["image/png", "text/plain"].includes(row.mime)) throw new ProbeError("insufficient_context");
  // JSON may escape each control byte in text as six ASCII bytes.
  const encodedSize = row.mime === "text/plain" ? row.size * 6 : Math.ceil(row.size / 3) * 4;
  await reserve(env, encodedSize + 8192);
  const object = await env.BLOBS.get(row.locator);
  if (!object || object.size !== row.size) {
    if (object) await object.body.cancel();
    throw new ProbeError("insufficient_context");
  }
  const bytes = new Uint8Array(await object.arrayBuffer());
  const digest = await sha256(bytes);
  if (bytes.length !== row.size || digest !== row.digest) throw new ProbeError("insufficient_context");
  if (row.mime === "image/png" &&
      ![137,80,78,71,13,10,26,10].every((b, i) => bytes[i] === b))
    throw new ProbeError("insufficient_context");
  let text: string | undefined;
  if (row.mime === "text/plain") {
    try { text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes); }
    catch { throw new ProbeError("insufficient_context"); }
  }
  const manifest: Manifest = {
    artifact_id: id, revision, project: env.PROJECT_ID,
    mime_type: row.mime, sha256: digest, size: bytes.length
  };
  return { manifest, bytes, text };
}
export async function sha256(bytes: Uint8Array): Promise<string> {
  const hash = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(hash), b => b.toString(16).padStart(2, "0")).join("");
}
export function base64(bytes: Uint8Array): string {
  // Bounded chunks avoid call-stack overflow at the largest original size.
  let text = "";
  for (let i = 0; i < bytes.length; i += 8192)
    text += String.fromCharCode(...bytes.subarray(i, i + 8192));
  return btoa(text);
}
