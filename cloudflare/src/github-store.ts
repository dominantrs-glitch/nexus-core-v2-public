import { importPKCS8, SignJWT } from "jose";
import { createPrivateKey } from "node:crypto";
import { StoreError, type GitBackend } from "./git-store";

export interface GitConfig {
  GIT_REPOSITORY: string; // owner/name, never a model-supplied URL
  GIT_REPOSITORY_ID: string;
  GIT_BRANCH: string;
  GIT_APP_ID: string;
  GIT_INSTALLATION_ID: string;
  GIT_APP_PRIVATE_KEY: string; // Worker Secret, GitHub PKCS#1 or PKCS#8 RSA PEM
}
const sha = (value: unknown): string => {
  if (typeof value !== "string" || !/^[a-f0-9]{40}$/.test(value)) throw new StoreError("canonical_invalid_or_unavailable");
  return value;
};
const canonicalPath = /^(nexus\.json|context\.json|(?:legacy-)?requests\/[a-f0-9]{64}\.json|native\/(catalog\.json|requests\/[a-f0-9]{64}\.json|projects\/[a-z0-9_-]+\/[1-9][0-9]*\.json)|projects\/[a-z0-9_-]+\/(manifest\.json|records\/[a-z0-9_-]+\.json|changes\/[1-9][0-9]*\.json|(?:originals|binary)\/(manifest\.json|[a-z0-9_-]+\/[1-9][0-9]*\.json)))$/;
export class GitHubBackend implements GitBackend {
  private token?: { value: string; expires: number };
  private prefix: string;
  // workerd's native fetch requires its global receiver; do not call a captured
  // native function as this.http(), which works in Node but throws in Workers.
  constructor(private config: GitConfig, private http: typeof fetch = (input, init) => fetch(input, init)) {
    if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(config.GIT_REPOSITORY) ||
        !/^[A-Za-z0-9_-]+$/.test(config.GIT_BRANCH) ||
        ![config.GIT_REPOSITORY_ID, config.GIT_APP_ID, config.GIT_INSTALLATION_ID].every(v => /^[1-9][0-9]*$/.test(v)))
      throw new StoreError("git_configuration_required");
    this.prefix = "/repos/" + config.GIT_REPOSITORY;
  }
  private async request(path: string, method: string, token: string, body?: unknown) {
    let response: Response;
    try {
      response = await this.http("https://api.github.com" + path, { method, redirect: "manual",
        headers: { Accept: "application/vnd.github+json", Authorization: "Bearer " + token,
          "X-GitHub-Api-Version": "2026-03-10", "User-Agent": "Nexus-Core-V2", "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(15000) });
    } catch { console.warn("nexus_git_transport_failed"); throw new StoreError("canonical_unavailable_or_outcome_unknown"); }
    if (response.status === 404) return null;
    if (!response.ok) {
      console.warn("nexus_git_http_failed", response.status);
      await response.body?.cancel();
      throw new StoreError(response.status === 409 || response.status === 422 ? "canonical_conflict_reread" :
        response.status === 429 || response.status === 403 ? "canonical_denied_or_rate_limited" : "canonical_unavailable_or_outcome_unknown");
    }
    // All API responses in this adapter are small JSON objects or bounded notes.
    const reader = response.body?.getReader();
    if (!reader) throw new StoreError("canonical_invalid_or_unavailable");
    const chunks: Uint8Array[] = []; let size = 0;
    for (;;) {
      const part = await reader.read(); if (part.done) break;
      size += part.value.length;
      if (size > 2 * 1024 * 1024) { await reader.cancel(); throw new StoreError("canonical_response_too_large"); }
      chunks.push(part.value);
    }
    const bytes = new Uint8Array(size); let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
    try { return JSON.parse(new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes)); }
    catch { throw new StoreError("canonical_invalid_or_unavailable"); }
  }
  private async auth() {
    if (this.token && this.token.expires > Date.now() + 60000) return this.token.value;
    const now = Math.floor(Date.now()/1000);
    // GitHub downloads PKCS#1 keys; normalize only in memory, never log key bytes.
    let pem = this.config.GIT_APP_PRIVATE_KEY;
    let key: CryptoKey;
    try {
      if (pem.includes("-----BEGIN RSA PRIVATE KEY-----"))
        pem = createPrivateKey(pem).export({type:"pkcs8",format:"pem"}).toString();
      key = await importPKCS8(pem, "RS256");
    } catch { throw new StoreError("git_private_key_invalid"); }
    const jwt = await new SignJWT({}).setProtectedHeader({ alg: "RS256" }).setIssuer(this.config.GIT_APP_ID)
      .setIssuedAt(now-60).setExpirationTime(now+540).sign(key);
    const result = await this.request(`/app/installations/${this.config.GIT_INSTALLATION_ID}/access_tokens`, "POST", jwt,
      { repository_ids: [Number(this.config.GIT_REPOSITORY_ID)], permissions: { contents: "write" } });
    const expires = Date.parse(result?.expires_at);
    if (typeof result?.token !== "string" || !Number.isFinite(expires) || expires <= Date.now())
      throw new StoreError("git_authentication_unavailable");
    this.token = { value: result.token, expires };
    return result.token;
  }
  private async api(path: string, method = "GET", body?: unknown) {
    return this.request(this.prefix + path, method, await this.auth(), body);
  }
  async head() {
    const repo = await this.api("");
    if (repo?.private !== true || String(repo?.id) !== this.config.GIT_REPOSITORY_ID)
      throw new StoreError("private_repository_identity_mismatch");
    const ref = await this.api(`/git/ref/heads/${this.config.GIT_BRANCH}`);
    return sha(ref?.object?.sha);
  }
  async read(commit: string, path: string) {
    sha(commit);
    if (!canonicalPath.test(path))
      throw new StoreError("invalid_canonical_path");
    const file = await this.api(`/contents/${path}?ref=${commit}`);
    if (file === null) return null;
    if (file.type !== "file" || file.encoding !== "base64" || typeof file.content !== "string")
      throw new StoreError("canonical_invalid_or_unavailable");
    try {
      const bytes = Uint8Array.from(atob(file.content.replace(/\s/g, "")), c => c.charCodeAt(0));
      return JSON.parse(new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes));
    } catch { throw new StoreError("canonical_invalid_or_unavailable"); }
  }
  async commit(base: string, files: Record<string, unknown>) {
    if (!Object.keys(files).length || Object.keys(files).some(path => !canonicalPath.test(path)))
      throw new StoreError("invalid_canonical_path");
    const parent = await this.api(`/git/commits/${sha(base)}`);
    const tree = await this.api("/git/trees", "POST", { base_tree: sha(parent?.tree?.sha),
      tree: Object.entries(files).map(([path, value]) => ({ path, mode: "100644", type: "blob", content: JSON.stringify(value) })) });
    const commit = await this.api("/git/commits", "POST", { message: "Nexus draft transaction", tree: sha(tree?.sha), parents: [base] });
    // A single-parent sibling commit cannot fast-forward a competing writer.
    const result = await this.api(`/git/refs/heads/${this.config.GIT_BRANCH}`, "PATCH", { sha: sha(commit?.sha), force: false });
    if (result?.object?.sha !== commit.sha) throw new StoreError("save_outcome_unknown_retry_same_request");
  }
}
