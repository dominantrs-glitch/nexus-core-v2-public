/** Reversible project organization. Source ledgers and ACLs never move. */
import {z} from 'zod';
import {hash} from './relay-common';
import {type GitBackend,StoreError} from './git-store';

const id=z.string().regex(/^[a-z0-9][a-z0-9_-]{0,79}$/),revision=z.number().int().safe().nonnegative();
const text=z.string().trim().min(1).max(2000);
const relation=z.enum(['parent','depends_on','surface_of','related']);
const record=z.object({status:z.enum(['active','archived','merged']),canonical:id.nullable(),
  quote:text,source:text,reason:text}).strict();
const edge=z.object({project:id,target:id,relation,quote:text,source:text,reason:text}).strict();
export const lifecycleSchema=z.object({schema:z.literal(1),owner:z.string(),generation:id,revision,
  records:z.record(id,record),relations:z.array(edge).max(500)}).strict();
export type Lifecycle=z.infer<typeof lifecycleSchema>;
export type Catalog={owner:string;generation:string;projects:{id:string;title:string;remote:boolean;revision:number;write_state?:string}[]};
export const lifecycleReadInput=z.object({project:id.optional(),detail:z.enum(['status','integrity','deletion_review']).default('status')}).strict();
export const lifecyclePlanInput=z.object({action:z.enum(['archive','restore','merge','relate','unrelate','rollback']),
  project:id,target:id.optional(),relation:relation.optional(),event_revision:revision.optional(),
  quote:text,source:text,reason:text}).strict();
export const lifecycleApplyInput=lifecyclePlanInput.extend({plan_digest:z.string().regex(/^[a-f0-9]{64}$/),request_id:z.string().min(1).max(150)}).strict();
type Plan=z.infer<typeof lifecyclePlanInput>;
const identity=(v:Lifecycle,c:Catalog)=>v.owner===c.owner&&v.generation===c.generation;
export async function loadLifecycle(git:GitBackend,commit:string,catalog:Catalog):Promise<Lifecycle>{
  const raw=await git.read(commit,'lifecycle.json');
  if(raw===null)return {schema:1,owner:catalog.owner,generation:catalog.generation,revision:0,records:{},relations:[]};
  const v=lifecycleSchema.safeParse(raw);
  if(!v.success||!identity(v.data,catalog))throw new StoreError('lifecycle_invalid_or_unavailable');
  return v.data;
}
export function canonical(l:Lifecycle,project:string):string{
  const seen=new Set<string>();let p=project;
  while(l.records[p]?.status==='merged'){
    if(seen.has(p)||!l.records[p].canonical)throw new StoreError('lifecycle_invalid_alias');
    seen.add(p);p=l.records[p].canonical!;
  }
  return p;
}
export function visible(l:Lifecycle,project:string){return !l.records[project]||l.records[project].status==='active';}
export function inActiveFamily(l:Lifecycle,project:string){return l.records[project]?.status!=='archived'&&visible(l,canonical(l,project));}
export function lifecycleView(l:Lifecycle,c:Catalog,project:string){
  const allowed=new Set(c.projects.filter(p=>p.remote).map(p=>p.id));
  const target=canonical(l,project),targetAvailable=allowed.has(target);
  return {status:l.records[project]?.status??'active',canonical_project:targetAvailable?target:null,
    canonical_available:targetAvailable,revision:l.revision,
    related_history:targetAvailable?c.projects.filter(p=>p.remote&&p.id!==target&&canonical(l,p.id)===target).map(p=>({project:p.id,title:p.title,revision:p.revision})):[],
    relations:l.relations.filter(e=>allowed.has(e.project)&&allowed.has(e.target))
      .map(e=>({...e,canonical_project:canonical(l,e.project),canonical_target:canonical(l,e.target)}))
      .filter(e=>allowed.has(e.canonical_project)&&allowed.has(e.canonical_target)&&
        [e.project,e.target,e.canonical_project,e.canonical_target].includes(project)),
    instruction:'Canonical is the destination for new work. Read related_history when relevant; source IDs retain their notes, originals and learning. Organization never inherits permissions, owner values or Contract approval. Archived projects remain directly readable and restorable.'};
}
export function integrity(l:Lifecycle,c:Catalog){
  const ids=new Set(c.projects.map(p=>p.id)),issues:{type:string;projects:string[]}[]=[];
  for(const [p,r] of Object.entries(l.records)){
    if(!ids.has(p)||r.canonical&&!ids.has(r.canonical))issues.push({type:'missing_project_reference',projects:[p,...r.canonical?[r.canonical]:[]]});
    if(r.status==='merged'?!r.canonical:!!r.canonical)issues.push({type:'invalid_canonical_state',projects:[p]});
    try{canonical(l,p);}catch{issues.push({type:'alias_cycle',projects:[p]});}
  }
  const keys=new Set<string>();const parents=new Map<string,string>();
  for(const e of l.relations){
    if(!ids.has(e.project)||!ids.has(e.target))issues.push({type:'missing_relation_target',projects:[e.project,e.target]});
    let project:string,target:string;
    try{project=canonical(l,e.project);target=canonical(l,e.target);}catch{continue;}
    const key=[project,target,e.relation].join(':');
    if(keys.has(key))issues.push({type:'duplicate_relation',projects:[e.project,e.target]});keys.add(key);
    if(project===target)issues.push({type:'self_relation',projects:[e.project,e.target]});
    if(e.relation==='parent'){
      if(parents.has(project))issues.push({type:'multiple_parents',projects:[e.project]});parents.set(project,target);
    }
  }
  for(const p of parents.keys()){
    const seen=new Set<string>();let next:string|undefined=p;
    while(next){if(seen.has(next)){issues.push({type:'parent_cycle',projects:[p]});break;}seen.add(next);next=parents.get(next);}
  }
  return issues;
}
export async function previewLifecycle(git:GitBackend,commit:string,c:Catalog,l:Lifecycle,a:Plan){
  const project=(id:string)=>{
    const p=c.projects.find(p=>p.id===id&&p.remote);
    if(!p)throw new StoreError('project_unavailable');
    if(p.write_state==='frozen')throw new StoreError('project_frozen_for_migration');return p;
  };
  const p=project(a.project),t=a.target?project(a.target):null;
  if(['merge','relate','unrelate'].includes(a.action)?!t||t.id===p.id:!!t)throw new StoreError('invalid_lifecycle_target');
  if(['relate','unrelate'].includes(a.action)?!a.relation:a.relation!==undefined)throw new StoreError('invalid_lifecycle_relation');
  if(a.action==='rollback'?a.event_revision===undefined:a.event_revision!==undefined)throw new StoreError('invalid_lifecycle_event');
  const next=structuredClone(l),entry={quote:a.quote,source:a.source,reason:a.reason};
  if(['archive','merge','relate'].includes(a.action)&&canonical(l,p.id)!==p.id)throw new StoreError('project_merged_use_canonical_or_restore');
  if(a.action==='archive')next.records[p.id]={...entry,status:'archived',canonical:null};
  if(a.action==='restore')next.records[p.id]={...entry,status:'active',canonical:null};
  if(a.action==='merge'){
    if(!visible(l,p.id)||!visible(l,t!.id)||canonical(l,t!.id)!==t!.id)throw new StoreError('merge_requires_active_canonical_projects');
    next.records[p.id]={...entry,status:'merged',canonical:t!.id};
    // Keep source identity/provenance, but validate the effective canonical
    // graph below. A collapsed edge/cycle requires an explicit relation edit.
  }
  if(a.action==='relate'){
    if(!visible(l,p.id)||!visible(l,t!.id))throw new StoreError('relation_requires_active_projects');
    next.relations=next.relations.filter(e=>!(e.project===p.id&&(e.relation===a.relation&&(a.relation==='parent'||e.target===t!.id))));
    next.relations.push({project:p.id,target:t!.id,relation:a.relation!,...entry});
  }
  if(a.action==='unrelate')next.relations=next.relations.filter(e=>!(e.project===p.id&&e.target===t!.id&&e.relation===a.relation));
  if(a.action==='rollback'){
    const event:any=await git.read(commit,`lifecycle/events/${a.event_revision}.json`);
    if(!event||event.project!==p.id||a.event_revision!==l.revision||event.after_digest!==await hash(JSON.stringify(l)))
      throw new StoreError('rollback_requires_latest_matching_event');
    const previous=lifecycleSchema.parse(event.before);
    if(!identity(previous,c))throw new StoreError('lifecycle_invalid_or_unavailable');
    next.records=previous.records;next.relations=previous.relations;
  }
  next.revision=l.revision+1;
  if(integrity(next,c).length)throw new StoreError('lifecycle_integrity_review_required');
  const through=(id:string)=>{
    const seen=new Set<string>();let current:string|null=id;
    while(current&&!seen.has(current)){if(current===p.id)return true;seen.add(current);current=l.records[current]?.canonical??null;}
    return false;
  };
  const family=c.projects.filter(e=>e.remote&&through(e.id)).map(e=>({project:e.id,revision:e.revision}));
  const impact={project:p.id,target:t?.id??null,affected:family,target_revision:t?.revision??null,
    previous_status:l.records[p.id]?.status??'active',next_status:next.records[p.id]?.status??'active',
    preserves:['notes','source IDs','history','artifacts','learning','permissions','pending work'],
    active_list:a.action==='archive'?'hidden':a.action==='merge'?'canonical target only':'review resulting state',
    external_dependencies:'not changed; inspect operating apps and scheduled jobs separately'};
  const plan_digest=await hash(JSON.stringify({owner:c.owner,generation:c.generation,revision:l.revision,a,impact,next}));
  return {plan_digest,impact,before:l,next,binding:false,instruction:'Preview is not approval. Apply only the explicitly requested organization, retaining this exact digest and request ID. No deletion, permission inheritance or native owner confirmation.'};
}
export async function applyLifecycle(git:GitBackend,snapshot:()=>Promise<{commit:string;catalog:Catalog}>,args:unknown){
  const a=lifecycleApplyInput.parse(args),{request_id,plan_digest,...plan}=a;
  let s=await snapshot();const key=`lifecycle/requests/${await hash(JSON.stringify([s.catalog.owner,request_id]))}.json`;
  const digest=await hash(JSON.stringify(a));
  for(let retry=0;retry<3;retry++){
    const old:any=await git.read(s.commit,key);
    if(old){
      if(!s.catalog.projects.some(p=>p.id===a.project&&p.remote))throw new StoreError('project_unavailable');
      if(old.digest!==digest)throw new StoreError('request_key_reused_with_different_content');
      return {...old.result,view:lifecycleView(await loadLifecycle(git,s.commit,s.catalog),s.catalog,a.project)};
    }
    const l=await loadLifecycle(git,s.commit,s.catalog),preview=await previewLifecycle(git,s.commit,s.catalog,l,plan);
    if(preview.plan_digest!==plan_digest)throw new StoreError('lifecycle_changed_preview_again');
    const result={status:'saved-organization',project:a.project,revision:preview.next.revision,
      binding:false,view:lifecycleView(preview.next,s.catalog,a.project)};
    const event={schema:1,project:a.project,action:a.action,quote:a.quote,source:a.source,reason:a.reason,
      before:l,after_digest:await hash(JSON.stringify(lifecycleSchema.parse(preview.next))),impact:preview.impact};
    try{await git.commit(s.commit,{'lifecycle.json':preview.next,[`lifecycle/events/${preview.next.revision}.json`]:event,[key]:{digest,result}});return result;}
    catch(e){
      if(e instanceof StoreError&&e.diagnostic)throw e;
      s=await snapshot();const receipt:any=await git.read(s.commit,key);
      if(receipt?.digest===digest&&s.catalog.projects.some(p=>p.id===a.project&&p.remote))
        return {...receipt.result,view:lifecycleView(await loadLifecycle(git,s.commit,s.catalog),s.catalog,a.project)};
      if(!(e instanceof StoreError&&e.code==='canonical_conflict_reread'&&retry<2))throw e;
    }
  }
  throw new StoreError('canonical_conflict_reread');
}
