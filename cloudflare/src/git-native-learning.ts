/** Repaired-case evidence remains a candidate, never permission, UAT or a rule. */
import {z} from 'zod';
import {hash} from './relay-common';
import {nativeRef,readNativeContext,type NativeRequest,type NativeRef} from './git-native-context';
const id=z.string().regex(/^[a-z0-9][a-z0-9_-]{0,79}$/),name=z.string().min(1).max(500);
const version=z.number().int().positive().safe(),sha=z.string().regex(/^[a-f0-9]{64}$/);
const names=z.array(name),text=z.string().min(1).max(8000);
export const nativeLearningRef=z.object({native_learning:name,source_project:id,revision:version,target:nativeRef}).strict();
export type NativeLearningRef=z.infer<typeof nativeLearningRef>;
type Document={owner:string;project:string;revision:number;files:Record<string,string>};
const lessonSchema=z.object({schema:z.literal('nexus.shared-lesson.v1'),principle:text,rationale:text,
  reuse_scope:z.enum(['project','task_type','general']),applicable_work_types:names,reuse_basis:z.string().max(8000),
  judgment_layer:z.enum(['candidate','work_knowledge']),required_constraints:names,forbidden_constraints:names,
  evidence_level:z.literal('repaired-case-machine-and-contract'),binding:z.literal(false),destinations:z.array(id).min(1)}).strict();
const proofSchema=z.object({schema:z.literal('nexus.shared-lesson-proof.v1'),project:id,generation:name,
  checkpoint:z.object({revision:version,sha256:sha}).strict(),contract_revision:version,contract_sha256:sha,
  output_event:version,output_sha256:sha,correction_event:version,check_events:z.array(version).min(2).max(200)}).strict();
const ordered=(v:any):any=>Array.isArray(v)?v.map(ordered):v!==null&&typeof v==='object'?
  Object.fromEntries(Object.keys(v).sort().map(k=>[k,ordered(v[k])])):v;
const checksum=(v:unknown)=>hash(JSON.stringify(ordered(v)));
const decode=(files:Record<string,string>,path:string)=>JSON.parse(new TextDecoder('utf-8',{fatal:true,ignoreBOM:true})
  .decode(Uint8Array.from(atob(files[path]),c=>c.charCodeAt(0))));
const unavailable=()=>new Error('native_learning_unavailable');
function latest(rows:any[],identity:string) {
  if(!Array.isArray(rows))throw unavailable();
  const matching=rows.filter(r=>r.id===identity);
  if(!matching.length||matching.some(r=>!Number.isSafeInteger(r.revision)||r.revision<1)||new Set(matching.map(r=>r.revision)).size!==matching.length)throw unavailable();
  return matching.reduce((a,b)=>a.revision>b.revision?a:b);
}
export async function readNativeLearning(ref:NativeLearningRef,request:NativeRequest,generation:string,
  load:(project:string,operation:'implement'|'review')=>Promise<Document>) {
  if(request.mode!=='delegate')throw unavailable();
  const source=await load(ref.source_project,'review'),target=await load(ref.target.native_project,ref.target.native_operation);
  const contexts=decode(source.files,'context.json'),record=latest(contexts.contexts,ref.native_learning);
  if(record.project!==source.project||record.owner!==source.owner||record.revision!==ref.revision||record.kind!=='candidate'||record.authority!=='candidate'||
    record.status!=='current'||!record.readers.includes(source.owner)||!record.projects.includes(target.project)||!record.operations.includes(ref.target.native_operation))throw unavailable();
  const evidence=latest(contexts.sources,record.source_id);
  if(evidence.project!==source.project||evidence.owner!==source.owner||evidence.revision!==record.source_revision||await hash(evidence.body)!==evidence.sha256)throw unavailable();
  const lesson=lessonSchema.parse(JSON.parse(record.body)),proof=proofSchema.parse(JSON.parse(evidence.body));
  if(proof.project!==source.project||proof.generation!==generation||source.revision<proof.checkpoint.revision||
    source.revision===proof.checkpoint.revision&&await checksum(source)!==proof.checkpoint.sha256||
    !lesson.destinations.includes(target.project))throw unavailable();
  const src=await readNativeContext({native_project:source.project,contract_revision:proof.contract_revision,native_operation:'review'},request,async()=>source);
  const dst=await readNativeContext(ref.target,request,async()=>target);
  if(await checksum(src.contract)!==proof.contract_sha256)throw unavailable();
  const state=decode(source.files,'project.json');
  const events=state.events.filter((e:any)=>e.revision===proof.contract_revision);
  if(events.some((e:any)=>!Number.isSafeInteger(e.seq)||e.seq<1)||new Set(events.map((e:any)=>e.seq)).size!==events.length)throw unavailable();
  events.sort((a:any,b:any)=>a.seq-b.seq);
  const output=events.filter((e:any)=>e.kind==='output').at(-1);
  const correction=events.filter((e:any)=>e.kind==='correction'&&e.seq<(output?.seq??0)).at(-1);
  if(!output||output.seq!==proof.output_event||output.payload.sha256!==proof.output_sha256||correction?.seq!==proof.correction_event||
    events.some((e:any)=>e.kind==='correction'&&e.seq>output.seq))throw unavailable();
  const checks=[];
  for(const acceptance of Object.keys(src.contract.acceptance))for(const kind of ['machine','contract']) {
    const check=events.filter((e:any)=>e.kind==='verification'&&e.payload.output_event===output.seq&&e.payload.acceptance===acceptance&&e.payload.kind===kind).at(-1);
    if(check?.payload.status!=='pass')throw unavailable();checks.push(check.seq);
  }
  if(JSON.stringify(checks.sort((a,b)=>a-b))!==JSON.stringify([...proof.check_events].sort((a,b)=>a-b)))throw unavailable();
  const artifacts=decode(source.files,'artifacts/manifest.json').entries.filter((a:any)=>a.artifact_id===output.payload.artifact_id&&a.revision===output.payload.revision);
  if(artifacts.length!==1||artifacts[0].project!==source.project||artifacts[0].sha256!==output.payload.sha256||!artifacts[0].principals.includes(source.owner))throw unavailable();
  if(!lesson.required_constraints.every(c=>dst.contract.constraints.includes(c))||lesson.forbidden_constraints.some(c=>dst.contract.constraints.includes(c)))throw unavailable();
  if(lesson.reuse_scope==='project'&&(!lesson.required_constraints.length||lesson.applicable_work_types.length))throw unavailable();
  if(lesson.reuse_scope!=='project'&&!lesson.reuse_basis.trim())throw unavailable();
  if(lesson.reuse_scope==='general'&&(lesson.applicable_work_types.length||lesson.judgment_layer!=='candidate'))throw unavailable();
  if(lesson.reuse_scope==='task_type') {
    const classification=decode(target.files,'project.json').events.filter((e:any)=>e.revision===dst.contract_revision&&e.kind==='work_classification').at(-1);
    if(!lesson.applicable_work_types.length||!classification?.payload?.work_types?.some((t:string)=>lesson.applicable_work_types.includes(t)))throw unavailable();
  }
  return {id:record.id,revision:record.revision,principle:lesson.principle,rationale:lesson.rationale,reuse_scope:lesson.reuse_scope,
    required_constraints:lesson.required_constraints,forbidden_constraints:lesson.forbidden_constraints,
    authority:'candidate',binding:false,verification:'current-native-repaired-case-machine-and-contract',
    source:{project:source.project,contract_revision:proof.contract_revision,output_event:output.seq,output_sha256:proof.output_sha256},
    target:{project:target.project,contract_revision:dst.contract_revision},
    instruction:'A scoped candidate based on reported machine/Contract checks, not a confirmed rule, permission, UAT or guaranteed general principle. Recheck current target requirements before applying. Raw correction text, private lessons and owner audit are omitted.'};
}
export type NativeLearning=Awaited<ReturnType<typeof readNativeLearning>>;
