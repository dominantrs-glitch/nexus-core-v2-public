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
const canonicalPath = /^(nexus\.json|context\.json|lifecycle\.json|lifecycle\/(events\/[1-9][0-9]*|requests\/[a-f0-9]{64})\.json|(?:legacy-)?requests\/[a-f0-9]{64}\.json|native\/(catalog\.json|requests\/[a-f0-9]{64}\.json|projects\/[a-z0-9_-]+\/[1-9][0-9]*\.json)|projects\/[a-z0-9_-]+\/(manifest\.json|(?:records|learning-evaluations)\/[a-z0-9_-]+\.json|(?:changes|removals)\/[1-9][0-9]*\.json|(?:originals|binary)\/(manifest\.json|[a-z0-9_-]+\/[1-9][0-9]*\.json)))$/;
export class GitHubBackend implements GitBackend {
  private token?: { value: string; expires: number };
  private authPending?: Promise<string>;
  private reads = new Map<string, Promise<unknown | null>>();
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
  private async request(path: string, method: string, token: string, body?: unknown,limit=2*1024*1024) {
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
      if ([401,403,429].includes(response.status)) {
        const integer=(name:string)=>{
          const raw=response.headers.get(name);
          return raw && /^\d{1,10}$/.test(raw) ? Number(raw) : undefined;
        };
        const remaining=integer('x-ratelimit-remaining'),reset=integer('x-ratelimit-reset');
        const retryHeader=response.headers.get('retry-after');
        const retrySeconds=integer('retry-after');
        const retryDate=retryHeader && retrySeconds===undefined ? Date.parse(retryHeader) : NaN;
        // Only inspect the bounded upstream message to classify a secondary limit.
        // Never expose/log that message, the URL, credentials or record contents.
        let message='';
        try {
          const reader=response.body?.getReader();let raw='',size=0;
          if(reader)for(;;){const part=await reader.read();if(part.done)break;
            size+=part.value.length;if(size>8192){await reader.cancel();raw='';break;}
            raw+=new TextDecoder().decode(part.value);}
          const parsed=JSON.parse(raw);if(typeof parsed.message==='string')message=parsed.message;
        } catch { /* Headers still identify primary limits and 429 responses. */ }
        const limited=response.status===429 || response.status===403 &&
          (remaining===0 || retrySeconds!==undefined || Number.isFinite(retryDate) ||
           /(?:secondary |api )?rate limit|abuse detection/i.test(message));
        if(limited) {
          const now=Date.now();
          const wait=Math.max(1,retrySeconds ?? (Number.isFinite(retryDate)?Math.ceil((retryDate-now)/1000):
            remaining===0 && reset!==undefined?Math.ceil((reset*1000-now)/1000):60));
          throw new StoreError('canonical_rate_limited',{http_status:response.status,retryable:true,
            retry_after_seconds:wait,retry_at:new Date(now+wait*1000).toISOString(),
            ...(remaining!==undefined?{rate_limit_remaining:remaining}:{})});
        }
        throw new StoreError(response.status===401?'canonical_authentication_denied':'canonical_access_denied',
          {http_status:response.status,retryable:false});
      }
      await response.body?.cancel();
      throw new StoreError(response.status === 409 || response.status === 422 ? "canonical_conflict_reread" :
        "canonical_unavailable_or_outcome_unknown");
    }
    // All API responses in this adapter are small JSON objects or bounded notes.
    const reader = response.body?.getReader();
    if (!reader) throw new StoreError("canonical_invalid_or_unavailable");
    const chunks: Uint8Array[] = []; let size = 0;
    for (;;) {
      const part = await reader.read(); if (part.done) break;
      size += part.value.length;
      if (size > limit) { await reader.cancel(); throw new StoreError("canonical_response_too_large"); }
      chunks.push(part.value);
    }
    const bytes = new Uint8Array(size); let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
    try { return JSON.parse(new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes)); }
    catch { throw new StoreError("canonical_invalid_or_unavailable"); }
  }
  private auth(): Promise<string> {
    if (this.token && this.token.expires > Date.now() + 60000) return Promise.resolve(this.token.value);
    // Parallel note/context reads share one in-flight token request, scoped to
    // this exact configured backend. Failed credentials are never cached.
    if(!this.authPending)this.authPending=this.issueToken().finally(()=>{this.authPending=undefined;});
    return this.authPending;
  }
  private async issueToken() {
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
    // Bound the cache to one top-level observation. Rechecking current access
    // must never be satisfied by a previous operation's manifest cache.
    this.reads.clear();
    const repo = await this.api("");
    if (repo?.private !== true || String(repo?.id) !== this.config.GIT_REPOSITORY_ID)
      throw new StoreError("private_repository_identity_mismatch");
    const ref = await this.api(`/git/ref/heads/${this.config.GIT_BRANCH}`);
    return sha(ref?.object?.sha);
  }
  async read(commit: string, path: string) {
    sha(commit);
    if (!canonicalPath.test(path) && !/^projects\/[a-z0-9_-]+\/(?:start-review|daily\/calendar)\.json$/.test(path))
      throw new StoreError("invalid_canonical_path");
    const key=commit+':'+path;
    if(!this.reads.has(key)) {
      if(this.reads.size>=256)this.reads.delete(this.reads.keys().next().value!);
      this.reads.set(key,this.readFile(commit,path).catch(error=>{this.reads.delete(key);throw error;}));
    }
    // Git blobs at an exact commit are immutable; callers may mutate parsed
    // manifests while constructing a transaction, so never share object identity.
    return structuredClone(await this.reads.get(key)!);
  }
  /** Batch immutable blobs without expanding the caller's canonical path scope. */
  async readMany(commit:string,paths:string[]) {
    sha(commit);
    if(paths.length>1200||paths.some(path=>!canonicalPath.test(path)&&
      !/^projects\/[a-z0-9_-]+\/(?:start-review|daily\/calendar)\.json$/.test(path)))throw new StoreError('invalid_canonical_path');
    const pending=[...new Set(paths)].filter(path=>!this.reads.has(commit+':'+path));
    const chunks=paths.length<=12&&paths.every(path=>/^projects\/[a-z0-9_-]+\/binary\/chunk-[a-f0-9]{64}\/1\.json$/.test(path));
    const [owner,name]=this.config.GIT_REPOSITORY.split('/');
    for(let offset=0;offset<pending.length;offset+=40){
      const batch=pending.slice(offset,offset+40),variables:Record<string,string>={owner,name};
      batch.forEach((path,i)=>variables['p'+i]=commit+':'+path);
      const query='query($owner:String!,$name:String!,'+batch.map((_,i)=>'$p'+i+':String!').join(',')+')'+
        '{repository(owner:$owner,name:$name,followRenames:false){databaseId isPrivate '+batch.map((_,i)=>
          'b'+i+':object(expression:$p'+i+'){__typename ... on Blob {text isTruncated byteSize}}').join(' ')+'}}';
      const result=await this.request('/graphql','POST',await this.auth(),{query,variables},(chunks?4:2)*1024*1024);
      // GraphQL can report rate exhaustion inside HTTP 200. Never cache a
      // partial batch or mistake it for absent records; no automatic retries.
      if(result?.errors?.some((e:any)=>e?.type==='RATE_LIMITED'))throw new StoreError('canonical_rate_limited',
        {http_status:200,retryable:true,retry_after_seconds:60,retry_at:new Date(Date.now()+60000).toISOString()});
      const repo=result?.data?.repository;
      if(result?.errors?.length||!repo||repo.isPrivate!==true||String(repo.databaseId)!==this.config.GIT_REPOSITORY_ID)
        throw new StoreError('canonical_batch_unavailable');
      const values:unknown[]=[];
      for(let i=0;i<batch.length;i++){
        const blob=repo['b'+i];
        if(blob===null){values.push(null);continue;}
        if(blob?.__typename!=='Blob'||blob.isTruncated!==false||typeof blob.text!=='string'||
          new TextEncoder().encode(blob.text).length!==blob.byteSize)throw new StoreError('canonical_batch_unavailable');
        try{values.push(JSON.parse(blob.text));}catch{throw new StoreError('canonical_invalid_or_unavailable');}
      }
      batch.forEach((path,i)=>this.reads.set(commit+':'+path,Promise.resolve(values[i])));
    }
  }
  release(commit:string,paths:string[]){for(const path of paths)this.reads.delete(commit+':'+path);}
  private async readFile(commit:string,path:string) {
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
    if (!Object.keys(files).length || Object.keys(files).some(path => !canonicalPath.test(path)&&!/^projects\/[a-z0-9_-]+\/(?:start-review|daily\/calendar)\.json$/.test(path)))
      throw new StoreError("invalid_canonical_path");
    const parent = await this.api(`/git/commits/${sha(base)}`);
    const tree = await this.api("/git/trees", "POST", { base_tree: sha(parent?.tree?.sha),
      tree: Object.entries(files).map(([path, value]) => ({ path, mode: "100644", type: "blob",
        ...(value===null?{sha:null}:{content:JSON.stringify(value)}) })) });
    const commit = await this.api("/git/commits", "POST", { message: "Nexus draft transaction", tree: sha(tree?.sha), parents: [base] });
    // A single-parent sibling commit cannot fast-forward a competing writer.
    const result = await this.api(`/git/refs/heads/${this.config.GIT_BRANCH}`, "PATCH", { sha: sha(commit?.sha), force: false });
    if (result?.object?.sha !== commit.sha) throw new StoreError("save_outcome_unknown_retry_same_request");
  }
}
