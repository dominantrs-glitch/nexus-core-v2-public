/** Scoped native read projections. Imported bytes never create a confirmation. */
import {z} from "zod";
import {hash} from "./relay-common";

const id=z.string().regex(/^[a-z0-9][a-z0-9_-]{0,79}$/),name=z.string().min(1).max(500);
const version=z.number().int().positive().safe();
const operation=z.enum(["resume","plan","implement","review"]);
const nativeOperation=z.enum(["implement","review"]);
export const nativeRef=z.object({native_project:id,contract_revision:version,native_operation:nativeOperation}).strict();
export const nativeShare=z.object({project:id,operation,native_operation:nativeOperation}).strict();
export type NativeRef=z.infer<typeof nativeRef>;
export type NativeRequest={project:string;operation:z.infer<typeof operation>;mode:"delegate"|"independent"|"red-team"};
const strings=z.array(name);
const contractSchema=z.object({revision:version,goal:z.string().min(1).max(20000),acceptance:z.record(name,z.array(z.enum(["machine","contract","uat","operational"])).min(1)),
  constraints:z.array(z.string().min(1).max(20000)),authority:z.literal("confirmed"),source:z.string().min(1).max(20000)}).strict();
const recordSchema=z.object({id:name,revision:version,project:id,owner:name,kind:z.enum(["decision","personal","reference","candidate"]),
  authority:z.enum(["confirmed","verified","derived","candidate","unknown"]),status:z.enum(["current","historical","superseded","suppressed"]),
  source_id:name,source_revision:version,body:z.string().min(1).max(32768),approval_receipt:z.string().nullable(),
  projects:strings.min(1),operations:strings.min(1),readers:strings.min(1)}).strict();
const sourceSchema=z.object({id:name,revision:version,project:id,owner:name,body:z.string(),sha256:z.string().regex(/^[a-f0-9]{64}$/)}).strict();
const policySchema=z.object({project:id,contract_revision:version,operation:nativeOperation,revision:version,owner:name,principal:name,
  payload:z.object({required_context:strings,required_authority:z.record(name,z.enum(["confirmed","verified","derived","candidate"])),
    required_artifacts:z.array(z.tuple([name,version]))}).strict()}).strict();
const unavailable=()=>new Error("native_context_unavailable");
const decode=(files:Record<string,string>,path:string)=>JSON.parse(new TextDecoder("utf-8",{fatal:true,ignoreBOM:true})
  .decode(Uint8Array.from(atob(files[path]),c=>c.charCodeAt(0))));
const ordered=(value:any):any=>Array.isArray(value)?value.map(ordered):value!==null&&typeof value==='object'?
  Object.fromEntries(Object.keys(value).sort().map(key=>[key,ordered(value[key])])):value;
const sorted=(values:string[])=>[...values].sort((a,b)=>{
  const x=Array.from(a,c=>c.codePointAt(0)!),y=Array.from(b,c=>c.codePointAt(0)!);
  for(let i=0;i<Math.min(x.length,y.length);i++)if(x[i]!==y[i])return x[i]-y[i];
  return x.length-y.length;
});
function uniqueLatest<T extends {id:string;revision:number}>(rows:T[]) {
  const seen=new Set<string>(),latest=new Map<string,T>();
  for(const row of rows) {
    const key=JSON.stringify([row.id,row.revision]);if(seen.has(key))throw unavailable();seen.add(key);
    if(!latest.has(row.id)||latest.get(row.id)!.revision<row.revision)latest.set(row.id,row);
  }
  return latest;
}

export async function readNativeContext(ref:NativeRef,request:NativeRequest,
  load:()=>Promise<{owner:string;project:string;revision:number;files:Record<string,string>}>) {
  const doc=await load(); // caller gates native share/owner/snapshot before loading any document
  if(doc.project!==ref.native_project)throw unavailable();
  const state=decode(doc.files,"project.json"),context=decode(doc.files,"context.json"),audit=decode(doc.files,"approvals.json");
  if(state.project.id!==doc.project || state.project.owner!==doc.owner || state.project.revision!==ref.contract_revision ||
    audit.project!==doc.project || audit.owner!==doc.owner || !Array.isArray(audit.approvals))throw unavailable();
  const contracts=z.array(contractSchema).parse(state.contracts);
  const selected=contracts.filter(c=>c.revision===ref.contract_revision);
  if(selected.length!==1 || contracts.some(c=>c.revision>ref.contract_revision))throw unavailable();
  const contract=selected[0];
  const confirm=async(receipt:unknown,kind:string,revision:number,summary:string)=>{
    if(typeof receipt!=="string" || !/^local-approval-[1-9][0-9]*$/.test(receipt))throw unavailable();
    const matches=audit.approvals.filter((r:any)=>`local-approval-${r.id}`===receipt);
    const r=matches[0];
    // Same canonical ApprovalRequest encoding used by the native OwnerAdapter.
    const expected=await hash(JSON.stringify({kind,project:doc.project,summary,target_revision:revision}));
    if(matches.length!==1 || r.owner!==doc.owner || r.project!==doc.project || r.kind!==kind ||
      r.target_revision!==revision || r.summary_digest!==expected || !Number.isFinite(Date.parse(r.approved_at)))throw unavailable();
    return {kind,revision,approved_at:r.approved_at};
  };
  const confirmations=state.events.filter((e:any)=>e.kind==="confirmation" && e.revision===contract.revision);
  if(confirmations.length!==1)throw unavailable();
  const summary=`Goal:\n${contract.goal}\n\nAcceptance（必須検証）:\n${JSON.stringify(contract.acceptance,null,2)}`+
    `\n\nConstraints:\n${contract.constraints.join("\n")}\n\nSource: ${contract.source}`;
  const proof=await confirm(confirmations[0].payload.approval_receipt,"confirmed_contract",contract.revision,summary);
  const policies=z.array(policySchema).parse(context.policies.filter((p:any)=>p.operation===ref.native_operation && p.contract_revision===contract.revision));
  if(!policies.length || new Set(policies.map(p=>p.revision)).size!==policies.length)throw unavailable();
  const policy=policies.reduce((a,b)=>a.revision>b.revision?a:b);
  if(policy.project!==doc.project || policy.owner!==doc.owner || policy.principal!==doc.owner ||
    Object.keys(policy.payload.required_authority).some(id=>!policy.payload.required_context.includes(id)))throw unavailable();
  const records=uniqueLatest(z.array(recordSchema).parse(context.contexts));
  const sources=z.array(sourceSchema).parse(context.sources);
  const latestSources=uniqueLatest(sources);
  const required=[];
  for(const identity of new Set(policy.payload.required_context)) {
    const r=records.get(identity);
    if(!r || r.project!==doc.project || r.owner!==doc.owner || r.status!=="current" || r.authority==="unknown" ||
      !r.projects.includes(doc.project) || !r.operations.includes(ref.native_operation) || !r.readers.includes(doc.owner) ||
      (policy.payload.required_authority[identity] && policy.payload.required_authority[identity]!==r.authority) ||
      (request.mode!=="delegate" && ["personal","candidate"].includes(r.kind)))throw unavailable();
    // A later source revision needs a new context revision; never silently reuse it.
    const source=latestSources.get(r.source_id);
    if(!source || source.revision!==r.source_revision || source.project!==doc.project || source.owner!==doc.owner ||
      await hash(source.body)!==source.sha256)throw unavailable();
    let confirmation;
    if(r.kind==="decision") {
      if(r.authority!=="confirmed")throw unavailable();
      const summary=`Decision ID: ${r.id}\n\n本文:\n${r.body}\n\nScope:\nProjects: ${sorted(r.projects).join(", ")}`+
        `\nOperations: ${sorted(r.operations).join(", ")}\nReaders: ${sorted(r.readers).join(", ")}`;
      confirmation=await confirm(r.approval_receipt,"confirmed_decision",r.revision,summary);
    }
    if(r.kind==="candidate" && r.authority!=="candidate")throw unavailable();
    let content:{body:string}|{content_ref:"contract"}={body:r.body};
    if(r.kind==="reference" && r.authority==="derived" && source.body===r.body) {
      try {
        if(JSON.stringify(ordered(JSON.parse(r.body)))===JSON.stringify(ordered(contract)))content={content_ref:"contract"};
      } catch { /* A non-JSON derived reference keeps its exact body. */ }
    }
    required.push({id:r.id,revision:r.revision,kind:r.kind,authority:r.authority,...content,
      binding:r.kind==="decision" && r.authority==="confirmed",binding_project:doc.project,
      source:{id:source.id,revision:source.revision,sha256:source.sha256},...(confirmation?{confirmation}: {})});
  }
  const artifacts=decode(doc.files,"artifacts/manifest.json");
  for(const [id,revision] of policy.payload.required_artifacts) {
    const matches=artifacts.entries.filter((a:any)=>a.artifact_id===id && a.revision===revision);
    if(matches.length!==1 || matches[0].project!==doc.project || !matches[0].principals.includes(doc.owner))throw unavailable();
  }
  const result={project:doc.project,contract_revision:contract.revision,storage_revision:doc.revision,native_operation:ref.native_operation,
    contract,confirmation:proof,required_context:required,required_artifacts_verified:policy.payload.required_artifacts.length,
    policy_revision:policy.revision,authority:"native-confirmed-source",binding_project:doc.project,
    instruction:"Confirmation applies only to this native source project. This read grants no permission, confirms no new target Contract, and does not establish UAT or completion. Only required scoped context is included; owner capabilities and the full audit are omitted."};
  if(new TextEncoder().encode(JSON.stringify(result)).length>32768)throw unavailable();
  return result;
}
export type NativeContext=Awaited<ReturnType<typeof readNativeContext>>;
