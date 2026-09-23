/** Native Core transport for the trusted owner-local adapter ONLY.
 * Not a model tool or an authority validator: Python Core validates semantics and
 * obtains native owner confirmations before an update reaches this transport.
 * The draft MCP API deliberately cannot call these methods.
 */
import {z} from "zod";
import {hash} from "./relay-common";
import {dataMode, StoreError, type GitBackend} from "./git-store";
import {nativeShare, type NativeRequest, type NativeRef} from "./git-native-context";

const id = z.string().regex(/^[a-z0-9][a-z0-9_-]{0,79}$/);
const text = z.string().min(1).max(200);
const digest = z.string().regex(/^[a-f0-9]{64}$/);
const revision = z.number().int().safe().nonnegative();
const originSchema = z.object({generation:text,source_digest:digest,package_sha256:digest}).strict();
const filePath = /^(manifest\.json|project\.json|context\.json|approvals\.json|artifacts\/(manifest\.json|blobs\/[a-f0-9]{64}))$/;
const filesSchema = z.record(z.string().regex(filePath), z.string().max(1400000));
const inputSchema = z.object({project:id,expected_revision:revision,expected_document_sha256:digest.nullable(),
  expected_generation:text,request_id:text,files:filesSchema,origin:originSchema}).strict();
const readSchema = z.object({project:id,snapshot:z.string().optional()}).strict();
const entrySchema = z.object({project:id,revision:revision.min(1),document_sha256:digest,
  enabled:z.boolean(),write_state:z.enum(["active","frozen"]),shares:z.array(nativeShare).max(100).default([])}).strict();
const catalogSchema = z.object({schema:z.literal(1),owner:text,native_owner:text,generation:text,mode:dataMode,
  projects:z.array(entrySchema).max(100)}).strict();
const documentSchema = z.object({schema:z.literal(1),project:id,owner:text,revision:revision.min(1),
  previous_sha256:digest.nullable(),origin:originSchema,files:filesSchema}).strict();
const resultSchema = z.object({project:id,revision:revision.min(1),document_sha256:digest}).strict();
const receiptSchema = z.object({owner:text,input_sha256:digest,result:resultSchema}).strict();
type Catalog = z.infer<typeof catalogSchema>;
type Document = z.infer<typeof documentSchema>;
const invalid = () => new StoreError("native_canonical_invalid_or_unavailable");
const documentPath = (project:string, version:number) => `native/projects/${project}/${version}.json`;
// Stable across Python and JS, including Unicode and object insertion order.
const canonical = (value:unknown):string => {
  if (Array.isArray(value)) return "["+value.map(canonical).join(",")+"]";
  if (value !== null && typeof value === "object") return "{"+Object.entries(value).sort(([a],[b])=>a<b?-1:a>b?1:0)
    .map(([k,v])=>JSON.stringify(k)+":"+canonical(v)).join(",")+"}";
  return JSON.stringify(value);
};
const checksum = (value:unknown) => hash(canonical(value));
async function byteHash(bytes:Uint8Array) {
  return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",bytes)), b=>b.toString(16).padStart(2,"0")).join("");
}

function preserveHistory(previous:z.infer<typeof filesSchema>, next:z.infer<typeof filesSchema>) {
  const parse=(files:z.infer<typeof filesSchema>,path:string)=>JSON.parse(new TextDecoder("utf-8",{fatal:true,ignoreBOM:true})
    .decode(Uint8Array.from(atob(files[path]),c=>c.charCodeAt(0))));
  const retained=(old:unknown[],current:unknown[],key:(v:any)=>string,normalize=(v:any)=>v)=>{
    if(!Array.isArray(old)||!Array.isArray(current))throw invalid();
    const index=new Map(current.map(v=>[key(v),canonical(normalize(v))]));
    if(index.size!==current.length || old.some(v=>index.get(key(v))!==canonical(normalize(v))))
      throw new StoreError("native_history_rewrite_forbidden");
  };
  const a=parse(previous,"project.json"),b=parse(next,"project.json");
  retained(a.contracts,b.contracts,v=>String(v.revision));
  retained(a.events,b.events,v=>String(v.seq));
  const x=parse(previous,"context.json"),y=parse(next,"context.json");
  for(const name of ["sources","contexts"])retained(x[name],y[name],v=>JSON.stringify([v.id,v.revision]));
  retained(x.policies,y.policies,v=>JSON.stringify([v.project,v.contract_revision,v.operation,v.revision]));
  retained(parse(previous,"approvals.json").approvals,parse(next,"approvals.json").approvals,v=>String(v.id));
  // Readers are current ACL and may be narrowed; the original/version metadata
  // and bytes remain immutable. The native adapter validates the effective ACL.
  retained(parse(previous,"artifacts/manifest.json").entries,parse(next,"artifacts/manifest.json").entries,
    v=>JSON.stringify([v.artifact_id,v.revision]),v=>{const {principals,...immutable}=v;return immutable;});
  for(const path of Object.keys(previous).filter(p=>p.startsWith("artifacts/blobs/")))
    if(previous[path]!==next[path])throw new StoreError("native_history_rewrite_forbidden");
}

export async function validateNativeFiles(files:z.infer<typeof filesSchema>, project:string, owner:string) {
  const paths=Object.keys(files);
  if (paths.length>133 || !paths.length || new TextEncoder().encode(JSON.stringify(files)).length>1024*1024) throw invalid();
  const decoded = new Map<string,Uint8Array>();
  const hashes = new Map<string,string>();
  for (const path of paths) {
    if (!filePath.test(path)) throw invalid();
    let bytes:Uint8Array;
    try {
      bytes=Uint8Array.from(atob(files[path]),c=>c.charCodeAt(0));
      // Canonical padded base64; do not accept ignored characters or alternate encodings.
      let encoded=""; for(const b of bytes)encoded+=String.fromCharCode(b);
      if (btoa(encoded)!==files[path])throw invalid();
    } catch {throw invalid();}
    decoded.set(path,bytes); hashes.set(path,await byteHash(bytes));
  }
  const json=(path:string) => {
    try {return JSON.parse(new TextDecoder("utf-8",{fatal:true,ignoreBOM:true}).decode(decoded.get(path)!));}
    catch {throw invalid();}
  };
  const manifest=json("manifest.json"), state=json("project.json"), contexts=json("context.json"), audit=json("approvals.json");
  const required=["project.json","context.json","artifacts/manifest.json","approvals.json"];
  if (manifest.format!=="nexus-core-v2" || manifest.project!==project || !manifest.files ||
      Object.keys(manifest.files).sort().join()!==required.sort().join() ||
      required.some(path=>manifest.files[path]!==hashes.get(path)) ||
      state.project?.id!==project || state.project?.owner!==owner ||
      state.schema!=="nexus.project.v1" || contexts.schema!=="nexus.context.v1" ||
      ![contexts.sources,contexts.contexts,contexts.policies].every(rows=>Array.isArray(rows) && rows.every(r=>r.project===project && r.owner===owner)) ||
      audit.project!==project || audit.owner!==owner) throw invalid();
  const artifacts=json("artifacts/manifest.json");
  if(artifacts.format!=="nexus-artifacts-v1" || !Array.isArray(artifacts.entries))throw invalid();
  const expected=new Set(["manifest.json",...required]);
  for(const item of artifacts.entries) {
    const blob=`artifacts/blobs/${item.sha256}`; expected.add(blob);
    if(item.project!==project || !/^[a-f0-9]{64}$/.test(item.sha256) || hashes.get(blob)!==item.sha256 ||
      decoded.get(blob)?.length!==item.size)throw invalid();
  }
  if(paths.length!==expected.size || paths.some(path=>!expected.has(path)))throw invalid();
}

export class GitCoreStore {
  constructor(private git:GitBackend,private owner:string,private generation:string,
    private mode:z.infer<typeof dataMode>="synthetic",private nativeOwner=owner) {}

  private async current(expected?:string) {
    const head=await this.git.head();
    if(expected!==undefined && expected!==head)throw new StoreError("snapshot_changed_restart_read");
    const root=await this.git.read(head,"nexus.json") as any;
    if(root?.schema!==1 || root.owner!==this.owner || root.generation!==this.generation || root.mode!==this.mode)throw invalid();
    const raw=await this.git.read(head,"native/catalog.json");
    const empty:Catalog={schema:1,owner:this.owner,native_owner:this.nativeOwner,generation:this.generation,mode:this.mode,projects:[]};
    const parsed=catalogSchema.safeParse(raw===null?empty:raw);
    if(!parsed.success || parsed.data.owner!==this.owner || parsed.data.native_owner!==this.nativeOwner ||
      parsed.data.generation!==this.generation || parsed.data.mode!==this.mode ||
      new Set(parsed.data.projects.map(p=>p.project)).size!==parsed.data.projects.length)throw invalid();
    return {head,catalog:parsed.data};
  }
  private async document(head:string,entry:z.infer<typeof entrySchema>) {
    if(!entry.enabled)throw new StoreError("native_project_unavailable");
    const parsed=documentSchema.safeParse(await this.git.read(head,documentPath(entry.project,entry.revision)));
    if(!parsed.success || parsed.data.project!==entry.project || parsed.data.owner!==this.nativeOwner ||
      parsed.data.revision!==entry.revision || await checksum(parsed.data)!==entry.document_sha256)throw invalid();
    await validateNativeFiles(parsed.data.files,entry.project,this.nativeOwner);
    return parsed.data;
  }
  async read(args:unknown) {
    const a=readSchema.parse(args),s=await this.current(a.snapshot);
    const entry=s.catalog.projects.find(p=>p.project===a.project);
    if(!entry)throw new StoreError("native_project_unavailable");
    return {snapshot:s.head,generation:this.generation,write_state:entry.write_state,document_sha256:entry.document_sha256,
      document:await this.document(s.head,entry)};
  }
  async readShared(ref:Pick<NativeRef,'native_project'|'native_operation'>,request:NativeRequest,snapshot:string) {
    const s=await this.current(snapshot),entry=s.catalog.projects.find(p=>p.project===ref.native_project);
    if(!entry?.enabled || entry.write_state!=="active" || !entry.shares.some(share=>share.project===request.project &&
      share.operation===request.operation && share.native_operation===ref.native_operation))throw new StoreError("native_project_unavailable");
    return this.document(s.head,entry);
  }
  async write(args:unknown) {
    const a=inputSchema.parse(args);
    if(a.expected_generation!==this.generation)throw new StoreError("native_generation_changed_reconfigure");
    await validateNativeFiles(a.files,a.project,this.nativeOwner);
    const fingerprint=await checksum(a),key=await hash(JSON.stringify([this.owner,"native-write",a.request_id]));
    const receiptPath=`native/requests/${key}.json`;
    const retry=async(s:Awaited<ReturnType<GitCoreStore["current"]>>) => {
      const entry=s.catalog.projects.find(p=>p.project===a.project);
      if(entry && !entry.enabled)throw new StoreError("native_project_unavailable");
      const raw=await this.git.read(s.head,receiptPath);
      if(raw===null)return null;
      if(!entry)throw new StoreError("native_project_unavailable");
      const receipt=receiptSchema.safeParse(raw);
      if(!receipt.success || receipt.data.owner!==this.owner)throw invalid();
      if(receipt.data.input_sha256!==fingerprint)throw new StoreError("request_id_reused_with_different_content");
      if(receipt.data.result.project!==a.project || receipt.data.result.revision>entry.revision)throw invalid();
      return receipt.data.result;
    };
    const s=await this.current(),prior=await retry(s);
    if(prior)return prior;
    const entry=s.catalog.projects.find(p=>p.project===a.project);
    if(entry?.write_state==="frozen")throw new StoreError("native_project_frozen");
    if((entry?.revision??0)!==a.expected_revision)throw new StoreError("native_revision_conflict_reread");
    if((entry?.document_sha256??null)!==a.expected_document_sha256)throw new StoreError("native_checkpoint_changed_reread");
    const previous=entry?await this.document(s.head,entry):null;
    if(previous && canonical(previous.origin)!==canonical(a.origin))throw new StoreError("native_origin_changed");
    if(previous)preserveHistory(previous.files,a.files);
    if(!previous && !a.origin.generation.startsWith("native-freeze-"))throw new StoreError("native_frozen_source_required");
    if(a.expected_revision===Number.MAX_SAFE_INTEGER)throw invalid();
    const doc:Document={schema:1,project:a.project,owner:this.nativeOwner,revision:a.expected_revision+1,
      previous_sha256:entry?.document_sha256??null,origin:a.origin,files:a.files};
    const result={project:a.project,revision:doc.revision,document_sha256:await checksum(doc)};
    const next={...result,enabled:true,write_state:"active" as const,shares:entry?.shares??[]};
    if(entry)s.catalog.projects[s.catalog.projects.indexOf(entry)]=next;else s.catalog.projects.push(next);
    catalogSchema.parse(s.catalog);
    try {
      await this.git.commit(s.head,{"native/catalog.json":s.catalog,[documentPath(a.project,doc.revision)]:doc,
        [receiptPath]:{owner:this.owner,input_sha256:fingerprint,result}});
    } catch {
      const recovered=await retry(await this.current());
      if(recovered)return recovered;
      throw new StoreError("native_save_outcome_unknown_retry_same_request");
    }
    return result;
  }
}
